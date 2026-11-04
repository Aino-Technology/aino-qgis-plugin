from uuid import uuid4

import pytest
from qgis.PyQt import sip
from qgis.core import QgsProject

from src.analysis.controller import AnalysisController
from src.analysis.request_controller import RequestController
from src.project_sync.controller import SyncController
from src.project_sync.recovery import RecoveryJournal, project_identity
from src.session import Session
from tests.test_analysis_controller import Api, Clock, state
from tests.test_api_client import token


class Capture:
    def __init__(self):
        self.calls = []
        self.cancelled = 0

    def __call__(self, area, ids, finished, failed):
        self.calls.append((area, ids, finished, failed))
        return self

    def cancel(self):
        self.cancelled += 1


@pytest.fixture
def request_setup(qgis_app, tmp_path):
    session = Session()
    session.set_access_token(token())
    api = Api(session)
    project = QgsProject()
    journal = RecoveryJournal(tmp_path)
    sync = SyncController(api, journal=journal)
    analysis = AnalysisController(api, journal, project, scheduler=Clock())
    capture = Capture()
    request = RequestController(project, session, journal, sync, analysis, capture)
    yield request, capture, api, journal, project
    request.close()
    sync.close()
    analysis.close()
    sip.delete(request)
    sip.delete(sync)
    sip.delete(analysis)
    sip.delete(project)
    sip.delete(session)


def ready(api):
    api.answer(
        {
            "project_id": 17,
            "link_id": str(uuid4()),
            "project_url": "https://example.invalid/17",
        }
    )
    snapshot = {"id": str(uuid4()), "project_id": 17, "state": "ready", "layers": []}
    api.answer(snapshot)
    return snapshot


def test_empty_project_one_go(request_setup):
    request, capture, api, journal, project = request_setup
    request.start("Parks in Berlin", None, [])
    assert (
        journal.load(project_identity(project), "account-a")["request"]["phase"]
        == "capturing"
    )
    capture.calls[0][2]([])
    snapshot = ready(api)
    assert [c[1] for c in api.calls] == [
        "/integrations/qgis/projects",
        "/integrations/qgis/projects/17/snapshots",
        "/integrations/qgis/analyses",
    ]
    assert api.calls[-1][2]["snapshot_id"] == snapshot["id"]
    assert api.calls[-1][2]["area"] is None
    assert api.calls[0][2]["title"] == "Parks in Berlin"
    request.sync.ready.emit(snapshot)
    assert len(api.calls) == 3


def test_double_go_is_single_intent(request_setup):
    request, capture, api, *_ = request_setup
    request.start("parks", None, [])
    original = request.state["intent_id"]
    request.start("other", None, [])
    assert request.state["intent_id"] == original
    assert len(capture.calls) == 1


def test_existing_project_reuses_link(request_setup):
    request, capture, api, *_ = request_setup
    request.start("first", None, [])
    capture.calls[-1][2]([])
    ready(api)
    task = str(uuid4())
    api.answer(
        {"task_id": task, "project_id": 17, "project_url": "https://example.invalid/17"}
    )
    api.answer(state(task, status="completed"))
    request.start("second", None, [])
    capture.calls[-1][2]([])
    assert sum(c[1] == "/integrations/qgis/projects" for c in api.calls) == 1
    assert api.calls[-1][1].endswith("/snapshots")


def test_resume_after_snapshot_before_start(request_setup, monkeypatch):
    request, capture, api, journal, project = request_setup
    request.start("parks", None, [])
    capture.calls[-1][2]([])
    original = request.analysis.start
    monkeypatch.setattr(request.analysis, "start", lambda *args: None)
    snapshot = ready(api)
    assert request.state["phase"] == "starting"
    monkeypatch.setattr(request.analysis, "start", original)
    request.pause()
    request.restore()
    assert api.calls[-1][2]["snapshot_id"] == snapshot["id"]
    assert len(capture.calls) == 1
    assert sum(c[1].endswith("/snapshots") for c in api.calls) == 1


def test_lost_start_reply_keeps_request_id(request_setup):
    request, capture, api, *_ = request_setup
    request.start("parks", None, [])
    capture.calls[-1][2]([])
    ready(api)
    original = api.calls[-1][2]
    api.answer(status=0, error="network_error")
    request.pause()
    request.restore()
    assert api.calls[-1][2] == original
    assert len(capture.calls) == 1


