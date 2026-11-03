import json
from pathlib import Path
from uuid import uuid4

import pytest
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QCoreApplication, QEvent
from qgis.core import QgsProject

from src.api_client import ApiResponse
from src.project_sync.recovery import RecoveryJournal, project_identity
from src.project_sync.controller import SyncController
from src.session import Session
from tests.test_api_client import token


class FakeApi:
    def __init__(self, session):
        self.session = session
        self.calls = []

    def send(self, method, path, body, callback):
        handle = type("Handle", (), {"pending": True, "abort": lambda self: None})()
        self.calls.append((method, path, json.loads(json.dumps(body)), callback))
        return handle

    def answer(self, data, status=200, error=""):
        self.calls[-1][3](ApiResponse(status, data, error))


@pytest.fixture
def sync(qgis_app, tmp_path):
    session = Session()
    session.set_access_token(token())
    api = FakeApi(session)
    journal = RecoveryJournal(tmp_path)
    project = QgsProject()
    controller = SyncController(api, journal=journal)
    yield controller, api, journal, project
    controller.close()
    project.clear()
    api.calls.clear()
    controller.deleteLater()
    session.deleteLater()
    project.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_journal_atomic_account_scoped_and_no_credentials(tmp_path, monkeypatch):
    journal = RecoveryJournal(tmp_path)
    identity = str(uuid4())
    journal.save(identity, "../../account", {"sync": {"phase": "link"}})
    assert journal.load(identity, "other") is None
    assert journal.load(identity, "../../account")["sync"]["phase"] == "link"
    with pytest.raises(ValueError):
        journal.save(identity, "../../account", {"upload_token": "secret"})
    import os

    monkeypatch.setattr(
        os, "replace", lambda *args: (_ for _ in ()).throw(OSError("crash"))
    )
    with pytest.raises(OSError):
        journal.save(identity, "../../account", {"sync": {"phase": "snapshot"}})
    assert journal.load(identity, "../../account")["sync"]["phase"] == "link"
    assert len(list(tmp_path.rglob("*.json"))) == 1


def test_link_intent_durable_before_send_and_lost_reply_replayed(sync):
    controller, api, journal, project = sync
    controller.prepare(project, [], lambda snapshot: None)
    original = api.calls[-1][2]
    identity = project_identity(project)
    assert journal.load(identity, "account-a")["sync"]["link_intent"] == original
    controller.close()
    resumed = SyncController(api, journal=journal)
    resumed.restore(project)
    assert api.calls[-1][2] == original
    resumed.close()
    resumed.deleteLater()


def test_save_as_preserves_identity_and_unsaved_notice(sync, tmp_path):
    controller, api, journal, project = sync
    notices = []
    controller.notice.connect(notices.append)
    controller.prepare(project, [], lambda snapshot: None)
    identity = project_identity(project)
    assert notices and "save" in notices[0].lower()
    project.setFileName(str(tmp_path / "one.qgz"))
    assert project.write()
    project.setFileName(str(tmp_path / "two.qgz"))
    assert project.write()
    reloaded = QgsProject()
    assert reloaded.read(project.fileName())
    assert project_identity(reloaded) == identity
    reloaded.clear()
    sip.delete(reloaded)


def test_snapshot_intent_replayed_before_upload_replacement(sync):
    controller, api, journal, project = sync
    controller.prepare(project, [], lambda snapshot: None)
    api.answer(
        {
            "project_id": 17,
            "link_id": str(uuid4()),
            "project_url": "https://example.invalid/project/17",
        }
    )
    request = api.calls[-1][2]
    assert api.calls[-1][1] == "/integrations/qgis/projects/17/snapshots"
    controller.close()
    resumed = SyncController(api, journal=journal)
    resumed.restore(project)
    assert api.calls[-1][2] == request
    assert api.calls[-1][1] == "/integrations/qgis/projects/17/snapshots"
    resumed.close()
    resumed.deleteLater()


