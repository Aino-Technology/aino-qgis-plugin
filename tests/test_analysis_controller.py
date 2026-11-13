import copy
import json
from pathlib import Path
from uuid import uuid4

import pytest
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QCoreApplication, QEvent
from qgis.core import QgsProject

from src.api_client import ApiResponse
from src.analysis.controller import AnalysisController
from src.project_sync.recovery import RecoveryJournal, project_identity
from src.session import Session
from tests.test_api_client import token


class Handle:
    def __init__(self):
        self.pending = True

    def abort(self):
        self.pending = False


class Api:
    def __init__(self, session):
        self.session = session
        self.calls = []

    def send(self, method, path, body, callback):
        handle = Handle()
        self.calls.append((method, path, copy.deepcopy(body), callback, handle))
        return handle

    def answer(self, data=None, status=200, error="", index=-1):
        call = self.calls[index]
        call[4].pending = False
        call[3](ApiResponse(status, data, error))


class Clock:
    def __init__(self):
        self.delay = None
        self.callback = None

    def schedule(self, seconds, callback):
        self.delay, self.callback = seconds, callback

    def stop(self):
        self.delay, self.callback = None, None

    def fire(self):
        callback = self.callback
        self.stop()
        callback()


def state(task, revision=1, status="running", **kwargs):
    return {
        "task_id": task,
        "project_id": 17,
        "status": status,
        "revision": revision,
        "steps": [],
        "pending_action": None,
        "results_ready": False,
        **kwargs,
    }


@pytest.fixture
def setup(qgis_app, tmp_path):
    session = Session()
    session.set_access_token(token())
    api = Api(session)
    clock = Clock()
    project = QgsProject()
    journal = RecoveryJournal(tmp_path)
    controller = AnalysisController(
        api, journal=journal, project=project, scheduler=clock
    )
    yield controller, api, clock, journal, project
    controller.close()
    api.calls.clear()
    sip.delete(controller)
    sip.delete(project)
    sip.delete(session)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_poll_after_response_single_reply_backoff_reset(setup):
    controller, api, clock, *_ = setup
    poll = controller.polling
    task = str(uuid4())
    poll.watch(task)
    assert len(api.calls) == 1 and clock.callback is None
    poll.watch(task)
    assert len(api.calls) == 1
    for delay in [4, 8, 16, 32, 60, 60]:
        api.answer(status=0, error="network_error")
        assert clock.delay == delay
        clock.fire()
    api.answer(state(task, status="awaiting_input"))
    assert clock.delay == 4
    clock.fire()
    api.answer(state(task, revision=2, status="completed"))
    assert clock.callback is None


def test_start_intent_before_http_and_lost_response_replays(setup):
    controller, api, clock, journal, project = setup
    controller.start(17, str(uuid4()), "Find parks", {"kind": "extent"})
    first = api.calls[-1][2]
    assert (
        journal.load(project_identity(project), "account-a")["analysis"]["start_intent"]
        == first
    )
    api.answer(status=0, error="network_error")
    assert controller.state.get("latest") is None
    controller.restore()
    assert api.calls[-1][2] == first
    task = str(uuid4())
    api.answer(
        {
            "task_id": task,
            "project_id": 17,
            "status": "queued",
            "project_url": "https://example.invalid/17",
        },
        202,
    )
    assert api.calls[-1][1].endswith(task)
    controller.restore()
    assert sum(call[0] == "POST" for call in api.calls) == 2


def test_revision_guard_and_close_never_cancel(setup):
    controller, api, clock, *_ = setup
    task = str(uuid4())
    controller.restore(task)
    api.answer(state(task, 8, "awaiting_input"))
    clock.fire()
    api.answer(state(task, 7))
    assert controller.state["latest"]["revision"] == 8
    controller.pause()
    assert clock.callback is None
    assert all(call[0] == "GET" for call in api.calls)