def test_changed_project_ignores_capture(request_setup):
    request, capture, api, *_ = request_setup
    request.start("parks", None, [])
    request.project.clear()
    capture.calls[-1][2]([])
    assert not api.calls


def test_incomplete_capture_requires_recapture(request_setup):
    request, capture, api, *_ = request_setup
    request.start("parks", None, ["frozen"])
    request.pause()
    request.restore()
    assert request.state["phase"] == "recapture"
    assert not api.calls and len(capture.calls) == 1
    request.retry()
    assert len(capture.calls) == 2 and capture.calls[-1][1] == ["frozen"]


def test_uncertain_start_cancel_restores_then_cancels_once(request_setup):
    request, capture, api, *_ = request_setup
    request.start("parks", None, [])
    capture.calls[-1][2]([])
    ready(api)
    original = api.calls[-1][2]
    api.answer(status=0, error="network_error")
    request.cancel()
    assert api.calls[-1][2] == original
    task = str(uuid4())
    api.answer(
        {"task_id": task, "project_id": 17, "project_url": "https://example.invalid/17"}
    )
    actions = [c for c in api.calls if c[1].endswith("/actions")]
    assert len(actions) == 1 and actions[0][2]["kind"] == "cancel"
    request.analysis.changed.emit(request.analysis.state)
    assert len([c for c in api.calls if c[1].endswith("/actions")]) == 1


def test_reconstructed_controller_reuses_uncertain_start(request_setup):
    request, capture, api, journal, project = request_setup
    request.start("parks", None, [])
    capture.calls[-1][2]([])
    ready(api)
    original = api.calls[-1][2]
    request.close()
    request.analysis.close()
    request.sync.close()
    sync = SyncController(api, journal=journal)
    analysis = AnalysisController(api, journal, project, scheduler=Clock())
    restored = RequestController(
        project, api.session, journal, sync, analysis, Capture()
    )
    try:
        restored.restore()
        assert api.calls[-1][2] == original
        assert not restored.capture.calls
        assert sum(c[1].endswith("/snapshots") for c in api.calls) == 1
    finally:
        restored.close()
        analysis.close()
        sync.close()
        sip.delete(restored)
        sip.delete(analysis)
        sip.delete(sync)


def test_cancel_sync_keeps_uncertain_link_and_never_starts(request_setup):
    request, capture, api, journal, project = request_setup
    request.start("parks", None, [])
    capture.calls[-1][2]([])
    original = api.calls[-1][2]
    request.cancel()
    assert request.state["phase"] == "cancelled"
    request.restore()
    assert api.calls[-1][2] == original
    ready(api)
    assert not any(c[1] == "/integrations/qgis/analyses" for c in api.calls)
    request.start("different", None, [])
    assert len(capture.calls) == 2


def test_account_change_ignores_capture(request_setup):
    request, capture, api, *_ = request_setup
    request.start("parks", None, [])
    api.session.clear()
    capture.calls[-1][2]([])
    assert not api.calls and request.state == {}


def test_other_ready_snapshot_cannot_start_request(request_setup):
    request, capture, api, *_ = request_setup
    request.start("parks", None, [])
    capture.calls[-1][2]([])
    request.sync.state["context"]["request_intent_id"] = str(uuid4())
    request.sync.ready.emit({"id": str(uuid4()), "project_id": 17})
    assert len(api.calls) == 1


def test_close_discards_owned_capture_and_fences_callbacks(request_setup):
    request, capture, api, *_ = request_setup
    request.start("parks", None, [])
    request.close()
    request.close()
    capture.calls[-1][2]([])
    assert capture.cancelled == 1 and not api.calls


@pytest.fixture
def request_dialog(qgis_app, tmp_path):
    from types import SimpleNamespace
    from qgis.core import QgsCoordinateReferenceSystem, QgsRectangle
    from qgis.gui import QgsMapCanvas
    from src.main_tab import OsmParserDialogMain
    from tests.test_layer_snapshot import layer_with_values

    project = QgsProject.instance()
    project.clear()
    layer = layer_with_values()
    project.addMapLayer(layer)
    canvas = QgsMapCanvas()
    canvas.resize(800, 600)
    canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
    canvas.setExtent(QgsRectangle(0, 0, 2000, 2000))
    session = Session()
    session.set_access_token(token())
    api = Api(session)
    dialog = OsmParserDialogMain(
        osm_parser=SimpleNamespace(
            session=session,
            api_client=api,
            recovery_journal=RecoveryJournal(tmp_path),
            iface=SimpleNamespace(mapCanvas=lambda: canvas),
        )
    )
    yield dialog, layer, api
    dialog.request_controller.close()
    dialog.result_controller.close()
    dialog.analysis_controller.close()
    dialog.sync_controller.close()
    dialog.close()
    sip.delete(dialog)
    sip.delete(canvas)
    sip.delete(session)
    project.clear()