def test_access_denied_requires_explicit_detach(sync):
    controller, api, journal, project = sync
    issues = []
    controller.relink_required.connect(issues.append)
    controller.prepare(project, [], lambda snapshot: None)
    old = project_identity(project)
    api.answer({"message": "Gone"}, 404)
    assert issues
    count = len(api.calls)
    controller.restore(project)
    assert len(api.calls) == count
    controller.detach(project)
    assert project_identity(project) != old


class FakeUpload:
    def __init__(self):
        self.calls = []

    def start(self, path, digest, issued, callback, **kwargs):
        self.calls.append((path, digest, issued, callback))

    def abort(self):
        pass


def captured(tmp_path, key="one", data=b"one", style="red"):
    import hashlib

    directory = tmp_path / str(uuid4())
    directory.mkdir()
    path = directory / "source.geojson"
    schema = directory / "source.fields.json"
    path.write_bytes(data)
    schema.write_text("[]")
    return {
        "client_layer_id": key,
        "name": "Same name",
        "visible": True,
        "order": 0,
        "content_hash": hashlib.sha256(data).hexdigest(),
        "style_hash": hashlib.sha256(style.encode()).hexdigest(),
        "style": {"dataset_type": "default", "style": {"color": style}, "qgis": {}},
        "field_schema": [],
        "local_path": str(path),
        "schema_path": str(schema),
        "feature_count": 1,
        "warnings": [],
        "provenance": {},
    }


def linked(api):
    api.answer(
        {
            "project_id": 17,
            "link_id": str(uuid4()),
            "project_url": "https://example.invalid/project/17",
        }
    )


def complete_snapshot(api, state="ready"):
    body = api.calls[-1][2]
    layers = [
        {
            **layer,
            "dataset_id": layer.get("dataset_id", 101 + index),
            "version": layer.get("dataset_version", 2),
        }
        for index, layer in enumerate(body["layers"])
    ]
    api.answer(
        {
            "project_id": 17,
            "id": str(uuid4()),
            "state": state,
            "layers": layers,
            "errors": [],
        }
    )


def test_staging_survives_ui_cleanup_and_only_changed_layer_uploads(sync, tmp_path):
    controller, api, journal, project = sync
    upload = FakeUpload()
    controller.uploader = upload
    first = [captured(tmp_path, "a"), captured(tmp_path, "b", b"two")]
    controller.prepare(project, first, None, context={"area": {"kind": "extent"}})
    assert all(not Path(item["local_path"]).exists() for item in first)
    assert all(Path(item["local_path"]).exists() for item in controller.state["layers"])
    linked(api)
    for index in range(2):
        api.answer({"file_path": f"17/file{index}.geojson"})
        upload.calls[-1][3]("")
    complete_snapshot(api)
    assert controller.state["phase"] == "ready"
    old_dataset = controller.datasets["b"]["dataset_id"]
    next_inputs = [
        captured(tmp_path, "a", b"edited"),
        captured(tmp_path, "b", b"two", "blue"),
    ]
    next_inputs[1]["order"] = 1
    controller.prepare(project, next_inputs, None)
    assert api.calls[-1][1] == "/project/datasets/upload-url"
    api.answer({"file_path": "17/changed.geojson"})
    upload.calls[-1][3]("")
    manifest = api.calls[-1][2]["layers"]
    assert len(upload.calls) == 3
    assert manifest[1]["dataset_id"] == old_dataset
    assert "file_path" not in manifest[1]
    assert manifest[1]["style"]["style"]["color"] == "blue"
    assert all(
        "local_path" not in item and "provenance" not in item for item in manifest
    )


