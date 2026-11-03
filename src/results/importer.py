import hashlib
import json
import os
import tempfile
from pathlib import Path

from qgis import utils
from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.core import QgsEditorWidgetSetup, QgsProject, QgsVectorLayer

from src.project_sync.area import require_gui_thread
from src.project_sync.layers import SnapshotCapture, capture_layers
from src.project_sync.managed import MANAGED_KEY, decode, watch_managed_output
from src.project_sync.recovery import PROVENANCE_KEY
from src.project_sync.styles import RenderReference, canonical_bytes

from .storage import build_storage
from .styles import apply_style


IDENTITY_KEYS = (
    "account_id",
    "client_project_id",
    "project_id",
    "task_id",
    "dataset_id",
    "version",
    "content_hash",
)


def existing_parts(project, result):
    found = {}
    for layer in project.mapLayers().values():
        try:
            provenance = json.loads(layer.customProperty(PROVENANCE_KEY, "null"))
        except (ValueError, TypeError):
            continue
        if isinstance(provenance, dict) and all(
            provenance.get(key) == result[key] for key in IDENTITY_KEYS
        ):
            if layer.isValid():
                found[provenance["part_id"]] = layer.id()
    return found


def _open(project, result, storage, reference):
    staging = QgsProject()
    found = existing_parts(project, result)
    layers = []
    versioned = any(
        layer.customProperty("aino/dataset_id", None) == result["dataset_id"]
        and layer.customProperty("aino/version", None) != result["version"]
        for layer in project.mapLayers().values()
    )
    try:
        for part in storage:
            if part["part_id"] in found:
                continue
            name = result["name"] + (f" (v{result['version']})" if versioned else "")
            layer = QgsVectorLayer(part["path"], name, "ogr")
            if not layer.isValid():
                raise ValueError("Persistent output did not reopen with all features")
            layer.setCustomProperty(
                MANAGED_KEY, canonical_bytes(part["mapping"]).decode()
            )
            layer.setCustomProperty("aino/task_id", result["task_id"])
            layer.setCustomProperty("aino/dataset_id", result["dataset_id"])
            layer.setCustomProperty("aino/version", result["version"])
            layer.setEditorWidgetSetup(
                layer.fields().indexFromName(part["mapping"]["feature_metadata"]),
                QgsEditorWidgetSetup("Hidden", {}),
            )
            for name in part["mapping"].get("internal_fields", []):
                layer.setEditorWidgetSetup(
                    layer.fields().indexFromName(name),
                    QgsEditorWidgetSetup("Hidden", {}),
                )
            for entry in part["mapping"]["fields"]:
                layer.setFieldAlias(
                    layer.fields().indexFromName(entry["physical"]),
                    entry["schema"]["name"],
                )
                if entry["codec"] == "json":
                    layer.setEditorWidgetSetup(
                        layer.fields().indexFromName(entry["physical"]),
                        QgsEditorWidgetSetup("TextEdit", {"IsMultiline": True}),
                    )
                    layer.setFieldAlias(
                        layer.fields().indexFromName(entry["physical"]),
                        entry["schema"]["name"] + " (JSON)",
                    )
            part["class_field"] = apply_style(
                layer, result["style"], part["mapping"], reference
            )
            part["mapping"]["server_class_field"] = part["class_field"]
            layer.setCustomProperty(
                MANAGED_KEY, canonical_bytes(part["mapping"]).decode()
            )
            watch_managed_output(layer)
            staging.addMapLayer(layer)
            layers.append((layer, part))
        return staging, layers, found
    except Exception:
        staging.clear()
        raise


def _provenance(result, part, part_ids, baseline):
    provenance = {key: result[key] for key in IDENTITY_KEYS}
    provenance.update(
        field_schema=result["field_schema"],
        part_id=part["part_id"],
        part_ids=part_ids,
        local_content_hash=baseline["content_hash"],
        local_style_hash=baseline["style_hash"],
        server_style=result["style"],
        server_style_hash=hashlib.sha256(canonical_bytes(result["style"])).hexdigest(),
    )
    metadata = baseline["style"].get("qgis", {}).get("classification", {})
    field = part.get("class_field")
    if field and metadata.get("field"):
        classes = {"unrendered": []}
        for class_id, fields in baseline.get("managed_class_values", {}).items():
            classes[class_id] = fields.get(field, [])
        entry = next(
            entry
            for entry in part["mapping"]["fields"]
            if entry["schema"]["name"] == field
        )
        if (
            metadata.get("kind") == "categorized"
            and metadata.get("source_field") == field
        ):
            for definition in metadata["definitions"]:
                raw = definition["value"]["value"]
                if raw == "":
                    classes.setdefault(definition["id"], [])
                    continue
                value = decode(raw, entry)
                classes.setdefault(definition["id"], [value])
        if metadata.get("kind") == "graduated":
            original = result["style"].get("qgis", {}).get("classification", {})
            by_index = {
                definition["index"]: definition["id"]
                for definition in original.get("definitions", [])
            }
            for definition in metadata["definitions"]:
                if definition["index"] in by_index:
                    classes.setdefault(
                        definition["id"], [by_index[definition["index"]]]
                    )
        if all(
            all(not isinstance(value, (dict, list)) for value in values)
            for values in classes.values()
        ):
            provenance["class_mapping"] = dict(
                dataset_id=result["dataset_id"],
                version=result["version"],
                content_hash=result["content_hash"],
                local_field=metadata["field"],
                server_field=field,
                classes=classes,
            )
    return provenance