def test_form_edits_and_hide_do_not_cancel_accepted_capture(request_dialog, wait_for):
    from qgis.PyQt.QtCore import Qt

    dialog, layer, api = request_dialog
    assert dialog.selected_input_ids() == []
    dialog.input_layers.item(0).setCheckState(Qt.CheckState.Checked)
    dialog.send_osm_request("original")
    job = dialog.request_controller._job
    dialog.input_layers.item(0).setCheckState(Qt.CheckState.Unchecked)
    dialog.prompt_field.setPlainText("edited")
    dialog.area_mode.setCurrentIndex(1)
    dialog.close()
    assert job.job.active
    wait_for(lambda: bool(api.calls))
    assert dialog.request_controller.state["prompt"] == "original"
    assert dialog.request_controller.state["area"] is None
    assert dialog.sync_controller.state["layers"][0]["client_layer_id"] == layer.id()
    assert not dialog.go_button.isEnabled()


def test_source_mutation_requires_explicit_recapture(request_dialog):
    from qgis.PyQt.QtCore import Qt

    dialog, layer, api = request_dialog
    dialog.input_layers.item(0).setCheckState(Qt.CheckState.Checked)
    dialog.send_osm_request("parks")
    layer.setName("changed during capture")
    assert dialog.request_controller.state["phase"] == "recapture"
    assert not api.calls
    dialog.request_controller.retry()
    assert dialog.request_controller._job is not None


def test_new_layers_remain_unchecked_after_accepted_selection(request_dialog):
    from qgis.PyQt.QtCore import Qt
    from tests.test_layer_snapshot import layer_with_values

    dialog, layer, api = request_dialog
    dialog.input_layers.item(0).setCheckState(Qt.CheckState.Checked)
    dialog.send_osm_request("parks")
    added = layer_with_values()
    QgsProject.instance().addMapLayer(added)
    assert dialog.request_controller.state["phase"] == "capturing"
    assert dialog.selected_input_ids() == [layer.id()]
    assert dialog.request_controller.state["layer_ids"] == [layer.id()]


@pytest.mark.parametrize("scope_change", ["project", "account"])
def test_late_sync_reply_cannot_cross_scope(request_setup, scope_change):
    request, capture, api, journal, project = request_setup
    request.start("parks", None, [])
    capture.calls[-1][2]([])
    identity = project_identity(project)
    saved = journal.load(identity, "account-a")
    if scope_change == "project":
        project.clear()
    else:
        api.session.clear()
    api.answer(
        {
            "project_id": 17,
            "link_id": str(uuid4()),
            "project_url": "https://example.invalid/17",
        }
    )
    assert len(api.calls) == 1
    assert journal.load(identity, "account-a") == saved


def test_cancel_intent_survives_restart_before_task_is_known(request_setup):
    request, capture, api, journal, project = request_setup
    request.start("parks", None, [])
    capture.calls[-1][2]([])
    ready(api)
    original = api.calls[-1][2]
    request.cancel()
    assert len(api.calls) == 3
    request.close()
    request.analysis.close()
    request.sync.close()
    sync = SyncController(api, journal=journal)
    analysis = AnalysisController(api, journal, project, scheduler=Clock())
    restored = RequestController(
        project, api.session, journal, sync, analysis, Capture()
    )
    try:
        restored.restore()
        assert api.calls[-1][2] == original
        api.answer(
            {
                "task_id": str(uuid4()),
                "project_id": 17,
                "project_url": "https://example.invalid/17",
            }
        )
        assert api.calls[-1][2]["kind"] == "cancel"
        assert sum(c[1].endswith("/actions") for c in api.calls) == 1
    finally:
        restored.close()
        analysis.close()
        sync.close()
        sip.delete(restored)
        sip.delete(analysis)
        sip.delete(sync)


def test_frozen_inputs_are_not_mutated_by_caller(request_setup):
    request, capture, *_ = request_setup
    area = {"kind": "polygon", "geometry": {"coordinates": [[1, 2]]}}
    ids = ["first"]
    request.start("parks", area, ids)
    area["geometry"]["coordinates"][0][0] = 9
    ids.append("second")
    assert request.state["area"]["geometry"]["coordinates"] == [[1, 2]]
    assert capture.calls[0][1] == ["first"]


