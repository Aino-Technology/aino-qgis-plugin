import copy
from uuid import uuid4

import pytest
from qgis.core import QgsProject
from qgis.PyQt import sip
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QLabel

from src.main_tab import OsmParserDialogMain
from tests.test_analysis_controller import state
from tests.test_api_client import token
from tests.test_request_controller import ready
from tests.test_request_controller import request_dialog as _request_dialog

request_dialog = _request_dialog


@pytest.mark.parametrize(
    "mode, guidance",
    [(1, "Use current view"), (2, "boundary layer"), (3, "Pick on map")],
)
def test_manual_area_validation_remains_actionable(request_dialog, mode, guidance):
    dialog, _, api = request_dialog
    dialog.area_mode.setCurrentIndex(mode)
    dialog.send_osm_request("parks")
    assert not api.calls
    displayed = "\n".join(
        label.text() for label in dialog.analysis_panel.findChildren(QLabel)
    )
    assert guidance in displayed, displayed
    assert dialog.currentWidget() is dialog.inputs_tab


@pytest.mark.parametrize("failure", ["http", "invalid", "save"])
def test_failed_final_manifest_has_enabled_retry(
    request_dialog, wait_for, monkeypatch, failure
):
    dialog, _, api = request_dialog
    dialog.send_osm_request("parks in Berlin")
    wait_for(lambda: bool(api.calls))
    ready(api)
    task = str(uuid4())
    api.answer(
        {"task_id": task, "project_id": 17, "project_url": "https://example.invalid/17"}
    )
    api.answer(state(task, status="completed", results_ready=True))
    assert api.calls[-1][1].endswith("/results")
    before = copy.deepcopy(dialog.analysis_controller.state)
    original_save = dialog.analysis_controller._save
    if failure == "http":
        api.answer(status=503, error="network_error")
    elif failure == "invalid":
        api.answer({"task_id": "wrong", "layers": []})
    else:

        def unavailable():
            raise OSError("SQL traceback secret charge-id")

        monkeypatch.setattr(dialog.analysis_controller, "_save", unavailable)
        api.answer({"task_id": task, "layers": []})
        monkeypatch.setattr(dialog.analysis_controller, "_save", original_save)
    assert dialog.analysis_resume.isEnabled(), dialog.analysis_panel.notice.text()
    count = len(api.calls)
    dialog.analysis_resume.click()
    assert [call[:3] for call in api.calls[count:]] == [
        ("GET", f"/integrations/qgis/analyses/{task}/results", None)
    ]
    dialog.analysis_resume.click()
    assert len(api.calls) == count + 1
    api.answer({"task_id": task, "layers": []})
    assert dialog.analysis_controller.state["results"]["task_id"] == task
    assert dialog.analysis_controller.state["start_intent"] == before["start_intent"]
    assert sum(c[1] == "/integrations/qgis/analyses" for c in api.calls) == 1


def test_unsubmitted_layer_choice_survives_plugin_reopen(request_dialog):
    dialog, layer, _ = request_dialog
    dialog.input_layers.item(0).setCheckState(Qt.CheckState.Checked)
    assert dialog.selected_input_ids() == [layer.id()]
    dialog.shutdown()
    reopened = OsmParserDialogMain(osm_parser=dialog.main_ui)
    try:
        assert reopened.selected_input_ids() == [layer.id()]
    finally:
        reopened.shutdown()
        sip.delete(reopened)


@pytest.mark.parametrize("accepted", [False, True])
def test_explicit_empty_draft_wins_over_accepted_inputs(
    request_dialog, accepted, wait_for
):
    dialog, layer, api = request_dialog
    dialog.input_layers.item(0).setCheckState(Qt.CheckState.Checked)
    if accepted:
        dialog.send_osm_request("parks")
    frozen = copy.deepcopy(dialog.request_controller.state)
    dialog.input_layers.item(0).setCheckState(Qt.CheckState.Unchecked)
    assert dialog.request_controller.state == frozen
    if accepted:
        wait_for(lambda: bool(api.calls))
    dialog.shutdown()
    reopened = OsmParserDialogMain(osm_parser=dialog.main_ui)
    try:
        assert reopened.selected_input_ids() == []
        if accepted:
            assert reopened.request_controller.state["layer_ids"] == [layer.id()]
    finally:
        reopened.shutdown()
        sip.delete(reopened)


def test_drafts_isolate_accounts_and_projects(request_dialog):
    dialog, layer, api = request_dialog
    dialog.input_layers.item(0).setCheckState(Qt.CheckState.Checked)
    api.session.set_access_token(token("account-b"))
    assert dialog.selected_input_ids() == []
    api.session.set_access_token(token("account-a"))
    assert dialog.selected_input_ids() == [layer.id()]
    QgsProject.instance().clear()
    assert dialog.selected_input_ids() == []


def test_saved_project_restores_draft_and_ignores_new_removed_layers(
    request_dialog, tmp_path
):
    from tests.test_layer_snapshot import layer_with_values

    dialog, layer, _ = request_dialog
    project = QgsProject.instance()
    dialog.input_layers.item(0).setCheckState(Qt.CheckState.Checked)
    selected = layer.id()
    added = layer_with_values()
    project.addMapLayer(added)
    assert dialog.selected_input_ids() == [selected]
    filename = str(tmp_path / "draft.qgs")
    assert project.write(filename)
    project.clear()
    assert project.read(filename)
    assert dialog.selected_input_ids() == [selected]
    project.removeMapLayer(selected)
    dialog._restore_draft_inputs()
    assert dialog.selected_input_ids() == []
