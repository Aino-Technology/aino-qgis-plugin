import copy
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal

from .area import require_gui_thread
from .recovery import (
    PROVENANCE_KEY,
    RecoveryJournal,
    project_identity,
    project_persistence,
    replace_project_identity,
)
from .upload_transport import UploadTransport
from .styles import canonical_bytes


WIRE_KEYS = (
    "client_layer_id",
    "name",
    "visible",
    "order",
    "content_hash",
    "style_hash",
    "style",
    "field_schema",
)


def _reference_style(item, provenance):
    if item["style_hash"] == provenance["local_style_hash"]:
        return copy.deepcopy(provenance["server_style"]), provenance[
            "server_style_hash"
        ]
    style = copy.deepcopy(item["style"])
    if style.get("dataset_type") == "default":
        return style, item["style_hash"]
    if style.get("dataset_type") != "groups":
        return None
    mapping = provenance.get("class_mapping")
    if not isinstance(mapping, dict) or any(
        mapping.get(key) != provenance[key]
        for key in ("dataset_id", "version", "content_hash")
    ):
        return None
    if mapping.get("server_field") not in {
        field["name"] for field in provenance["field_schema"]
    } or not isinstance(mapping.get("classes"), dict):
        return None
    for group in style.get("style", {}).get("groups", []):
        if group.get("column") != mapping.get("local_field"):
            return None
        mapped = []
        for value in group.get("values", []):
            values = mapping["classes"].get(value)
            if not isinstance(values, list) or any(
                isinstance(value, (dict, list)) for value in values
            ):
                return None
            mapped.extend(values)
        group["column"] = mapping["server_field"]
        group["values"] = mapped
    local_metadata = style.get("qgis", {})
    style["qgis"] = {
        **copy.deepcopy(provenance["server_style"].get("qgis", {})),
        "local_presentation": local_metadata,
    }
    return style, hashlib.sha256(canonical_bytes(style)).hexdigest()


def _output_references(layers, account_id, client_project_id, project_id):
    grouped = {}
    for layer in layers:
        provenance = layer.get("output_provenance")
        if isinstance(provenance, dict):
            grouped.setdefault(provenance.get("dataset_id"), []).append(layer)
    replacements = {}
    for dataset_id, parts in grouped.items():
        first = parts[0]["output_provenance"]
        required = {
            "account_id",
            "client_project_id",
            "project_id",
            "dataset_id",
            "version",
            "content_hash",
            "field_schema",
            "part_id",
            "part_ids",
            "local_content_hash",
            "local_style_hash",
            "server_style",
            "server_style_hash",
        }
        if not required <= first.keys():
            continue
        if (
            first["account_id"] != account_id
            or first["client_project_id"] != client_project_id
            or first["project_id"] != project_id
        ):
            continue
        identity = (
            "account_id",
            "client_project_id",
            "project_id",
            "dataset_id",
            "version",
            "content_hash",
            "field_schema",
            "part_ids",
            "server_style",
            "server_style_hash",
        )
        if any(
            not required <= item["output_provenance"].keys()
            or any(item["output_provenance"][key] != first[key] for key in identity)
            or item["content_hash"] != item["output_provenance"]["local_content_hash"]
            for item in parts
        ):
            continue
        part_ids = [item["output_provenance"]["part_id"] for item in parts]
        if len(set(part_ids)) != len(part_ids) or set(part_ids) != set(
            first["part_ids"]
        ):
            continue
        if len(parts) > 1 and (
            any(
                item["style_hash"] != item["output_provenance"]["local_style_hash"]
                for item in parts
            )
            or len({item["visible"] for item in parts}) != 1
            or len({item["name"] for item in parts}) != 1
            or sorted(item["order"] for item in parts)
            != list(
                range(
                    min(item["order"] for item in parts),
                    min(item["order"] for item in parts) + len(parts),
                )
            )
        ):
            continue
        presentation = _reference_style(parts[0], first)
        if presentation is None:
            continue
        wire = {key: copy.deepcopy(parts[0][key]) for key in WIRE_KEYS}
        wire.update(style=presentation[0], style_hash=presentation[1])
        wire.update(
            client_layer_id=f"output:{dataset_id}",
            dataset_id=dataset_id,
            dataset_version=first["version"],
            content_hash=first["content_hash"],
            field_schema=first["field_schema"],
        )
        if len(parts) > 1:
            wire.update(
                style=first["server_style"], style_hash=first["server_style_hash"]
            )
        replacements[parts[0]["client_layer_id"]] = wire
        for item in parts[1:]:
            replacements[item["client_layer_id"]] = None
    return replacements