@pytest.mark.parametrize(
    "boundary",
    ["link", "mint", "tus_create", "tus_patch", "uploaded", "snapshot", "poll"],
)
def test_crash_boundaries_preserve_logical_operation(sync, tmp_path, boundary):
    controller, api, journal, project = sync
    upload = FakeUpload()
    controller.uploader = upload
    controller.prepare(project, [captured(tmp_path)], None)
    if boundary != "link":
        linked(api)
    if boundary in {"tus_create", "tus_patch", "uploaded", "snapshot", "poll"}:
        api.answer(
            {
                "file_path": "17/first.geojson",
                "upload_token": "NEVER-PERSIST",
                "upload_url": "https://storage.invalid",
                "Location": "OPAQUE-SECRET",
            }
        )
    if boundary in {"uploaded", "snapshot", "poll"}:
        if boundary == "uploaded":
            controller._advance = lambda: None
        upload.calls[-1][3]("")
    if boundary == "poll":
        complete_snapshot(api, "preparing")
    previous = json.loads(json.dumps(controller.state))
    frozen = previous.get("snapshot_intent")
    controller.close()
    resumed = SyncController(api, journal=journal, uploader=FakeUpload())
    resumed.restore(project)
    if boundary == "link":
        assert api.calls[-1][2] == previous["link_intent"]
    elif boundary in {"mint", "tus_create", "tus_patch"}:
        assert api.calls[-1][1] == "/project/datasets/upload-url"
        assert (
            resumed.state["layers"][0]["upload_intent"]["request_id"]
            != previous["layers"][0]["upload_intent"]["request_id"]
        )
        api.answer({"file_path": "17/new-path.geojson"})
        resumed.uploader.calls[-1][3]("")
        assert api.calls[-1][2]["layers"][0]["file_path"] == "17/new-path.geojson"
    elif boundary in {"uploaded", "snapshot"}:
        if frozen:
            assert api.calls[-1][2] == frozen
        assert api.calls[-1][2]["layers"][0]["file_path"] == "17/first.geojson"
    else:
        assert api.calls[-1][0] == "GET"
        assert api.calls[-1][1].endswith(previous["snapshot"]["id"])
    serialized = "\n".join(
        path.read_text() for path in journal.directory.rglob("journal.json")
    )
    assert "NEVER-PERSIST" not in serialized and "OPAQUE-SECRET" not in serialized
    resumed.close()
    resumed.deleteLater()


def test_account_change_drops_late_callback_and_isolates_journal(sync):
    controller, api, journal, project = sync
    controller.prepare(project, [], None)
    old = api.calls[-1]
    identity = project_identity(project)
    api.session.set_access_token(token("account-b"))
    assert controller.state is None
    old[3](ApiResponse(200, {"project_id": 17}))
    count = len(api.calls)
    controller.restore(project)
    assert len(api.calls) == count
    assert controller.state is None
    assert journal.load(identity, "account-b") is None
    api.session.set_access_token(token("account-a", 2))
    controller.restore(project)
    assert api.calls[-1][2] == old[2]


def test_failed_input_blocks_ready_with_layer_name(sync):
    controller, api, journal, project = sync
    ready, failures = [], []
    controller.ready.connect(ready.append)
    controller.failed.connect(failures.append)
    controller.prepare(project, [], None)
    linked(api)
    controller.state["manifest"] = [{"client_layer_id": "x", "name": "Broken roads"}]
    api.answer(
        {
            "project_id": 17,
            "id": str(uuid4()),
            "state": "failed",
            "layers": [],
            "errors": [{"client_layer_id": "x", "message": "Invalid geometry"}],
        }
    )
    assert not ready
    assert failures == ["Broken roads: Invalid geometry"]


def test_journal_sections_preserve_analysis_and_import_work(sync):
    controller, api, journal, project = sync
    controller.prepare(project, [], None)
    identity = project_identity(project)
    journal.update_sections(
        identity,
        "account-a",
        analysis={"request_id": "durable"},
        imports={"pending": [1]},
    )
    linked(api)
    saved = journal.load(identity, "account-a")
    assert saved["analysis"] == {"request_id": "durable"}
    assert saved["imports"] == {"pending": [1]}


