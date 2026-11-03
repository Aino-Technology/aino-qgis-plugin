import hashlib
import json
import os
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from uuid import UUID, uuid4

from qgis.PyQt.QtCore import QObject, QStandardPaths, QTimer, pyqtSignal
from qgis.core import QgsProject


PROJECT_SCOPE = "aino"
PROJECT_KEY = "client_project_id"
PROVENANCE_KEY = "aino/output_provenance"


def _current_identity(project):
    if not isinstance(project, QgsProject):
        return None
    value, _ = project.readEntry(PROJECT_SCOPE, PROJECT_KEY, "")
    return str(UUID(value)) if value else None


def _xml_identity(stream):
    path = []
    for event, element in ET.iterparse(stream, events=("start", "end")):
        if event == "start":
            path.append(element.get("name", element.tag))
        else:
            if path == ["qgis", "properties", PROJECT_SCOPE, PROJECT_KEY]:
                return str(UUID(element.text or ""))
            path.pop()
            element.clear()
    return None


def _stored_identity(project):
    if not isinstance(project, QgsProject):
        return None
    filename = project.fileName()
    if not filename or "://" in filename or project.projectStorage() is not None:
        return None
    path = Path(filename)
    try:
        if path.suffix.lower() == ".qgz":
            with zipfile.ZipFile(path) as archive:
                names = [
                    name for name in archive.namelist() if name.lower().endswith(".qgs")
                ]
                if len(names) != 1:
                    return None
                with archive.open(names[0]) as stream:
                    return _xml_identity(stream)
        if path.suffix.lower() == ".qgs":
            with path.open("rb") as stream:
                return _xml_identity(stream)
    except (OSError, ValueError, RuntimeError, ET.ParseError, zipfile.BadZipFile):
        return None
    return None


class ProjectIdentityPersistence(QObject):
    """Track the saved UUID as a project-owned object, independently of unrelated edits."""

    changed = pyqtSignal()

    def __init__(self, project):
        super().__init__(project)
        self.setObjectName("aino-recovery-identity")
        self.saved_identity = _stored_identity(project)
        self._save_refresh = QTimer(self)
        self._save_refresh.setSingleShot(True)
        self._save_refresh.timeout.connect(self._refresh)
        project.projectSaved.connect(self._saved)
        project.readProject.connect(self._refresh)
        project.fileNameChanged.connect(self._refresh)
        project.cleared.connect(self._cleared)

    @property
    def notice(self):
        try:
            identity = _current_identity(self.parent())
        except ValueError:
            return "The current Aino association UUID is invalid; synchronization cannot recover this link."
        if not identity or identity == self.saved_identity:
            return ""
        return (
            "Save the QGIS project yourself to persist its current Aino link and recover it after restarting QGIS. "
            "This association UUID is not confirmed in the saved project. It has not been saved automatically."
        )

    def _saved(self):
        self._save_refresh.start(0)

    def _refresh(self, *args):
        self.saved_identity = _stored_identity(self.parent())
        self.changed.emit()

    def _cleared(self):
        self._save_refresh.stop()
        self.saved_identity = None
        self.changed.emit()


def project_persistence(project):
    """Return the one lifecycle-bound persistence tracker owned by this QgsProject."""
    tracker = project.findChild(ProjectIdentityPersistence, "aino-recovery-identity")
    return tracker if tracker is not None else ProjectIdentityPersistence(project)


def replace_project_identity(project):
    """Replace only the in-memory association and disclose the need for an explicit save."""
    tracker = project_persistence(project)
    identity = str(uuid4())
    project.writeEntry(PROJECT_SCOPE, PROJECT_KEY, identity)
    tracker.changed.emit()
    return identity


def project_identity(project, create=True):
    """Return a project UUID preserved by Save As without saving the project."""
    project_persistence(project)
    value = _current_identity(project)
    if value or not create:
        return value
    return replace_project_identity(project)


def _check_secrets(value, path=()):
    if path == ("analysis", "results", "report"):
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {
                "password",
                "token",
                "access_token",
                "api_key",
                "upload_token",
                "authorization",
                "location",
                "upload_url",
            }:
                raise ValueError("Credentials and transport URLs cannot be journaled")
            _check_secrets(item, (*path, key))
    elif isinstance(value, list):
        for item in value:
            _check_secrets(item, (*path, "[]"))


class RecoveryJournal:
    """Atomic account/project-scoped JSON with independently owned workflow sections."""

    def __init__(self, directory=None):
        self.directory = Path(
            directory
            or Path(
                QStandardPaths.writableLocation(
                    QStandardPaths.StandardLocation.AppDataLocation
                )
            )
            / "aino"
            / "recovery"
        )

    def scope(self, client_project_id, account_id):
        if not isinstance(account_id, str) or not account_id:
            raise ValueError("Sign in before accessing recovery state")
        return (
            self.directory
            / hashlib.sha256(account_id.encode()).hexdigest()
            / str(UUID(client_project_id))
        )

    def save(self, client_project_id: str, account_id: str, state: dict):
        """Durably replace one journal; failed replacement keeps its previous contents."""
        _check_secrets(state)
        root = self.scope(client_project_id, account_id)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = json.dumps(
            {
                "version": 1,
                "client_project_id": client_project_id,
                "account_id": account_id,
                "state": state,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
        descriptor, temporary = tempfile.mkstemp(prefix=".journal-", dir=root)
        try:
            with os.fdopen(descriptor, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, root / "journal.json")
            directory_fd = os.open(root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def load(self, client_project_id: str, account_id: str) -> dict | None:
        """Read only this account's UUID journal, rejecting mismatched or corrupt envelopes."""
        path = self.scope(client_project_id, account_id) / "journal.json"
        if not path.exists():
            return None
        document = json.loads(path.read_text())
        if (
            document.get("version") != 1
            or document.get("client_project_id") != client_project_id
            or document.get("account_id") != account_id
            or not isinstance(document.get("state"), dict)
        ):
            raise ValueError("Recovery journal is invalid; it was not overwritten")
        _check_secrets(document["state"])
        return document["state"]

    def update_sections(self, client_project_id, account_id, **sections):
        """Merge owned top-level sections, preserving analysis/import records from other controllers."""
        state = self.load(client_project_id, account_id) or {}
        state.update(sections)
        self.save(client_project_id, account_id, state)

    def adopt(self, client_project_id, account_id, operation_id, layers):
        """Move closed capture files into operation ownership before UI invalidation can delete them."""
        root = (
            self.scope(client_project_id, account_id)
            / "staging"
            / str(UUID(operation_id))
        )
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        adopted = []
        for index, layer in enumerate(layers):
            item = json.loads(json.dumps(layer))
            for key, extension in [
                ("local_path", ".geojson"),
                ("schema_path", ".fields.json"),
            ]:
                source = Path(item[key])
                if source.is_symlink() or not source.is_file():
                    raise ValueError(
                        "Captured file is unavailable or is a symbolic link"
                    )
                target = root / f"{index}{extension}"
                try:
                    os.replace(source, target)
                except OSError as error:
                    raise OSError(
                        "Cannot transfer capture ownership; prepare inputs on the recovery filesystem"
                    ) from error
                item[key] = str(target)
            item["size_bytes"] = Path(item["local_path"]).stat().st_size
            adopted.append(item)
        directory_fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return adopted