def test_interrupted_adoption_never_reuses_old_snapshot(request_setup):
    request, capture, api, journal, project = request_setup
    request.start("parks", None, [])
    request.state["phase"] = "synchronizing"
    request._save()
    request.pause()
    journal.update_sections(
        project_identity(project),
        "account-a",
        sync={
            "phase": "ready",
            "context": {"request_intent_id": "other"},
            "snapshot": {"id": str(uuid4()), "state": "ready", "project_id": 17},
        },
    )
    request.restore()
    assert request.state["phase"] == "recapture"
    assert not api.calls


@pytest.mark.parametrize("reconstruct_at", [None, "before_retry", "after_retry"])
def test_failed_snapshot_retry_starts_once_with_new_snapshot(
    request_setup, reconstruct_at
):
    request, capture, api, journal, project = request_setup
    rebuilt = []

    def reconstruct(current):
        current.close()
        current.analysis.close()
        current.sync.close()
        sync = SyncController(api, journal=journal)
        analysis = AnalysisController(api, journal, project, scheduler=Clock())
        restored = RequestController(
            project, api.session, journal, sync, analysis, Capture()
        )
        rebuilt.append(restored)
        restored.restore()
        return restored

    try:
        request.start("parks", None, [])
        capture.calls[-1][2]([])
        api.answer(
            {
                "project_id": 17,
                "link_id": str(uuid4()),
                "project_url": "https://example.invalid/17",
            }
        )
        failed_snapshot = str(uuid4())
        api.answer(
            {
                "id": failed_snapshot,
                "project_id": 17,
                "state": "failed",
                "layers": [],
                "errors": [],
            }
        )
        assert request.state["phase"] == "recapture"
        old_operation = request.state["sync_operation_id"]
        if reconstruct_at == "before_retry":
            request = reconstruct(request)
        request.retry()
        request.capture.calls[-1][2]([])
        new_operation = request.sync.state["operation_id"]
        assert new_operation != old_operation
        snapshot_body = api.calls[-1][2]
        if reconstruct_at == "after_retry":
            request = reconstruct(request)
            assert api.calls[-1][2] == snapshot_body
        snapshot = {
            "id": str(uuid4()),
            "project_id": 17,
            "state": "ready",
            "layers": [],
        }
        api.answer(snapshot)
        request.sync.ready.emit(snapshot)
        starts = [
            call for call in api.calls if call[1] == "/integrations/qgis/analyses"
        ]
        assert len(starts) == 1
        assert starts[0][2]["snapshot_id"] == snapshot["id"] != failed_snapshot
        saved = journal.load(project_identity(project), "account-a")["request"]
        assert saved["sync_operation_id"] == new_operation
        assert saved["snapshot_id"] == snapshot["id"]
    finally:
        for restored in rebuilt:
            restored.close()
            restored.analysis.close()
            restored.sync.close()
            sip.delete(restored.analysis)
            sip.delete(restored.sync)
            sip.delete(restored)


def test_uncertain_snapshot_retry_preserves_sync_association(request_setup):
    request, capture, api, *_ = request_setup
    request.start("parks", None, [])
    capture.calls[-1][2]([])
    api.answer(
        {
            "project_id": 17,
            "link_id": str(uuid4()),
            "project_url": "https://example.invalid/17",
        }
    )
    operation = request.state["sync_operation_id"]
    body = api.calls[-1][2]
    api.answer(status=0, error="network_error")
    request.retry()
    assert request.state["sync_operation_id"] == operation
    assert api.calls[-1][2] == body
    assert len(capture.calls) == 1


@pytest.mark.parametrize(
    "prompt, title, filename, expected",
    [
        ("Parks in Berlin\nAdditional details", "", "", "Parks in Berlin"),
        ("A" * 140 + "\nAdditional details", "", "", "A" * 120),
        ("Prompt\nDetails", "T" * 150, "/tmp/file.qgz", "T" * 150),
        ("Prompt\nDetails", "", "/tmp/saved-project.qgz", "saved-project"),
    ],
)
def test_first_link_title_uses_bounded_first_prompt_line(
    request_setup, prompt, title, filename, expected
):
    request, capture, api, _, project = request_setup
    project.setTitle(title)
    project.setFileName(filename)
    request.start(prompt, None, [])
    capture.calls[-1][2]([])
    assert api.calls[-1][2]["title"] == expected