def test_protected_output_reference_requires_all_unchanged_representable_parts(
    tmp_path,
):
    from src.project_sync.controller import _output_references

    client_id = str(uuid4())
    parts = [captured(tmp_path, "point"), captured(tmp_path, "line", b"line")]
    for index, part in enumerate(parts):
        part["order"] = index
        part["output_provenance"] = {
            "account_id": "account-a",
            "client_project_id": client_id,
            "project_id": 17,
            "dataset_id": 201,
            "version": 3,
            "content_hash": "a" * 64,
            "field_schema": [{"name": "server", "type": "string"}],
            "part_id": part["client_layer_id"],
            "part_ids": ["point", "line"],
            "local_content_hash": part["content_hash"],
            "local_style_hash": part["style_hash"],
            "server_style": {
                "dataset_type": "default",
                "style": {"color": "red"},
                "qgis": {},
            },
            "server_style_hash": "b" * 64,
        }
    refs = _output_references(parts, "account-a", client_id, 17)
    assert refs["point"]["client_layer_id"] == "output:201"
    assert refs["point"]["content_hash"] == "a" * 64
    assert refs["line"] is None
    assert not _output_references(parts[:1], "account-a", client_id, 17)
    assert not _output_references(parts, "account-b", client_id, 17)
    parts[1]["visible"] = False
    assert not _output_references(parts, "account-a", client_id, 17)
    parts[1]["visible"] = True
    parts[1]["style_hash"] = "c" * 64
    assert not _output_references(parts, "account-a", client_id, 17)
    parts[1]["style_hash"] = parts[1]["output_provenance"]["local_style_hash"]
    parts[1]["content_hash"] = "d" * 64
    assert not _output_references(parts, "account-a", client_id, 17)
    single = parts[0]
    single["output_provenance"]["part_ids"] = ["point"]
    single["style_hash"] = "e" * 64
    single["style"]["style"]["color"] = "blue"
    ref = _output_references([single], "account-a", client_id, 17)["point"]
    assert ref["dataset_id"] == 201 and ref["content_hash"] == "a" * 64
    assert ref["style"]["style"]["color"] == "blue"


def test_categorized_output_color_changes_require_exact_class_mapping(tmp_path):
    from src.project_sync.controller import _output_references

    item = captured(tmp_path)
    identity = str(uuid4())
    server_style = {
        "dataset_type": "groups",
        "style": {
            "groups": [
                {"column": "server_class", "values": ["original"], "color": "red"}
            ]
        },
        "qgis": {"classification": {"field": "server_class"}},
    }
    provenance = {
        "account_id": "account-a",
        "client_project_id": identity,
        "project_id": 17,
        "dataset_id": 201,
        "version": 3,
        "content_hash": "a" * 64,
        "field_schema": [{"name": "server_class", "type": "string"}],
        "part_id": "one",
        "part_ids": ["one"],
        "local_content_hash": item["content_hash"],
        "local_style_hash": item["style_hash"],
        "server_style": server_style,
        "server_style_hash": "b" * 64,
    }
    item["output_provenance"] = provenance
    item["style"] = {
        "dataset_type": "groups",
        "style": {
            "groups": [
                {"column": "aino_class_2", "values": ["captured"], "color": "blue"}
            ]
        },
        "qgis": {"classification": {"field": "aino_class_2"}},
    }
    reference = _output_references([item], "account-a", identity, 17)["one"]
    assert reference["style"] == server_style
    item["style_hash"] = "c" * 64
    assert not _output_references([item], "account-a", identity, 17)
    provenance["class_mapping"] = {
        "dataset_id": 201,
        "version": 3,
        "content_hash": "a" * 64,
        "local_field": "aino_class_2",
        "server_field": "server_class",
        "classes": {"captured": ["original"]},
    }
    reference = _output_references([item], "account-a", identity, 17)["one"]
    assert reference["style"]["style"]["groups"] == [
        {"column": "server_class", "values": ["original"], "color": "blue"}
    ]
    assert item["style"]["style"]["groups"][0]["column"] == "aino_class_2"
    assert reference["field_schema"] == provenance["field_schema"]
    provenance["class_mapping"]["version"] = 4
    assert not _output_references([item], "account-a", identity, 17)