def _restore_authoritative_classes(layers, result, baselines, reference):
    by_id = {baseline["client_layer_id"]: baseline for baseline in baselines}
    original = result["style"].get("qgis", {}).get("classification", {})
    by_index = {
        definition["index"]: definition["id"]
        for definition in original.get("definitions", [])
    }
    changed = False
    for layer, part in layers:
        baseline = by_id[layer.id()]
        classification = baseline["style"].get("qgis", {}).get("classification", {})
        if classification.get("kind") != "graduated":
            continue
        actual = baseline.get("managed_class_values", {})
        expected = {
            definition["id"]: by_index.get(definition["index"])
            for definition in classification["definitions"]
        }
        expected["unrendered"] = "unrendered"
        if any(
            any(
                value != expected.get(class_id)
                for value in fields.get(part["class_field"], [])
            )
            for class_id, fields in actual.items()
        ):
            apply_style(
                layer,
                result["style"],
                part["mapping"],
                reference,
                prefer_graduated=False,
            )
            part["warnings"] = [
                "Stored range metadata no longer matches output values; categories preserve the output style."
            ]
            changed = True
    return changed


def _register(project, staging, layers, found, result, storage, baselines):
    by_id = {baseline["client_layer_id"]: baseline for baseline in baselines}
    part_ids = [part["part_id"] for part in storage]
    for layer, part in layers:
        provenance_path = Path(part["path"]).with_suffix(".provenance.json")
        if provenance_path.exists():
            provenance = json.loads(provenance_path.read_text())
            if any(provenance.get(key) != result[key] for key in IDENTITY_KEYS):
                raise ValueError("Persistent output identity changed")
        else:
            provenance = _provenance(result, part, part_ids, by_id[layer.id()])
            descriptor, temporary = tempfile.mkstemp(
                prefix=".provenance-", dir=provenance_path.parent
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(canonical_bytes(provenance))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, provenance_path)
                directory_fd = os.open(provenance_path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                Path(temporary).unlink(missing_ok=True)
        layer.setCustomProperty(PROVENANCE_KEY, canonical_bytes(provenance).decode())
        project.addMapLayer(staging.takeMapLayer(layer))
        found[part["part_id"]] = layer.id()
    return [found[part_id] for part_id in part_ids]


def import_result(
    project, result: dict, local_path: str, *, reference=None
) -> list[str]:
    """Import persistent output on the Qt thread; interactive callers use ImportJob."""
    require_gui_thread()
    if reference is None:
        if utils.iface is None:
            raise ValueError("Result import requires a canvas RenderReference")
        reference = RenderReference(utils.iface.mapCanvas().mapSettings())
    storage = build_storage(local_path, result)
    staging, layers, found = _open(project, result, storage, reference)
    try:
        with tempfile.TemporaryDirectory(prefix="aino-baseline-") as directory:
            baselines = capture_layers(
                staging,
                [layer.id() for layer, _ in layers],
                directory,
                reference=reference,
            )
            if _restore_authoritative_classes(layers, result, baselines, reference):
                baselines = capture_layers(
                    staging,
                    [layer.id() for layer, _ in layers],
                    directory,
                    reference=reference,
                )
            return _register(
                project, staging, layers, found, result, storage, baselines
            )
    finally:
        staging.clear()


class ImportJob(QObject):
    finished = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, project, result, storage, reference, parent=None, valid=None):
        super().__init__(parent)
        self.project, self.result, self.storage = project, result, storage
        self.reference = reference
        self.valid = valid or (lambda: True)
        self.staging, self.layers, self.found = _open(
            project, result, storage, reference
        )
        self.directory = tempfile.TemporaryDirectory(prefix="aino-baseline-")
        self.capture = SnapshotCapture(
            self.staging,
            [layer.id() for layer, _ in self.layers],
            self.directory.name,
            reference=reference,
            parent=self,
        )
        self.capture.finished.connect(self._ready)
        self.capture.failed.connect(self._failed)

    def start(self):
        self.capture.start()

    def _ready(self, baselines):
        if not self.valid():
            self.cancel()
            return
        try:
            if _restore_authoritative_classes(
                self.layers, self.result, baselines, self.reference
            ):
                self.capture.deleteLater()
                self.capture = SnapshotCapture(
                    self.staging,
                    [layer.id() for layer, _ in self.layers],
                    self.directory.name,
                    reference=self.reference,
                    parent=self,
                )
                self.capture.finished.connect(self._ready)
                self.capture.failed.connect(self._failed)
                self.capture.start()
                return
            ids = _register(
                self.project,
                self.staging,
                self.layers,
                self.found,
                self.result,
                self.storage,
                baselines,
            )
        except Exception as error:
            self._failed(str(error))
            return
        self.cancel()
        self.finished.emit(ids)

    def _failed(self, message):
        self.cancel()
        self.failed.emit(message)

    def cancel(self):
        self.capture.cancel()
        self.staging.clear()
        self.directory.cleanup()
