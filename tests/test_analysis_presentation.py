import pytest

from src.analysis.presentation import present_analysis, present_step


@pytest.mark.parametrize(
    "status,label",
    [
        ("queued", "Waiting to start"),
        ("planning", "Preparing analysis"),
        ("running", "Analysis in progress"),
        ("awaiting_input", "Waiting for your answer"),
        ("awaiting_confirmation", "Confirmation needed"),
        ("completed", "Analysis finished"),
        ("failed", "Could not complete the analysis"),
        ("cancelled", "Analysis cancelled"),
        ("new_enum", "Updating status"),
    ],
)
def test_lifecycle(status, label):
    assert present_analysis({"latest": {"status": status}}, {}, {})["headline"] == label


@pytest.mark.parametrize(
    "status,label",
    [
        ("to_do", "Waiting"),
        ("in_progress", "In progress"),
        ("completed", "Done"),
        ("failed", "Could not complete"),
        ("cancelled", "Cancelled"),
        ("paused", "Paused"),
        ("future", "Updating status"),
    ],
)
def test_steps(status, label):
    step = present_step(
        {
            "title": "Parks",
            "status": status,
            "error": {"code": "source_unavailable", "message": "Source unavailable"},
        }
    )
    assert step["status"] == label
    assert step["error"] == "Source unavailable"


def test_area_and_capture():
    assert (
        present_analysis(
            {"latest": {"status": "planning", "phase": "resolving_area"}}, {}, {}
        )["headline"]
        == "Finding the analysis area"
    )
    assert (
        present_analysis({}, {"phase": "capturing"}, {})["headline"]
        == "Preparing layers"
    )
    assert (
        present_analysis({}, {"phase": "synchronizing"}, {})["headline"]
        == "Uploading layers"
    )


def test_quality_import_independence():
    state = {
        "latest": {
            "status": "completed",
            "outcome": {
                "quality": "partial",
                "issues": [
                    {"message": "Road data unavailable", "code": "source_unavailable"}
                ],
            },
        },
        "results": {"layers": [{"name": "Parks", "purpose": "result"}]},
    }
    state["results"]["outcome"] = state["latest"]["outcome"]
    shown = present_analysis(
        state, {}, {"status": "downloading", "warnings": ["Style simplified"]}
    )
    assert shown["headline"] == "Analysis finished"
    assert "Partial result" in shown["outcome_message"]
    assert "Parks" in shown["outcome_message"]
    assert "Road data unavailable" in shown["outcome_message"]
    assert any("Downloading" in n for n in shown["notices"])
    assert any("Style" in n for n in shown["notices"])
    assert (
        "Completeness unknown"
        in present_analysis(
            {"latest": {"status": "completed"}, "results": {"layers": []}}, {}, {}
        )["outcome_message"]
    )


def test_raw_error_is_not_user_copy():
    shown = present_step(
        {"status": "failed", "error": "SELECT secret FROM billing; traceback"}
    )
    assert "SELECT" not in shown["error"]


def test_panel_has_one_primary_headline(qgis_app):
    from src.main_tab.analysis_panel import AnalysisPanel

    panel = AnalysisPanel()
    panel.set_state({"latest": {"status": "completed"}, "results": {"layers": []}})
    panel.set_import_state({"status": "downloading"})
    assert panel.stage.text() == "Analysis finished"
    assert "Downloading" in panel.import_notice.text()
    panel.deleteLater()
    from qgis.PyQt.QtCore import QCoreApplication, QEvent

    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.mark.parametrize(
    "scenario,quality,layers,issues",
    [
        ("source_success", "complete", ["Parks"], []),
        ("confirmed_zero", "complete", [], []),
        ("partial", "partial", ["Parks"], ["Road source unavailable"]),
        ("recovered_replacement", "complete", ["Alternative source"], []),
        (
            "all_sources_failed_h3",
            "unavailable",
            ["H3 grid", "Grid derivative"],
            ["Requested sources unavailable"],
        ),
        ("grid_only", "complete", ["H3 grid"], []),
        ("consumed_snapshot", "complete", ["Input-backed result"], []),
        ("legacy", "unknown", ["Legacy layer"], []),
    ],
)
def test_outcome_contract_scenarios(scenario, quality, layers, issues):
    manifest = {"layers": [{"name": name} for name in layers]}
    if scenario != "legacy":
        manifest["outcome"] = {
            "quality": quality,
            "message": "Analysis finished",
            "issues": [{"message": message} for message in issues],
        }
    shown = present_analysis(
        {"latest": {"status": "completed"}, "results": manifest},
        {},
        {"status": "complete", "imported_names": layers},
    )
    from src.analysis.presentation import QUALITY

    assert QUALITY[quality] in shown["outcome_message"]
    for text in layers + issues:
        assert text in shown["outcome_message"]
    assert shown["headline"] == "Analysis finished"


def test_unknown_status_survives_polling_and_continues(qgis_app):
    from src.analysis.polling import PollingController
    from src.session import Session
    from tests.test_analysis_controller import Api, Clock, state

    session = Session()
    api = Api(session)
    scheduler = Clock()
    poller = PollingController(api, scheduler=scheduler)
    observed = []
    poller.updated.connect(observed.append)
    poller.watch("task")
    api.answer(state("task", status="future_state"))
    assert (
        present_analysis({"latest": observed[0]}, {}, {})["headline"]
        == "Updating status"
    )
    assert poller.task_id == "task"
    assert scheduler.delay == 4
    poller.stop()
    poller.deleteLater()
    session.deleteLater()