def test_text_only_completion_fetches_full_report(setup):
    controller, api, clock, journal, project = setup
    task = str(uuid4())
    results = []
    controller.results_ready.connect(results.append)
    controller.restore(task)
    api.answer(state(task, 1, "completed", results_ready=True, summary="preview"))
    assert api.calls[-1][1].endswith("/results")
    text = "Full answer " * 200
    api.answer({"task_id": task, "layers": [], "report": text})
    assert results[-1]["report"] == text
    assert (
        journal.load(project_identity(project), "account-a")["analysis"]["results"][
            "report"
        ]
        == text
    )


def waiting(task, kind="answer", action_id=None):
    return state(
        task,
        2,
        "awaiting_input",
        pending_action={
            "action_id": action_id or str(uuid4()),
            "kind": kind,
            "items": [],
            "sources": [],
        },
    )


def test_action_durable_loss_replay_and_stale_round_refresh(setup):
    controller, api, clock, journal, project = setup
    task = str(uuid4())
    controller.restore(task)
    pending = waiting(task)
    api.answer(pending)
    action = {
        "kind": "answer",
        "action_id": pending["pending_action"]["action_id"],
        "payload": {"answers": [{"question_id": "purpose", "text": "parks"}]},
    }
    controller.submit_action(action)
    original = api.calls[-1][2]
    assert (
        journal.load(project_identity(project), "account-a")["analysis"][
            "action_intent"
        ]
        == original
    )
    assert clock.callback is None
    api.answer(status=0, error="network_error")
    controller.restore()
    assert api.calls[-1][2] == original
    api.answer(rejection("qgis_action_conflict", "core", "conflict"), 409)
    assert api.calls[-1][0] == "GET"
    api.answer(state(task, 5))
    assert "action_intent" not in controller.state
    assert controller.state["latest"]["revision"] == 5


def test_cancel_completed_race_uses_server_result(setup):
    controller, api, clock, *_ = setup
    task = str(uuid4())
    controller.restore(task)
    api.answer(state(task))
    controller.cancel()
    assert api.calls[-1][2]["kind"] == "cancel"
    assert controller.state["latest"]["status"] == "running"
    api.answer(state(task, 2, "completed", results_ready=True))
    assert controller.state["latest"]["status"] == "completed"
    assert api.calls[-1][1].endswith("/results")
    api.answer(
        {"task_id": task, "layers": [], "report": "Completed before cancellation"}
    )
    assert clock.callback is None


def test_web_action_appears_next_poll_and_old_answer_is_not_retargeted(setup):
    controller, api, clock, *_ = setup
    task = str(uuid4())
    controller.restore(task)
    old = waiting(task)
    api.answer(old)
    clock.fire()
    new = waiting(task)
    new["revision"] = 3
    api.answer(new)
    with pytest.raises(ValueError, match="no longer current"):
        controller.submit_action(
            {
                "kind": "answer",
                "action_id": old["pending_action"]["action_id"],
                "payload": {},
            }
        )
    assert all(call[0] == "GET" for call in api.calls)