def test_ui_warning_review_and_default_capture_filesystem(qgis_app, tmp_path, wait_for):
    from types import SimpleNamespace
    from qgis.PyQt.QtCore import Qt
    from qgis.core import QgsCoordinateReferenceSystem, QgsRectangle
    from qgis.gui import QgsMapCanvas
    from src.main_tab import OsmParserDialogMain
    from tests.test_layer_snapshot import layer_with_values

    project = QgsProject.instance()
    layer = layer_with_values()
    project.addMapLayer(layer)
    canvas = QgsMapCanvas()
    canvas.resize(800, 600)
    canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
    canvas.setExtent(QgsRectangle(0, 0, 2000, 2000))
    session = Session()
    session.set_access_token(token())
    api = FakeApi(session)
    journal = RecoveryJournal(tmp_path / "app-data")
    dialog = OsmParserDialogMain(
        osm_parser=SimpleNamespace(
            session=session,
            api_client=api,
            recovery_journal=journal,
            iface=SimpleNamespace(mapCanvas=lambda: canvas),
        )
    )
    try:
        dialog.input_layers.item(0).setCheckState(Qt.CheckState.Checked)
        dialog.prepare_inputs()
        wait_for(lambda: dialog.prepared_inputs is not None)
        area, inputs = dialog.prepared_inputs
        assert Path(inputs[0]["local_path"]).is_relative_to(
            journal.directory / "previews"
        )
        inputs[0]["warnings"] = ["Unsupported style simplification"]
        dialog._inputs_finished(dialog._capture_generation, area, inputs)
        assert not api.calls
        assert "simplification" in dialog.capture_status.text()
        assert "simplifications" in dialog.sync_inputs_button.text()
        dialog.sync_inputs_button.click()
        assert api.calls[-1][1] == "/integrations/qgis/projects"
        owned = Path(dialog.sync_controller.state["layers"][0]["local_path"])
        assert owned.exists()
        layer.setName("Changed after accepted capture")
        assert owned.exists()
        assert dialog.sync_controller.state["layers"][0]["name"] != layer.name()
    finally:
        dialog.close()
        dialog.deleteLater()
        canvas.deleteLater()
        session.deleteLater()
        api.calls.clear()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        project.clear()


def test_real_upload_durable_intent_and_account_change_abort(
    sync, tmp_path, local_api, wait_for, qgis_app
):
    from qgis.PyQt.QtCore import QThread
    from tests.test_upload_transport import issued

    controller, api, journal, project = sync
    controller.prepare(project, [captured(tmp_path)], None)
    linked(api)
    api.answer(issued(local_api.origin))
    wait_for(lambda: len(local_api.requests) == 1)
    state = journal.load(project_identity(project), "account-a")["sync"]
    assert state["layers"][0]["transport_intent"]["method"] == "POST"
    assert state["layers"][0]["transport_intent"]["request_id"]
    assert "upload_token" not in json.dumps(state)
    api.session.set_access_token(token("account-b"))
    local_api.respond(0, 201, headers={"Location": "/session"}, raw=b"")
    qgis_app.processEvents()
    assert controller.state is None
    assert len(local_api.requests) == 1
    assert QThread.currentThread() == qgis_app.thread()


@pytest.mark.parametrize("selection", ["complete", "subset"])
def test_imported_qgis_custom_properties_control_reference_selection(
    sync, tmp_path, selection
):
    from src.project_sync.layers import capture_layers
    from src.project_sync.recovery import PROVENANCE_KEY
    from tests.test_layer_snapshot import layer_with_values
    from tests.test_style_conversion import reference

    controller, api, journal, project = sync
    layers = [layer_with_values(), layer_with_values()]
    project.addMapLayers(layers)
    identity = project_identity(project)
    captures = capture_layers(
        project,
        [layer.id() for layer in layers],
        str(tmp_path / "capture"),
        reference=reference(),
    )
    part_ids = [layer.id() for layer in layers]
    for item in captures:
        layer = project.mapLayer(item["client_layer_id"])
        layer.setCustomProperty(
            PROVENANCE_KEY,
            json.dumps(
                {
                    "account_id": "account-a",
                    "client_project_id": identity,
                    "project_id": 17,
                    "dataset_id": 201,
                    "version": 3,
                    "content_hash": "a" * 64,
                    "field_schema": [],
                    "part_id": layer.id(),
                    "part_ids": part_ids,
                    "local_content_hash": item["content_hash"],
                    "local_style_hash": item["style_hash"],
                    "server_style": {
                        "dataset_type": "default",
                        "style": {"color": "red"},
                        "qgis": {},
                    },
                    "server_style_hash": "b" * 64,
                }
            ),
        )
    selected = captures if selection == "complete" else captures[:1]
    controller.prepare(project, selected, None)
    linked(api)
    if selection == "complete":
        assert api.calls[-1][1] == "/integrations/qgis/projects/17/snapshots"
        manifest = api.calls[-1][2]["layers"]
        assert len(manifest) == 1
        assert manifest[0]["client_layer_id"] == "output:201"
        assert manifest[0]["content_hash"] == "a" * 64
        assert "file_path" not in manifest[0]
    else:
        assert api.calls[-1][1] == "/project/datasets/upload-url"
        assert len(controller.state["manifest"]) == 1
        assert (
            controller.state["manifest"][0]["client_layer_id"]
            == selected[0]["client_layer_id"]
        )
        assert "dataset_id" not in controller.state["manifest"][0]
        exported = json.loads(
            Path(controller.state["layers"][0]["local_path"]).read_text()
        )
        assert len(exported["features"]) == layers[0].featureCount()


