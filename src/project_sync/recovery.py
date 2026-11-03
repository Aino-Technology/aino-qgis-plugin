import hashlib
import json
import os
import tempfile
from pathlib import Path
from uuid import UUID, uuid4

from qgis.PyQt.QtCore import QStandardPaths


PROJECT_SCOPE = "aino"
PROJECT_KEY = "client_project_id"
PROVENANCE_KEY = "aino/output_provenance"


def project_identity(project, create=True):
    """Return a project UUID preserved by Save As without saving the project."""
    value, _ = project.readEntry(PROJECT_SCOPE, PROJECT_KEY, "")
    if value:
        return str(UUID(value))
    if not create:
        return None
    value = str(uuid4())
    project.writeEntry(PROJECT_SCOPE, PROJECT_KEY, value)
    return value


def _check_secrets(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {
                "password",
                "token",
                "access_token",
                "upload_token",
                "authorization",
                "location",
                "upload_url",
            }:
                raise ValueError("Credentials and transport URLs cannot be journaled")
            _check_secrets(item)
    elif isinstance(value, list):
        for item in value:
            _check_secrets(item)


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