class SyncController(QObject):
    """Prepare and recover one frozen snapshot without starting or cancelling analysis."""

    ready = pyqtSignal(dict)
    failed = pyqtSignal(str)
    notice = pyqtSignal(str)
    status = pyqtSignal(str)
    relink_required = pyqtSignal(str)

    def __init__(
        self, api_client, *, journal=None, uploader=None, parent=None, poll_ms=4000
    ):
        super().__init__(parent)
        self.api = api_client
        self.session = api_client.session
        self.journal = journal or RecoveryJournal()
        self.uploader = uploader or UploadTransport(self)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._advance)
        self.poll_ms = poll_ms
        self._generation = 0
        self._handle = None
        self._closed = False
        self._callback = None
        self.project = None
        self._persistence = None
        self.account_id = None
        self.client_project_id = None
        self.state = None
        self.datasets = {}
        self.session.account_changed.connect(self._account_changed)

    @property
    def busy(self):
        return self.state is not None and self.state.get("phase") not in {
            "ready",
            "failed",
            "blocked",
        }

    def _bind(self, project):
        require_gui_thread()
        if self._closed:
            raise RuntimeError("Synchronization controller is closed")
        if not self.session.account_id:
            raise ValueError("Sign in before synchronizing inputs")
        identity = project_identity(project)
        if (
            self.project is not project
            or self.account_id != self.session.account_id
            or self.client_project_id != identity
        ):
            self.pause()
        self.project = project
        self.account_id = self.session.account_id
        self.client_project_id = identity
        loaded = self.journal.load(identity, self.account_id) or {}
        self.datasets = loaded.get("datasets", {})
        self.state = loaded.get("sync")
        self._watch_persistence(project)

    def _watch_persistence(self, project):
        tracker = project_persistence(project)
        if tracker is not self._persistence:
            self._unwatch_persistence()
            self._persistence = tracker
            tracker.changed.connect(self._identity_notice)
        self._identity_notice()

    def _unwatch_persistence(self):
        if self._persistence is not None:
            try:
                self._persistence.changed.disconnect(self._identity_notice)
            except (TypeError, RuntimeError):
                pass
            self._persistence = None

    def _identity_notice(self):
        if self._persistence is not None and not self._closed:
            self.notice.emit(self._persistence.notice)

    def prepare(self, project, layers, on_ready, *, context=None):
        """Accept completed captures; frozen pending work must be reconciled before new preparation."""
        if (
            self.project is project
            and self.account_id == self.session.account_id
            and self.busy
        ):
            raise ValueError(
                "Resume the pending synchronization before preparing new inputs"
            )
        self._bind(project)
        if self.busy:
            raise ValueError(
                "Resume the pending synchronization before preparing new inputs"
            )
        if self.state and self.state.get("phase") == "blocked":
            self.relink_required.emit(
                "Linked project is inaccessible. Restore access or explicitly detach and create a new link."
            )
            return
        self._callback = on_ready
        operation_id = str(uuid4())
        captured = copy.deepcopy(layers)
        for item in captured:
            layer = project.mapLayer(item["client_layer_id"])
            raw = layer.customProperty(PROVENANCE_KEY, "") if layer else ""
            if raw:
                try:
                    item["output_provenance"] = json.loads(raw)
                except (ValueError, TypeError):
                    pass
        owned = self.journal.adopt(
            self.client_project_id, self.account_id, operation_id, captured
        )
        binding = self.state.get("binding") if self.state else None
        self.state = {
            "operation_id": operation_id,
            "phase": "upload" if binding else "link",
            "layers": owned,
            "context": copy.deepcopy(context or {}),
            "binding": binding,
        }
        if not binding:
            self.state["link_intent"] = {
                "request_id": str(uuid4()),
                "client_project_id": self.client_project_id,
                "title": (
                    project.title() or Path(project.fileName()).stem or "QGIS project"
                )[:500],
            }
        self._save()
        self._advance()

    def restore(self, project):
        """Replay the exact journaled mutation, or poll an acknowledged snapshot."""
        self.pause()
        self._bind(project)
        if self.state:
            if self.state["phase"] == "blocked":
                self.relink_required.emit(
                    "Linked project is inaccessible. Restore access or explicitly detach and create a new link."
                )
            else:
                self._advance()
        return copy.deepcopy(self.state)

    def retry_link(self, project):
        """Retry the existing frozen operation after access has been restored."""
        self._bind(project)
        if self.state and self.state["phase"] == "blocked":
            self.state["phase"] = self.state.pop("blocked_phase")
            self._save()
            self._advance()

    def detach(self, project):
        """Explicitly start a separate local association without deleting prior recovery state."""
        self.pause()
        self._watch_persistence(project)
        replace_project_identity(project)
        self.project = None
        self.state = None
        self.datasets = {}

    def pause(self):
        """Stop local transport while preserving uncertain server operation identity."""
        self._generation += 1
        self.timer.stop()
        if self._handle is not None:
            self._handle.abort()
            self._handle = None
        self.uploader.abort()
        self._callback = None

    def close(self):
        """Stop local work on unload, never sending server cancellation."""
        self.pause()
        self._unwatch_persistence()
        self._closed = True

    def _account_changed(self, previous, current):
        self.pause()
        self._unwatch_persistence()
        self.account_id = None
        self.client_project_id = None
        self.state = None
        self.datasets = {}
        self.project = None

    def _save(self):
        self.journal.update_sections(
            self.client_project_id,
            self.account_id,
            sync=self.state,
            datasets=self.datasets,
        )

    def _send(self, method, path, body, callback):
        generation = self._generation
        account_id = self.account_id
        self._save()

        def finished(response):
            if (
                generation != self._generation
                or account_id != self.session.account_id
                or self._closed
            ):
                return
            self._handle = None
            if not response.ok:
                if response.status in {403, 404}:
                    self.state["blocked_phase"] = self.state["phase"]
                    self.state["phase"] = "blocked"
                    try:
                        self._save()
                    except (ValueError, OSError) as error:
                        self.failed.emit(str(error))
                        return
                    self.relink_required.emit(response.message)
                else:
                    self.failed.emit(
                        f"{response.message}. Resume synchronization to reconcile the saved operation."
                    )
                return
            try:
                callback(response.data)
            except (ValueError, KeyError, TypeError, OSError) as error:
                self.failed.emit(f"Cannot synchronize: {error}")

        self._handle = self.api.send(method, path, body, finished)

    def _advance(self):
        try:
            self._drive()
        except (ValueError, KeyError, TypeError, OSError) as error:
            self.failed.emit(f"Cannot synchronize: {error}")

    def _drive(self):
        if self._closed or not self.state or self.account_id != self.session.account_id:
            return
        phase = self.state["phase"]
        self.status.emit(
            {
                "link": "Linking project…",
                "upload": "Uploading immutable inputs…",
                "snapshot": "Registering snapshot…",
                "poll": "Preparing server inputs…",
                "ready": "Snapshot ready",
                "failed": "Input preparation failed",
                "blocked": "Project link needs attention",
            }[phase]
        )
        if phase == "link":
            self._send(
                "POST",
                "/integrations/qgis/projects",
                self.state["link_intent"],
                self._linked,
            )
        elif phase == "upload":
            self._upload_next()
        elif phase == "snapshot":
            self._send(
                "POST",
                f"/integrations/qgis/projects/{self.state['binding']['project_id']}/snapshots",
                self.state["snapshot_intent"],
                self._snapshot,
            )
        elif phase == "poll":
            self._send(
                "GET",
                f"/integrations/qgis/snapshots/{self.state['snapshot']['id']}",
                None,
                self._snapshot,
            )
        elif phase == "ready":
            self.ready.emit(copy.deepcopy(self.state["snapshot"]))
            callback, self._callback = self._callback, None
            if callback:
                callback(copy.deepcopy(self.state["snapshot"]))
        elif phase == "failed":
            self.failed.emit(self.state["error"])

    def _linked(self, data):
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("project_id"), int)
            or data["project_id"] <= 0
        ):
            raise ValueError("Invalid linked project response")
        self.state["binding"] = {
            key: data[key] for key in ("project_id", "link_id", "project_url")
        }
        self.state["phase"] = "upload"
        self._save()
        self._advance()

    def _upload_next(self):
        if "manifest" not in self.state:
            references = _output_references(
                self.state["layers"],
                self.account_id,
                self.client_project_id,
                self.state["binding"]["project_id"],
            )
            manifest = []
            for item in self.state["layers"]:
                key = item["client_layer_id"]
                if key in references:
                    if references[key] is not None:
                        manifest.append(references[key])
                    continue
                wire = {key: copy.deepcopy(item[key]) for key in WIRE_KEYS}
                previous = self.datasets.get(key)
                if (
                    previous
                    and previous["content_hash"] == item["content_hash"]
                    and previous["field_schema"] == item["field_schema"]
                ):
                    wire.update(
                        dataset_id=previous["dataset_id"],
                        dataset_version=previous["version"],
                    )
                manifest.append(wire)
            self.state["manifest"] = manifest
            self._save()
        for wire in self.state["manifest"]:
            if "dataset_id" in wire or "file_path" in wire:
                continue
            item = next(
                item
                for item in self.state["layers"]
                if item["client_layer_id"] == wire["client_layer_id"]
            )
            item["upload_intent"] = {
                "request_id": str(uuid4()),
                "body": {
                    "project_id": self.state["binding"]["project_id"],
                    "filename": "layer.geojson",
                    "size_bytes": item["size_bytes"],
                    "content_type": "application/geo+json",
                },
            }
            self._send(
                "POST",
                "/project/datasets/upload-url",
                item["upload_intent"]["body"],
                lambda data: self._upload_issued(item, wire, data),
            )
            return
        self.state["snapshot_intent"] = {
            "request_id": str(uuid4()),
            "layers": copy.deepcopy(self.state["manifest"]),
        }
        self.state["phase"] = "snapshot"
        self._save()
        self._advance()

    def _upload_issued(self, item, wire, issued):
        if not isinstance(issued, dict) or not isinstance(issued.get("file_path"), str):
            raise ValueError("Invalid upload authorization response")
        project_id = self.state["binding"]["project_id"]
        path = issued.get("file_path", "")
        pieces = path.split("/")
        if (
            len(pieces) != 2
            or pieces[0] != str(project_id)
            or pieces[1] in {"", ".", ".."}
            or any(character in path for character in ("\\", "%", "?", "#"))
        ):
            raise ValueError("Invalid issued upload object path")
        item["upload_path"] = path
        self._save()
        generation = self._generation
        account_id = self.account_id

        def completed(error):
            if (
                generation != self._generation
                or account_id != self.session.account_id
                or self._closed
            ):
                return
            if error:
                self.failed.emit(
                    f"{item['name']}: {error}. Resume to start a new upload path."
                )
                return
            wire["file_path"] = path
            item["uploaded"] = True
            try:
                self._save()
                self._advance()
            except (ValueError, OSError) as failure:
                self.failed.emit(str(failure))

        def before_request(method, offset, length):
            if generation != self._generation or account_id != self.session.account_id:
                raise ValueError("Upload account changed")
            item["transport_intent"] = {
                "request_id": str(uuid4()),
                "method": method,
                "offset": offset,
                "length": length,
            }
            self._save()

        self.uploader.start(
            item["local_path"],
            item["content_hash"],
            issued,
            completed,
            before_request=before_request,
        )

    def _snapshot(self, data):
        if (
            not isinstance(data, dict)
            or data.get("project_id") != self.state["binding"]["project_id"]
            or not isinstance(data.get("state"), str)
            or data.get("state") not in {"ready", "preparing", "failed"}
            or not isinstance(data.get("id"), str)
        ):
            raise ValueError("Invalid snapshot response")
        self.state["snapshot"] = data
        self.state["phase"] = {
            "ready": "ready",
            "preparing": "poll",
            "failed": "failed",
        }[data["state"]]
        if data["state"] == "ready":
            for layer in data["layers"]:
                self.datasets[layer["client_layer_id"]] = {
                    key: copy.deepcopy(layer[key])
                    for key in ("dataset_id", "version", "content_hash", "field_schema")
                }
        elif data["state"] == "failed":
            names = {
                item["client_layer_id"]: item["name"]
                for item in self.state.get("manifest", [])
            }
            self.state["error"] = (
                "; ".join(
                    f"{names.get(error['client_layer_id'], error['client_layer_id'])}: {error['message']}"
                    for error in data["errors"]
                )
                or "Input preparation failed"
            )
        self._save()
        if data["state"] == "preparing":
            self.timer.start(self.poll_ms)
        else:
            self._advance()