def test_journal_failure_blocks_mutation_and_preserves_other_sections(
    setup, monkeypatch
):
    controller, api, clock, journal, project = setup
    identity = project_identity(project)
    journal.update_sections(
        identity, "account-a", sync={"phase": "ready"}, imports={"keep": True}
    )
    original = journal.update_sections

    def failure(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(journal, "update_sections", failure)
    with pytest.raises(OSError):
        controller.start(17, str(uuid4()), "parks", {})
    with pytest.raises(OSError):
        controller.restore()
    assert api.calls == []
    monkeypatch.setattr(journal, "update_sections", original)
    controller.restore()
    saved = journal.load(identity, "account-a")
    assert saved["sync"] == {"phase": "ready"} and saved["imports"] == {"keep": True}
    assert saved["analysis"]["start_intent"] == api.calls[-1][2]


def test_project_and_account_late_callbacks_cannot_mutate_new_scope(setup):
    controller, api, clock, journal, project = setup
    task = str(uuid4())
    controller.restore(task)
    old = api.calls[-1]
    replacement = QgsProject()
    try:
        controller.bind(replacement)
        old[3](ApiResponse(200, state(task, 99)))
        assert controller.state == {}
        controller.restore(str(uuid4()))
        other = api.calls[-1]
        api.session.set_access_token(token("account-b"))
        other[3](ApiResponse(200, state(task, 100)))
        assert controller.state == {} and clock.callback is None
    finally:
        controller.bind(project)
        sip.delete(replacement)


def test_report_shapes_and_safe_text():
    from src.analysis.actions import report_text

    text = (
        "Участок {literal} **important**\n\n{widget:secret-id}\n\n"
        + "Full answer " * 200
    )
    rendered = report_text(text)
    assert "secret-id" not in rendered and rendered.endswith("answer")
    assert "{literal}" in rendered
    structured = {
        "executive_summary": "Overview",
        "recommendation": "Act",
        "conclusion": "Done",
        "kpi": [{"indicator": "Road", "threshold": "500m"}],
        "risks": [{"risk": "No survey", "severity": "high"}],
        "methodology": {"score_formula": "a + b"},
        "parcels_available": False,
        "parcels_unavailable_reason": "Missing parcel source",
    }
    rendered = report_text(structured)
    for value in [
        "Overview",
        "Act",
        "Done",
        "500m",
        "No survey",
        "a + b",
        "not verified",
        "Missing parcel source",
    ]:
        assert value in rendered
    assert "executive_summary" not in rendered
    assert report_text({"text": "Generic"}) == "Generic"
    assert (
        report_text(None) == report_text({"unknown": "hidden"}) == report_text("") == ""
    )


def test_native_questions_full_wording_multiselect_free_text_and_plan(qgis_app):
    from src.analysis.actions import ActionPanel

    panel = ActionPanel()
    try:
        pending = {
            "action_id": str(uuid4()),
            "kind": "answer",
            "items": [
                {
                    "id": "q1",
                    "header": "Purpose",
                    "question": "What exactly should we find?",
                    "multi_select": True,
                    "allow_free_text": True,
                    "options": [
                        {"id": "a", "label": "Parks", "description": "Public parks"},
                        {"id": "b", "label": "Forests"},
                    ],
                },
                {
                    "id": "q2",
                    "question": "Which criteria?",
                    "options": [],
                    "allow_free_text": True,
                },
            ],
        }
        panel.set_action(pending)
        with pytest.raises(ValueError, match="every"):
            panel.action()
        panel.rows[0][1][0][1].setChecked(True)
        panel.rows[0][1][1][1].setChecked(True)
        panel.rows[0][2].setPlainText("With paths")
        panel.rows[1][2].setPlainText("Accessible")
        assert panel.action()["payload"]["answers"] == [
            {"question_id": "q1", "option_ids": ["a", "b"], "text": "With paths"},
            {"question_id": "q2", "option_ids": [], "text": "Accessible"},
        ]
        panel.set_action(pending)
        assert panel.rows[1][2].toPlainText() == "Accessible"
        panel.set_action(
            {
                "kind": "approve_plan",
                "action_id": "revision",
                "text": "Stored plan",
                "items": [],
            }
        )
        assert panel.action()["payload"] == {}
        panel.set_action(
            {
                "kind": "approve_plan",
                "action_id": "question",
                "text": "Flattened plan",
                "items": [
                    {
                        "id": "plan",
                        "question": "Run or edit?",
                        "options": [
                            {"id": "run", "label": "Run"},
                            {"id": "edit", "label": "Edit"},
                        ],
                    }
                ],
            }
        )
        panel.rows[0][1][1][1].setChecked(True)
        with pytest.raises(ValueError, match="Describe"):
            panel.action()
        panel.rows[0][2].setPlainText("Use schools")
        assert panel.action()["payload"]["answers"][0]["option_ids"] == ["edit"]
    finally:
        sip.delete(panel)


def test_native_consent_rates_missing_prices_and_credits_pause(qgis_app):
    from src.analysis.actions import ActionPanel
    from qgis.PyQt.QtWidgets import QLabel

    panel = ActionPanel()
    try:
        panel.set_action(
            {
                "kind": "confirm_sources",
                "action_id": "round",
                "sources": ["paid", "free", "missing"],
                "source_info": [
                    {"tag": "paid", "unit": "venue", "credits_per_unit": 1.25},
                    {"tag": "free", "unit": "request", "credits_per_unit": 0},
                ],
                "expires_at": "2026-11-03T12:00:00Z",
            }
        )
        text = " ".join(label.text() for label in panel.findChildren(QLabel))
        assert "1.25 credits / venue" in text and "0 credits / request" in text
        assert "missing: price unavailable" in text and "2026-11-03" in text
        assert panel.action() == {
            "kind": "confirm_sources",
            "action_id": "round",
            "payload": {},
        }
        panel.set_action(None)
        assert panel.submit_button is None
    finally:
        sip.delete(panel)


def test_restored_owner_destruction_aborts_pending_delivery(setup):
    controller, api, clock, *_ = setup
    controller.restore(str(uuid4()))
    handle = api.calls[-1][4]
    controller.pause()
    assert not handle.pending
    assert not any(call[0] == "POST" for call in api.calls)


@pytest.mark.parametrize("phase", ["start", "action"])
def test_reconstruct_replays_exact_durable_intent(setup, phase):
    controller, api, clock, journal, project = setup
    controller.start(17, str(uuid4()), "parks", {})
    if phase == "action":
        task = str(uuid4())
        api.answer(
            {
                "task_id": task,
                "project_id": 17,
                "project_url": "https://example.invalid/17",
            },
            202,
        )
        pending = waiting(task)
        api.answer(pending)
        controller.submit_action(
            {
                "kind": "answer",
                "action_id": pending["pending_action"]["action_id"],
                "payload": {"answers": [{"question_id": "q", "text": "parks"}]},
            }
        )
    original = api.calls[-1][2]
    controller.pause()
    recovered = AnalysisController(
        api, journal=journal, project=project, scheduler=Clock()
    )
    try:
        recovered.restore()
        assert api.calls[-1][2] == original
    finally:
        recovered.close()
        sip.delete(recovered)


def test_real_http_poll_auth_pause_and_same_account_resume(
    client, local_api, wait_for, tmp_path
):
    session, api = client
    session.set_access_token(token())
    project = QgsProject()
    clock = Clock()
    controller = AnalysisController(
        api, journal=RecoveryJournal(tmp_path), project=project, scheduler=clock
    )
    task = str(uuid4())
    try:
        controller.restore(task)
        wait_for(lambda: len(local_api.requests) == 1)
        local_api.respond(0, 401, {})
        wait_for(lambda: controller.polling.handle.waiting_for_login)
        controller.restore()
        assert len(local_api.requests) == 1 and clock.callback is None
        session.set_access_token(token(generation=2))
        wait_for(lambda: len(local_api.requests) == 2)
        local_api.respond(1, 200, state(task))
        wait_for(lambda: controller.state.get("latest") is not None)
        assert clock.delay == 4
        clock.fire()
        wait_for(lambda: len(local_api.requests) == 3)
        controller.pause()
        assert not api.pending_count and clock.callback is None
        assert all(request["method"] == "GET" for request in local_api.requests)
    finally:
        controller.close()
        sip.delete(controller)
        sip.delete(project)


def test_default_go_prepares_snapshot_and_repeated_ready_never_restarts(
    qgis_app, tmp_path, wait_for
):
    from types import SimpleNamespace
    from qgis.gui import QgsMapCanvas
    from src.main_tab import OsmParserDialogMain

    project = QgsProject.instance()
    project.clear()
    canvas = QgsMapCanvas()
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
    try:
        dialog.prompt_field.setPlainText("parks")
        dialog.go_button.click()
        assert not dialog.go_button.isEnabled()
        wait_for(lambda: bool(api.calls))
        assert api.calls[-1][1] == "/integrations/qgis/projects"
        api.answer(
            {
                "project_id": 17,
                "link_id": str(uuid4()),
                "project_url": "https://example.invalid/17",
            }
        )
        snapshot = {
            "id": str(uuid4()),
            "project_id": 17,
            "state": "ready",
            "layers": [],
        }
        api.answer(snapshot)
        assert api.calls[-1][1] == "/integrations/qgis/analyses"
        assert api.calls[-1][2]["area"] is None
        original = api.calls[-1][2]
        dialog._sync_ready(snapshot)
        dialog._sync_ready(snapshot)
        assert len(api.calls) == 3
        task = str(uuid4())
        api.answer(
            {
                "task_id": task,
                "project_id": 17,
                "project_url": "https://example.invalid/17",
            },
            202,
        )
        api.answer(
            state(
                task,
                1,
                "awaiting_confirmation",
                error="Analysis is waiting for additional credits",
            )
        )
        assert "not enough credits" in dialog.analysis_status.text()
        assert not dialog.analysis_panel.add_credits.isHidden()
        assert dialog.analysis_actions.submit_button is None
        assert dialog.analysis_controller.state["start_intent"] == original
        dialog.close()
        assert not any(call[1].endswith("/actions") for call in api.calls)
    finally:
        dialog.shutdown()
        sip.delete(dialog)
        sip.delete(canvas)
        sip.delete(session)
        project.clear()


def test_corrupt_new_scope_cannot_save_or_send_old_task(setup, monkeypatch):
    controller, api, clock, journal, project = setup
    controller.restore(str(uuid4()))
    other = QgsProject()
    project_identity(other)
    controller.project = other
    original = journal.load

    def invalid(*args):
        raise ValueError("invalid journal")

    monkeypatch.setattr(journal, "load", invalid)
    try:
        count = len(api.calls)
        with pytest.raises(ValueError, match="invalid journal"):
            controller.restore()
        assert controller.state == {} and controller.identity is None
        with pytest.raises(ValueError):
            controller.restore()
        assert len(api.calls) == count
        monkeypatch.setattr(journal, "load", original)
        controller.restore()
        assert controller.state == {} and len(api.calls) == count
    finally:
        controller.bind(project)
        sip.delete(other)


def test_optional_project_defaults_to_current_qgis_project(setup):
    _, api, _, journal, _ = setup
    controller = AnalysisController(api, journal=journal)
    try:
        assert controller.project is QgsProject.instance()
    finally:
        controller.close()
        sip.delete(controller)


def rejection(
    code, domain="api_gateway", result="bad_request", message="Request rejected"
):
    return {
        "result": result,
        "domain": domain,
        "error_code": code,
        "error_message": message,
    }


def test_definite_start_validation_rejection_survives_restart_and_allows_correction(
    setup,
):
    controller, api, clock, journal, project = setup
    controller.start(17, str(uuid4()), "x" * 50001, {})
    rejected = api.calls[-1][2]
    api.answer(
        rejection("qgis_request_validation", message="Prompt exceeds 50000 characters"),
        400,
    )
    assert "start_intent" not in controller.state
    assert controller.state["last_rejection"]["request"] == rejected
    assert (
        journal.load(project_identity(project), "account-a")["analysis"][
            "last_rejection"
        ]["operation"]
        == "start"
    )
    controller.pause()
    recovered = AnalysisController(
        api, journal=journal, project=project, scheduler=Clock()
    )
    try:
        count = len(api.calls)
        recovered.restore()
        assert len(api.calls) == count
        assert (
            recovered.state["last_rejection"]["message"]
            == "Prompt exceeds 50000 characters"
        )
        recovered.start(17, str(uuid4()), "Find parks", {})
        assert api.calls[-1][2]["request_id"] != rejected["request_id"]
        assert api.calls[-1][2]["prompt"] == "Find parks"
    finally:
        recovered.close()
        sip.delete(recovered)


def test_insufficient_credit_action_rejection_restores_polling_and_cancel_after_restart(
    setup,
):
    controller, api, clock, journal, project = setup
    task = str(uuid4())
    controller.restore(task)
    pending = waiting(task)
    api.answer(pending)
    controller.submit_action(
        {
            "kind": "answer",
            "action_id": pending["pending_action"]["action_id"],
            "payload": {"answers": [{"question_id": "q", "text": "parks"}]},
        }
    )
    rejected = api.calls[-1][2]
    api.answer(
        rejection("insufficient_credits", "billing", "forbidden", "Not enough credits"),
        403,
    )
    assert "action_intent" not in controller.state
    assert api.calls[-1][0] == "GET" and controller.polling.task_id == task
    assert (
        journal.load(project_identity(project), "account-a")["analysis"][
            "last_rejection"
        ]["request"]
        == rejected
    )
    controller.pause()
    recovered = AnalysisController(
        api, journal=journal, project=project, scheduler=Clock()
    )
    try:
        recovered.restore()
        assert api.calls[-1][0] == "GET"
        recovered.cancel()
        assert api.calls[-1][2]["kind"] == "cancel"
        assert api.calls[-1][2]["request_id"] != rejected["request_id"]
        api.answer(state(task, 3, "cancelled"))
        assert recovered.state["latest"]["status"] == "cancelled"
    finally:
        recovered.close()
        sip.delete(recovered)


@pytest.mark.parametrize("operation", ["start", "action"])
@pytest.mark.parametrize("failure", ["network", "restart", "legacy"])
def test_validation_after_uncertain_delivery_preserves_original_intent(
    setup, operation, failure
):
    controller, api, clock, journal, project = setup
    if operation == "start":
        controller.start(17, str(uuid4()), "parks", {})
    else:
        task = str(uuid4())
        controller.restore(task)
        pending = waiting(task)
        api.answer(pending)
        controller.submit_action(
            {
                "kind": "answer",
                "action_id": pending["pending_action"]["action_id"],
                "payload": {},
            }
        )
    original = api.calls[-1][2]
    intent_key = operation + "_intent"
    if failure == "network":
        api.answer(status=0, error="network_error")
        controller.restore()
        recovered = controller
    else:
        controller.pause()
        if failure == "legacy":
            saved = journal.load(project_identity(project), "account-a")["analysis"]
            saved.pop(operation + "_dispatched", None)
            journal.update_sections(
                project_identity(project), "account-a", analysis=saved
            )
        recovered = AnalysisController(
            api, journal=journal, project=project, scheduler=Clock()
        )
        recovered.restore()
    try:
        assert api.calls[-1][2] == original
        api.answer(rejection("qgis_request_validation"), 400)
        assert recovered.state[intent_key] == original
        recovered.restore()
        assert api.calls[-1][2] == original
    finally:
        if recovered is not controller:
            recovered.close()
            sip.delete(recovered)


@pytest.mark.parametrize("operation", ["start", "action"])
@pytest.mark.parametrize(
    "status, data",
    [
        (403, rejection("permission_denied", "core", "forbidden")),
        (403, rejection("insufficient_credits", "wrong", "forbidden")),
        (400, rejection("unexpected_error")),
        (409, rejection("conflict", "api_gateway", "conflict")),
        (500, rejection("qgis_request_validation")),
    ],
)
def test_arbitrary_error_is_not_proof_of_nonacceptance(setup, operation, status, data):
    controller, api, clock, journal, project = setup
    if operation == "start":
        controller.start(17, str(uuid4()), "parks", {})
    else:
        task = str(uuid4())
        controller.restore(task)
        pending = waiting(task)
        api.answer(pending)
        controller.submit_action(
            {
                "kind": "answer",
                "action_id": pending["pending_action"]["action_id"],
                "payload": {},
            }
        )
    original = api.calls[-1][2]
    api.answer(data, status)
    assert controller.state[operation + "_intent"] == original
    controller.restore()
    assert api.calls[-1][2] == original


@pytest.mark.parametrize("operation", ["start", "action"])
def test_native_rejection_controls_and_notice_survive_restart(
    qgis_app, tmp_path, operation
):
    from types import SimpleNamespace
    from qgis.gui import QgsMapCanvas
    from src.main_tab import OsmParserDialogMain

    project = QgsProject.instance()
    project.clear()
    canvas = QgsMapCanvas()
    session = Session()
    session.set_access_token(token())
    api = Api(session)
    owner = SimpleNamespace(
        session=session,
        api_client=api,
        recovery_journal=RecoveryJournal(tmp_path),
        iface=SimpleNamespace(mapCanvas=lambda: canvas),
    )
    dialogs = [OsmParserDialogMain(osm_parser=owner)]
    try:
        dialog = dialogs[0]
        controller = dialog.analysis_controller
        if operation == "start":
            controller.start(17, str(uuid4()), "x" * 50001, {})
            api.answer(
                rejection("qgis_request_validation", message="Prompt too long"), 400
            )
            assert dialog.go_button.isEnabled()
            assert not dialog.analysis_resume.isEnabled()
            assert "start rejected" in dialog.analysis_stage.text()
        else:
            task = str(uuid4())
            controller.restore(task)
            pending = waiting(task)
            api.answer(pending)
            controller.submit_action(
                {
                    "kind": "answer",
                    "action_id": pending["pending_action"]["action_id"],
                    "payload": {},
                }
            )
            api.answer(
                rejection(
                    "insufficient_credits", "billing", "forbidden", "Not enough credits"
                ),
                403,
            )
            assert dialog.analysis_cancel.isEnabled()
            assert dialog.analysis_actions.isEnabled()
            assert api.calls[-1][0] == "GET"
        dialog.close()
        restored = OsmParserDialogMain(osm_parser=owner)
        dialogs.append(restored)
        if operation == "start":
            assert "rejected" in restored.analysis_rejection.text()
            assert restored.analysis_panel.add_credits.isHidden()
            assert restored.go_button.isEnabled()
            assert not restored.analysis_resume.isEnabled()
        else:
            assert "Not enough credits" in restored.analysis_rejection.text()
            assert not restored.analysis_panel.add_credits.isHidden()
            assert restored.analysis_cancel.isEnabled()
            api.answer(state(task, 3, "completed"))
            assert restored.analysis_controller.state["latest"]["status"] == "completed"
            assert not restored.analysis_cancel.isEnabled()
    finally:
        for dialog in dialogs:
            dialog.close()
            dialog.shutdown()
            sip.delete(dialog)
        sip.delete(canvas)
        sip.delete(session)
        project.clear()


def test_first_action_validation_rejection_releases_only_rejected_body(setup):
    controller, api, clock, journal, project = setup
    task = str(uuid4())
    controller.restore(task)
    pending = waiting(task)
    api.answer(pending)
    controller.submit_action(
        {
            "kind": "answer",
            "action_id": pending["pending_action"]["action_id"],
            "payload": {},
        }
    )
    rejected = api.calls[-1][2]
    api.answer(rejection("qgis_request_validation"), 400)
    assert "action_intent" not in controller.state
    assert controller.state["last_rejection"]["request"] == rejected
    assert api.calls[-1][0] == "GET"
    api.answer(pending)
    controller.submit_action(
        {
            "kind": "answer",
            "action_id": pending["pending_action"]["action_id"],
            "payload": {"answers": [{"question_id": "q", "text": "Corrected"}]},
        }
    )
    assert api.calls[-1][2]["request_id"] != rejected["request_id"]


def test_screening_report_candidates_journal_reconstruction_and_import_handoff(
    setup, monkeypatch
):
    from src.results.controller import ResultController
    from tests.test_result_import import result
    from tests.test_style_conversion import reference

    controller, api, clock, journal, project = setup
    task = str(uuid4())
    report = json.loads(
        (Path(__file__).parent / "fixtures/screening_report.json").read_text()
    )
    manifest = {
        "task_id": task,
        "layers": [{**result(), "task_id": task}],
        "report": report,
    }
    importer = ResultController(api, journal, project)
    deliveries, downloads, notifications = [], [], []
    monkeypatch.setattr(
        importer.downloader, "download_results", lambda *args: downloads.append(args)
    )

    def handoff(value):
        assert (
            journal.load(project_identity(project), "account-a")["analysis"]["results"]
            == manifest
        )
        deliveries.append(value)
        importer.start(value, 17, reference())

    def changed(value):
        if value.get("results"):
            assert (
                journal.load(project_identity(project), "account-a")["analysis"][
                    "results"
                ]
                == manifest
            )
            notifications.append(value)

    controller.results_ready.connect(handoff)
    controller.changed.connect(changed)
    controller.restore(task)
    identity = project_identity(project)
    journal.update_sections(
        identity, "account-a", sync={"phase": "ready"}, datasets={"retained": True}
    )
    api.answer(state(task, status="completed", results_ready=True))
    api.answer(manifest)
    recovered = None
    try:
        assert deliveries == [manifest]
        assert notifications and downloads[0][1]["report"] == report
        assert downloads[0][1]["layers"][0]["dataset_id"] == 71
        saved = journal.load(identity, "account-a")
        assert saved["imports"]["tasks"][task]["status"] == "pending"
        assert saved["sync"] == {"phase": "ready"} and saved["datasets"] == {
            "retained": True
        }
        controller.close()
        recovered = AnalysisController(
            api,
            journal=RecoveryJournal(journal.directory),
            project=project,
            scheduler=Clock(),
        )
        recovered.results_ready.connect(handoff)
        recovered.restore()
        assert deliveries == [manifest, manifest]
        recovered._results()
        api.answer(manifest)
        assert deliveries == [manifest, manifest, manifest]
        assert len(downloads) == 1
        assert journal.load(identity, "account-a")["analysis"]["results"] == manifest
    finally:
        importer.close()
        sip.delete(importer)
        if recovered is not None:
            recovered.close()
            sip.delete(recovered)


@pytest.mark.parametrize(
    "secret",
    [
        "password",
        "token",
        "access_token",
        "api_key",
        "upload_token",
        "authorization",
        "location",
        "upload_url",
    ],
)
@pytest.mark.parametrize("section", ["session", "sync", "analysis", "imports"])
def test_journal_rejects_transport_credentials_outside_report(
    tmp_path, secret, section
):
    journal = RecoveryJournal(tmp_path)
    identity = str(uuid4())
    journal.save(identity, "account-a", {"sync": {"phase": "ready"}})
    with pytest.raises(ValueError, match="cannot be journaled"):
        journal.update_sections(
            identity,
            "account-a",
            **{section: {secret: "https://storage.invalid/opaque-session-token"}},
        )
    assert journal.load(identity, "account-a") == {"sync": {"phase": "ready"}}


def test_journal_report_property_names_are_data_and_not_transport(tmp_path):
    journal = RecoveryJournal(tmp_path)
    identity = str(uuid4())
    report = {
        "properties": {
            "location": "North district",
            "token": "an ordinary attribute",
            "password": "field label",
        }
    }
    state = {"analysis": {"results": {"report": report}}}
    journal.save(identity, "account-a", state)
    assert journal.load(identity, "account-a") == state