@pytest.mark.parametrize("response", [None, [], {"project_id": "invalid"}])
def test_malformed_success_does_not_lose_saved_link_intent(sync, response):
    controller, api, journal, project = sync
    errors = []
    controller.failed.connect(errors.append)
    controller.prepare(project, [], None)
    intent = api.calls[-1][2]
    api.answer(response)
    assert errors
    controller.restore(project)
    assert api.calls[-1][2] == intent


@pytest.mark.parametrize("extension", ["qgs", "qgz"])
def test_first_sync_named_project_warns_until_identity_is_saved(
    sync, tmp_path, extension, qgis_app
):
    controller, api, journal, project = sync
    filename = tmp_path / f"already-saved.{extension}"
    project.setFileName(str(filename))
    assert project.write()
    qgis_app.processEvents()
    notices = []
    controller.notice.connect(notices.append)
    controller.prepare(project, [], None)
    identity = project_identity(project)
    intent = api.calls[-1][2]
    assert notices and "save" in notices[-1].lower()
    before = QgsProject()
    try:
        assert before.read(str(filename))
        assert project_identity(before, create=False) is None
        assert journal.load(identity, "account-a")["sync"]["link_intent"] == intent
    finally:
        sip.delete(before)
    assert project.write()
    qgis_app.processEvents()
    assert notices[-1] == ""
    controller.close()
    restored = QgsProject()
    resumed = SyncController(api, journal=journal)
    recovered_notices = []
    resumed.notice.connect(recovered_notices.append)
    try:
        assert restored.read(str(filename))
        restored.setTitle("Unrelated unsaved title")
        assert restored.isDirty()
        resumed.restore(restored)
        assert project_identity(restored) == identity
        assert api.calls[-1][2] == intent
        assert recovered_notices[-1] == ""
    finally:
        resumed.close()
        sip.delete(resumed)
        sip.delete(restored)


def test_detach_notice_tracks_successful_save_as_not_filename_or_failed_write(
    sync, tmp_path, qgis_app
):
    controller, api, journal, project = sync
    controller.prepare(project, [], None)
    old = project_identity(project)
    original = tmp_path / "original.qgz"
    project.setFileName(str(original))
    assert project.write()
    qgis_app.processEvents()
    notices = []
    controller.notice.connect(notices.append)
    controller.detach(project)
    replacement = project_identity(project)
    assert replacement != old
    assert notices and "save" in notices[-1].lower()
    before = QgsProject()
    try:
        assert before.read(str(original))
        assert project_identity(before) == old
    finally:
        sip.delete(before)
    project.setFileName(str(tmp_path / "missing-directory" / "failure.qgz"))
    assert not project.write()
    qgis_app.processEvents()
    assert "save" in notices[-1].lower()
    target = tmp_path / "replacement.qgz"
    project.setFileName(str(target))
    assert "save" in notices[-1].lower()
    assert project.write()
    qgis_app.processEvents()
    assert notices[-1] == ""
    after = QgsProject()
    try:
        assert after.read(str(target))
        assert project_identity(after) == replacement
    finally:
        sip.delete(after)
    assert journal.load(old, "account-a") is not None


