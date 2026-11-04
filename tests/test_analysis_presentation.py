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
