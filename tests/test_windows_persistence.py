import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from qgis.core import QgsProject

from src import filesystem
from src.project_sync.recovery import RecoveryJournal
from src.results.download import _StorageWorker
from src.results.importer import import_result
from tests.test_request_controller import ready, request_setup as request_setup
from tests.test_result_import import GEOMETRIES, document, result, write
from tests.test_style_conversion import reference
from tests.test_sync_controller import captured


@pytest.fixture
def windows_file_io(monkeypatch):
    original_open, original_fsync = os.open, os.fsync
    original_path_open = Path.open
    readonly = set()

    def path_open(path, mode="r", *args, **kwargs):
        stream = original_path_open(path, mode, *args, **kwargs)
        readonly.discard(stream.fileno())
        if not any(flag in mode for flag in "+wax"):
            readonly.add(stream.fileno())
        return stream

    def open_file(path, flags, *args, **kwargs):
        if flags == os.O_RDONLY and Path(path).is_dir():
            raise PermissionError(13, "Permission denied", str(path))
        descriptor = original_open(path, flags, *args, **kwargs)
        readonly.discard(descriptor)
        return descriptor

    def flush_file(descriptor):
        if descriptor in readonly:
            raise PermissionError(13, "Flush requires write access")
        return original_fsync(descriptor)

    monkeypatch.setattr(Path, "open", path_open)
    monkeypatch.setattr(filesystem, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(os, "open", open_file)
    monkeypatch.setattr(os, "fsync", flush_file)


def test_windows_journal_save_update_and_replace(tmp_path, windows_file_io):
    journal, identity = RecoveryJournal(tmp_path), str(uuid4())
    journal.save(identity, "account", {"request": {"phase": "capturing"}})
    journal.update_sections(identity, "account", sync={"phase": "link"})
    assert journal.load(identity, "account") == {
        "request": {"phase": "capturing"},
        "sync": {"phase": "link"},
    }
    assert not list(tmp_path.rglob(".journal-*"))


def test_windows_capture_adoption(tmp_path, windows_file_io):
    journal, identity = RecoveryJournal(tmp_path / "journal"), str(uuid4())
    layer = captured(tmp_path)
    owned = journal.adopt(identity, "account", str(uuid4()), [layer])
    assert not Path(layer["local_path"]).exists()
    assert Path(owned[0]["local_path"]).read_bytes() == b"one"
    assert json.loads(Path(owned[0]["schema_path"]).read_text()) == []


def test_windows_download_storage_worker(qgis_app, tmp_path, windows_file_io):
    manifest = result()
    temporary = write(tmp_path, document(GEOMETRIES[0]))
    target = str(tmp_path / "download.geojson")
    worker = _StorageWorker(temporary, target, manifest)
    completed = []
    worker.completed.connect(lambda output, error: completed.append((output, error)))
    worker.run()
    assert completed and completed[0][1] == "", completed
    assert completed[0][0]["local_path"] == target
    assert Path(completed[0][0]["storage"][0]["path"]).is_file()


def test_windows_import_persists_provenance(qgis_app, tmp_path, windows_file_io):
    project = QgsProject()
    manifest = result()
    path = write(tmp_path, document(GEOMETRIES[0]))
    try:
        ids = import_result(project, manifest, path, reference=reference())
        assert len(ids) == 1 and project.mapLayer(ids[0]).isValid()
        assert import_result(project, manifest, path, reference=reference()) == ids
        assert list(tmp_path.rglob("*.provenance.json"))
    finally:
        project.clear()


def test_windows_one_go_reaches_analysis(request, windows_file_io):
    controller, capture, api, journal, project = request.getfixturevalue(
        "request_setup"
    )
    controller.start("Parks in Berlin", None, [])
    capture.calls[0][2]([])
    snapshot = ready(api)
    assert api.calls[-1][1] == "/integrations/qgis/analyses"
    assert api.calls[-1][2]["snapshot_id"] == snapshot["id"]


@pytest.mark.parametrize("failure", [None, "open", "fsync"])
def test_posix_directory_sync_preserves_errors_and_closes(
    monkeypatch, tmp_path, failure
):
    calls = []

    def open_directory(path, flags):
        calls.append(("open", path, flags))
        if failure == "open":
            raise PermissionError("directory denied")
        return 42

    def flush_directory(descriptor):
        calls.append(("fsync", descriptor))
        if failure == "fsync":
            raise OSError("disk failure")

    monkeypatch.setattr(filesystem, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(
        filesystem,
        "os",
        SimpleNamespace(
            O_RDONLY=os.O_RDONLY,
            open=open_directory,
            fsync=flush_directory,
            close=lambda descriptor: calls.append(("close", descriptor)),
        ),
    )
    if failure:
        with pytest.raises(OSError):
            filesystem.sync_directory(tmp_path)
    else:
        filesystem.sync_directory(tmp_path)
    assert calls[0] == ("open", tmp_path, os.O_RDONLY)
    if failure == "open":
        assert len(calls) == 1
    else:
        assert calls[1:] == [("fsync", 42), ("close", 42)]