def test_unsaved_identity_survives_controller_recreation_without_false_confirmation(
    sync, tmp_path, qgis_app
):
    controller, api, journal, project = sync
    filename = tmp_path / "saved.qgz"
    project.setFileName(str(filename))
    assert project.write()
    qgis_app.processEvents()
    controller.prepare(project, [], None)
    identity = project_identity(project)
    controller.close()
    resumed = SyncController(api, journal=journal)
    notices = []
    resumed.notice.connect(notices.append)
    try:
        resumed.restore(project)
        assert project_identity(project) == identity
        assert notices and "save" in notices[-1].lower()
        assert project.write()
        qgis_app.processEvents()
        assert notices[-1] == ""
        notices.clear()
        resumed.close()
        project.setTitle("No controller callback after close")
        assert project.write()
        qgis_app.processEvents()
        assert notices == []
    finally:
        resumed.close()
        sip.delete(resumed)


def test_identity_tracker_owned_once_and_follows_project_clear_read_and_destruction(
    qgis_app, tmp_path
):
    from src.project_sync.recovery import (
        ProjectIdentityPersistence,
        project_persistence,
        replace_project_identity,
    )

    project = QgsProject()
    tracker = project_persistence(project)
    try:
        identity = project_identity(project)
        target = tmp_path / "tracked.qgz"
        project.setFileName(str(target))
        assert tracker.notice
        assert project.write()
        qgis_app.processEvents()
        assert tracker.notice == ""
        assert tracker is project_persistence(project)
        assert len(project.findChildren(ProjectIdentityPersistence)) == 1
        replace_project_identity(project)
        assert tracker.notice
        project.clear()
        assert tracker.notice == ""
        assert project_identity(project, create=False) is None
        assert project.read(str(target))
        assert project_identity(project) == identity
        assert tracker is project_persistence(project)
        assert tracker.notice == ""
        project.setTitle("Unsaved unrelated edit")
        assert project.isDirty() and tracker.notice == ""
    finally:
        sip.delete(project)
    assert sip.isdeleted(tracker)


def test_successful_save_compares_written_uuid_when_identity_changes_during_serialization(
    sync, tmp_path, qgis_app
):
    from src.project_sync.recovery import replace_project_identity

    controller, api, journal, project = sync
    notices = []
    controller.notice.connect(notices.append)
    controller.prepare(project, [], None)
    filename = tmp_path / "write-order.qgz"
    project.setFileName(str(filename))

    def changed_during_write(document):
        replace_project_identity(project)

    project.writeProject.connect(changed_during_write)
    assert project.write()
    qgis_app.processEvents()
    project.writeProject.disconnect(changed_during_write)
    stored = QgsProject()
    try:
        assert stored.read(str(filename))
        actual_saved = project_identity(stored, create=False)
        assert bool(notices[-1]) == (actual_saved != project_identity(project))
    finally:
        sip.delete(stored)
    assert project.write()
    qgis_app.processEvents()
    assert notices[-1] == ""


@pytest.mark.parametrize(
    "filename",
    [
        "missing.qgz",
        "invalid.qgz",
        "unrecognized.project",
        "https://example.invalid/project.qgz",
    ],
)
def test_unverified_project_metadata_never_confirms_uuid(qgis_app, tmp_path, filename):
    from src.project_sync.recovery import (
        PROJECT_SCOPE,
        PROJECT_KEY,
        project_persistence,
    )

    project = QgsProject()
    try:
        path = filename if "://" in filename else str(tmp_path / filename)
        if filename == "invalid.qgz":
            Path(path).write_bytes(b"Not a QGIS archive")
        project.setFileName(path)
        project.writeEntry(PROJECT_SCOPE, PROJECT_KEY, str(uuid4()))
        assert "save" in project_persistence(project).notice.lower()
    finally:
        sip.delete(project)
