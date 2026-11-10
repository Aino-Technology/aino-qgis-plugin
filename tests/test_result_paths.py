import copy
import hashlib
import os
from pathlib import Path, PureWindowsPath
from uuid import uuid4

from qgis.PyQt import sip
from qgis.core import QgsProject

from src.project_sync.recovery import RecoveryJournal, project_identity
from src.project_sync.styles import canonical_bytes
from src.results.controller import ResultController
from src.results.download import ResultDownloader
from src.results.storage import build_storage
from src.session import Session
from tests.test_analysis_controller import Api
from tests.test_api_client import token
from tests.test_result_import import GEOMETRIES, document, result
from tests.test_style_conversion import reference


WINDOWS_RECOVERY = PureWindowsPath(
    r"C:\Users\mml11\AppData\Roaming\QGIS\QGIS4\aino\recovery"
)


def windows_path(root, path):
    return str(WINDOWS_RECOVERY.joinpath(*Path(path).relative_to(root).parts))


def test_import_under_windows_path_limit(
    client, local_api, wait_for, tmp_path, monkeypatch
):
    session, api = client
    session.set_access_token(token())
    project = QgsProject()
    identity = project_identity(project)
    journal = RecoveryJournal(tmp_path / "recovery")
    controller = ResultController(api, journal, project)
    manifest = {"task_id": str(uuid4()), "layers": [result()]}
    analysis = {"results": manifest, "latest": {"status": "completed"}}
    journal.update_sections(
        identity,
        session.account_id,
        analysis=analysis,
        imports={
            "tasks": {
                manifest["task_id"]: {
                    "manifest_hash": hashlib.sha256(
                        canonical_bytes(manifest)
                    ).hexdigest(),
                    "project_id": 17,
                    "outputs": [],
                    "layer_ids": [],
                    "status": "failed",
                }
            }
        },
    )
    completed, failures, replacements = [], [], []
    controller.completed.connect(completed.append)
    controller.failed.connect(failures.append)
    original_replace = os.replace

    def replace(source, target):
        if not all(Path(p).is_relative_to(journal.directory) for p in (source, target)):
            return original_replace(source, target)
        paths = [windows_path(journal.directory, p) for p in (source, target)]
        replacements.extend(paths)
        if any(len(path.encode("utf-16-le")) // 2 >= 260 for path in paths):
            raise FileNotFoundError(
                3, "The system cannot find the path specified", str(target)
            )
        return original_replace(source, target)

    monkeypatch.setattr(os, "replace", replace)
    try:
        controller.start(manifest, 17, reference())
        wait_for(lambda: len(local_api.requests) == 1)
        local_api.respond(0, data={"url": local_api.origin + "/signed"})
        wait_for(lambda: len(local_api.requests) == 2)
        local_api.respond(1, data=document(GEOMETRIES[0]))
        wait_for(lambda: completed or failures, timeout=10000)
        assert not failures, failures
        assert completed and len(completed[0]) == 1
        assert project.mapLayer(completed[0][0]).isValid()
        paths = [
            windows_path(journal.directory, p) for p in journal.directory.rglob("*")
        ]
        assert replacements and all(
            len(p.encode("utf-16-le")) // 2 < 260 for p in paths
        )
        assert all(request["method"] == "GET" for request in local_api.requests)
        assert journal.load(identity, session.account_id)["analysis"] == analysis
        assert controller.state["status"] == "complete"
    finally:
        controller.close()
        project.clear()
        sip.delete(controller)
        sip.delete(project)


def test_result_directories_are_stable_and_scoped(qgis_app, tmp_path):
    project_id, task_id = str(uuid4()), str(uuid4())
    scopes = [
        ("account-a", project_id, task_id),
        ("account-b", project_id, task_id),
        ("account-a", str(uuid4()), task_id),
        ("account-a", project_id, str(uuid4())),
        ("account-a", project_id, task_id),
    ]
    directories = []
    for account, identity, task in scopes:
        session = Session()
        session.set_access_token(token(account))
        api = Api(session)
        downloader = ResultDownloader(api)
        layer = result()
        layer["client_project_id"] = identity
        try:
            downloader.download_results(
                task, {"layers": [layer]}, str(tmp_path), lambda *_: None
            )
            directories.append(downloader.directory)
        finally:
            downloader.close()
            sip.delete(downloader)
            sip.delete(session)
    assert directories[0] == directories[-1]
    assert len(set(directories[:4])) == 4


def test_existing_saved_result_paths_are_reused(client, local_api, wait_for, tmp_path):
    session, api = client
    session.set_access_token(token())
    project = QgsProject()
    identity = project_identity(project)
    journal = RecoveryJournal(tmp_path / "recovery")
    task = str(uuid4())
    layer = result()
    manifest = {"task_id": task, "layers": [layer]}
    scoped = copy.deepcopy(layer)
    scoped.update(
        account_id=session.account_id,
        client_project_id=identity,
        project_id=17,
        task_id=task,
    )
    legacy = (
        journal.scope(identity, session.account_id)
        / "results"
        / hashlib.sha256(task.encode()).hexdigest()
    )
    legacy.mkdir(parents=True)
    raw = legacy / ("a" * 64 + ".geojson")
    raw.write_bytes(canonical_bytes(document(GEOMETRIES[0])))
    storage = build_storage(str(raw), scoped)
    saved = {"result": scoped, "local_path": str(raw), "storage": storage}
    journal.update_sections(
        identity,
        session.account_id,
        imports={
            "tasks": {
                task: {
                    "manifest_hash": hashlib.sha256(
                        canonical_bytes(manifest)
                    ).hexdigest(),
                    "project_id": 17,
                    "outputs": [saved],
                    "layer_ids": [],
                    "status": "importing",
                }
            }
        },
    )
    controller = ResultController(api, journal, project)
    completed, failures = [], []
    controller.completed.connect(completed.append)
    controller.failed.connect(failures.append)
    try:
        controller.start(manifest, 17, reference())
        wait_for(lambda: completed or failures, timeout=10000)
        assert not failures and completed
        assert not local_api.requests
        assert controller.state["outputs"][0]["local_path"] == str(raw)
        assert all(Path(part["path"]).is_file() for part in storage)
        assert project.mapLayer(completed[0][0]).source().startswith(storage[0]["path"])
    finally:
        controller.close()
        project.clear()
        sip.delete(controller)
        sip.delete(project)