def test_panel_ignores_previous_task_import(qgis_app):
    from qgis.PyQt.QtCore import QCoreApplication, QEvent

    from src.main_tab.analysis_panel import AnalysisPanel

    panel = AnalysisPanel()
    panel.set_state({"task_id": "new", "latest": {"status": "running"}})
    panel.set_import_state(
        {"task_id": "old", "status": "failed", "details": "old failure"}
    )
    assert panel.stage.text() == "Analysis in progress"
    assert not panel.import_notice.text()
    panel.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.mark.parametrize("status", [None, "", "  ", 42, []])
def test_invalid_status_types_are_rejected(status):
    from src.analysis.polling import valid_state

    assert not valid_state(
        {"task_id": "task", "revision": 1, "steps": [], "status": status}, "task"
    )


def test_legacy_manifest_does_not_inherit_unmatched_state_quality():
    shown = present_analysis(
        {
            "latest": {"status": "completed", "outcome": {"quality": "complete"}},
            "results": {"layers": []},
        },
        {},
        {},
    )
    assert "Completeness unknown" in shown["outcome_message"]


@pytest.mark.parametrize(
    "raw",
    [
        "Traceback (most recent call last): private worker failed",
        "SELECT secret FROM billing WHERE token='private'",
        "charge_id=7794d4e5-f1fa-4b66-bc72-3a1447ac070e",
    ],
)
@pytest.mark.parametrize(
    "origin", ["analysis", "import", "recovery", "diagnostic", "unknown_enum"]
)
def test_expanded_details_exclude_raw_failures(qgis_app, raw, origin):
    from qgis.PyQt.QtCore import QCoreApplication, QEvent

    from src.main_tab.analysis_panel import AnalysisPanel

    panel = AnalysisPanel()
    state = {
        "latest": {
            "status": "failed",
            "steps": [
                {
                    "title": "Fetch data",
                    "status": "failed",
                    "error": {
                        "code": "source_unavailable",
                        "message": "A data source could not be reached.",
                    },
                }
            ],
        }
    }
    if origin == "analysis":
        state["latest"]["error"] = raw
        state["last_rejection"] = {"error_code": raw}
    if origin == "unknown_enum":
        state["latest"].update(status=raw, phase=raw)
        state["latest"]["steps"].append(
            {"title": "Next step", "status": raw, "error": {"code": raw}}
        )
    panel.set_state(state)
    if origin == "import":
        panel.set_import_state({"status": "failed", "details": raw})
    elif origin == "recovery":
        panel.set_error(raw)
    elif origin == "diagnostic":
        panel.set_diagnostic(raw)
    panel.show()
    panel.details_button.setChecked(True)
    qgis_app.processEvents()
    assert panel.details.isVisible()
    assert raw not in "\n".join(
        widget.text()
        for widget in [
            panel.details,
            panel.stage,
            panel.steps,
            panel.notice,
            panel.import_notice,
        ]
    )
    assert "source_unavailable" in panel.details.text()
    assert "A data source could not be reached." in panel.steps.text()
    panel.close()
    panel.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_expanded_details_accept_only_vetted_metadata(qgis_app):
    from qgis.PyQt.QtCore import QCoreApplication, QEvent

    from src.main_tab.analysis_panel import AnalysisPanel

    panel = AnalysisPanel()
    panel.set_diagnostic(
        {
            "code": "start_response_unavailable",
            "http_status": 503,
            "message": "SELECT secret",
            "origin": "charge-private",
        }
    )
    panel.details_button.setChecked(True)
    assert "start_response_unavailable" in panel.details.text()
    assert "HTTP 503" in panel.details.text()
    assert (
        "SELECT" not in panel.details.text()
        and "charge-private" not in panel.details.text()
    )
    panel.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.mark.parametrize("failure", ["api", "recovery"])
def test_controller_failures_deliver_safe_expanded_details(
    qgis_app, tmp_path, monkeypatch, failure
):
    from uuid import uuid4

    from qgis.core import QgsProject
    from qgis.PyQt import sip
    from qgis.PyQt.QtCore import QCoreApplication, QEvent

    from src.analysis.controller import AnalysisController
    from src.main_tab.analysis_panel import AnalysisPanel
    from src.project_sync.recovery import RecoveryJournal
    from src.session import Session
    from tests.test_analysis_controller import Api, Clock
    from tests.test_api_client import token

    raw = "Traceback: SELECT secret; charge_id=7794d4e5-f1fa-4b66-bc72-3a1447ac070e"
    session = Session()
    session.set_access_token(token())
    api = Api(session)
    project = QgsProject()
    controller = AnalysisController(
        api, RecoveryJournal(tmp_path), project, scheduler=Clock()
    )
    panel = AnalysisPanel()
    controller.changed.connect(panel.set_state)
    controller.status.connect(panel.set_notice)
    controller.diagnostic.connect(panel.set_diagnostic)
    controller.start(17, str(uuid4()), "Find parks")
    if failure == "api":
        api.answer({"message": raw}, status=503)
        expected = "start_response_unavailable (HTTP 503)"
    else:

        def fail_save():
            raise OSError(raw)

        monkeypatch.setattr(controller, "_save", fail_save)
        api.answer(
            {
                "task_id": str(uuid4()),
                "project_id": 17,
                "project_url": "https://example.invalid/17",
            }
        )
        expected = "recovery_unavailable"
    panel.show()
    panel.details_button.setChecked(True)
    qgis_app.processEvents()
    assert expected in panel.details.text()
    assert raw not in panel.details.text() + panel.notice.text()
    controller.close()
    panel.close()
    panel.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    sip.delete(controller)
    project.clear()
    sip.delete(project)
    sip.delete(session)
