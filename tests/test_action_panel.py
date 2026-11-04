from copy import deepcopy

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtTest import QTest

from src.analysis.actions import ActionPanel
from src.main_tab.analysis_panel import AnalysisPanel


def pending(action_id="first"):
    return {
        "kind": "answer",
        "action_id": action_id,
        "text": "Aino explanation",
        "items": [
            {
                "id": "route",
                "header": "Route",
                "question": "Which route?",
                "options": [
                    {
                        "id": "walk-real",
                        "label": "Walk",
                        "description": "Use walking paths",
                    },
                    {"id": "drive-real", "label": "Drive", "description": "Use roads"},
                ],
            }
        ],
    }


def test_separate_explanation_and_exact_duplicate(qgis_app):
    panel = AnalysisPanel()
    action = pending()
    panel.set_state({"latest": {"pending_action": action, "summary": action["text"]}})
    assert panel.explanation.text() == action["text"]
    assert action["text"] not in [
        label.text() for label in panel.actions.findChildren(QtWidgets.QLabel)
    ]
    assert not panel.report.toPlainText()
    panel.set_state(
        {"latest": {"pending_action": action, "summary": action["text"] + " More."}}
    )
    assert "More." in panel.report.toPlainText()
    panel.deleteLater()


def test_revision_retains_input_and_new_identity_clears(qgis_app):
    panel = ActionPanel()
    action = pending()
    panel.set_action(action)
    panel.rows[0][1][0][1].setChecked(True)
    panel.rows[0][2].setPlainText("Keep my answer")
    panel.show()
    panel.activateWindow()
    qgis_app.processEvents()
    editor = panel.rows[0][2]
    editor.setFocus()
    cursor = editor.textCursor()
    cursor.setPosition(3)
    editor.setTextCursor(cursor)
    revised = deepcopy(action)
    revised.update(revision=2, text="Revised explanation", expires_at="tomorrow")
    panel.set_action(revised)
    assert editor.hasFocus()
    panel.set_action(revised, busy=True)
    assert not panel.isEnabled()
    assert panel.pending == revised
    assert panel.rows[0][2] is editor
    assert editor.textCursor().position() == 3
    panel.set_action(revised)
    assert panel.action()["payload"]["answers"][0] == {
        "question_id": "route",
        "option_ids": ["walk-real"],
        "text": "Keep my answer",
    }
    panel.set_action(pending("second"))
    assert not panel.rows[0][1][0][1].isChecked()
    assert not panel.rows[0][2].toPlainText()
    panel.deleteLater()


def test_description_click_and_keyboard_select_real_ids(qgis_app):
    panel = ActionPanel()
    panel.set_action(pending())
    panel.show()
    qgis_app.processEvents()
    description = next(
        label
        for label in panel.findChildren(QtWidgets.QLabel)
        if label.text() == "Use roads"
    )
    QTest.mouseClick(description, Qt.MouseButton.LeftButton)
    assert panel.action()["payload"]["answers"][0]["option_ids"] == ["drive-real"]
    first = panel.rows[0][1][0][1]
    first.setFocus()
    QTest.keyClick(first, Qt.Key.Key_Space)
    assert panel.action()["payload"]["answers"][0]["option_ids"] == ["walk-real"]
    panel.close()
    panel.deleteLater()


def test_area_question_uses_ordinary_form_and_blocks_unsupported_tools(qgis_app):
    import pytest

    panel = ActionPanel()
    action = pending()
    action["items"][0]["question"] = "Which Springfield did you mean?"
    panel.set_action(action)
    panel.rows[0][2].setPlainText("Springfield, Illinois")
    assert panel.action()["payload"]["answers"][0]["text"] == "Springfield, Illinois"
    action = pending("unsupported")
    action["items"][0]["options"][0]["action"] = "draw_polygon"
    panel.set_action(action)
    assert not panel.submit_button.isEnabled()
    with pytest.raises(ValueError, match="cannot be submitted"):
        panel.action()
    panel.set_action(None)
    qgis_app.processEvents()
    panel.set_action(None)
    panel.deleteLater()


def test_busy_form_does_not_emit_submission(qgis_app):
    panel = ActionPanel()
    panel.set_action(pending())
    panel.rows[0][1][0][1].setChecked(True)
    sent = []
    panel.submitted.connect(sent.append)
    panel.set_action(pending(), busy=True)
    panel.submit_button.click()
    assert not sent
    panel.set_action(pending())
    panel.submit_button.click()
    assert sent[0]["action_id"] == "first"
    panel.deleteLater()


def test_long_card_descriptions_fit_at_narrow_width(qgis_app):
    from src.analysis.actions import WrappingLabel

    panel = ActionPanel()
    action = pending()
    action["items"][0]["options"][0]["description"] = (
        "A long option description with complete details. " * 15
    )
    panel.set_action(action)
    panel.resize(280, 1200)
    panel.show()
    for _ in range(5):
        qgis_app.processEvents()
    labels = panel.findChildren(WrappingLabel)
    for label in labels:
        assert label.height() >= label.heightForWidth(label.width())
    panel.close()
    panel.deleteLater()


def test_explanation_shrinks_after_long_action(qgis_app):
    panel = AnalysisPanel()
    panel.resize(320, 800)
    panel.show()
    action = pending()
    action["text"] = "A long explanation. " * 30
    panel.set_state({"latest": {"pending_action": action}})
    for _ in range(5):
        qgis_app.processEvents()
    tall = panel.explanation.minimumHeight()
    panel.set_state({"latest": {"pending_action": pending("next")}})
    for _ in range(5):
        qgis_app.processEvents()
    assert panel.explanation.minimumHeight() < tall
    assert panel.explanation.height() < tall
    panel.close()
    panel.deleteLater()
