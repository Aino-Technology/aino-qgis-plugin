from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import Qt

from src.analysis.actions import ActionPanel, WrappingLabel, report_text
from src.analysis.presentation import diagnostic_text, present_analysis


class AnalysisPanel(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(
            "QPushButton { background: #111827; color: white; border: 1px solid #111827; border-radius: 6px; padding: 7px; }"
            "QPushButton:disabled { background: #E5E7EB; color: #667085; border-color: #E5E7EB; }"
            "QPushButton:focus { border: 2px solid #315EFB; }"
        )
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetMinimumSize)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.state = {}
        self.request_state = {}
        self.import_state = {}
        self.diagnostic = ""
        self.stage = self._label("Ready — enter your request and press GO.")
        self.notice = self._label("")
        self.outcome = self._label("")
        self.rejection = self._label("")
        self.steps = self._label("")
        self.explanation = self._label("")
        self.actions = ActionPanel(self)
        layout.addWidget(self.actions)
        self.report = QtWidgets.QTextBrowser(self)
        self.report.setMinimumHeight(140)
        layout.addWidget(self.report)
        self.save_notice = self._label("")
        self.import_notice = self._label("")
        self.details_button = QtWidgets.QPushButton("Technical details", self)
        self.details_button.setCheckable(True)
        layout.addWidget(self.details_button)
        self.details = self._label("")
        self.details_button.toggled.connect(self.details.setVisible)
        self.set_state({})

    def _label(self, text):
        label = WrappingLabel(text, self)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        self.layout().addWidget(label)
        label.setVisible(bool(text))
        return label

    def set_state(self, state):
        if state.get("task_id") != self.state.get("task_id") or not state:
            self.diagnostic = ""
            self.import_state = {}
        self.state = state
        if not state or state.get("results") is None:
            self.import_state = {}
        self._render()
        latest = state.get("latest") or {}
        pending = latest.get("pending_action") or None
        explanation = pending.get("text", "") if pending else ""
        explanation = explanation if isinstance(explanation, str) else ""
        self.explanation.setText(explanation)
        self.explanation.setVisible(bool(explanation))
        error = latest.get("error")
        if error:
            self.set_notice(
                "Analysis is waiting for additional credits"
                if error == "Analysis is waiting for additional credits"
                else "Analysis needs attention. See the steps and technical details."
            )
        else:
            self.set_notice("")
        self.actions.set_action(pending, bool(state.get("action_intent")))
        self.actions.setVisible(bool(pending))
        report = report_text((state.get("results") or {}).get("report"))
        summary = latest.get("summary")
        text = report or (summary if isinstance(summary, str) else "")
        self.report.setPlainText(text if text != explanation else "")
        self.report.setVisible(bool(self.report.toPlainText()))

    def set_error(self, message):
        self.set_diagnostic({"code": "request_failed"})
        self.set_notice(
            "Request needs attention. Review the technical details and retry."
        )

    def set_notice(self, message):
        self.notice.setText(message)
        self.notice.setVisible(bool(message))

    def set_diagnostic(self, message):
        self.diagnostic = diagnostic_text(message)
        self._render()

    def set_import_state(self, state):
        if (
            state.get("task_id")
            and self.state.get("task_id")
            and state["task_id"] != self.state["task_id"]
        ):
            return
        self.import_state = state
        self._render()

    def set_request_state(self, state):
        self.request_state = state
        self._render()

    def _render(self):
        shown = present_analysis(self.state, self.request_state, self.import_state)
        self.stage.setText(shown["headline"])
        steps = []
        for step in shown["steps"]:
            line = step["title"] + ": " + step["status"]
            if step["error"]:
                line += "\n" + step["error"]
            steps.append(line)
        self.steps.setText("\n\n".join(steps))
        self.steps.setVisible(bool(steps))
        self.outcome.setText(shown["outcome_message"])
        self.outcome.setVisible(bool(shown["outcome_message"]))
        self.import_notice.setText("\n".join(shown["notices"]))
        self.import_notice.setVisible(bool(shown["notices"]))
        details = [shown["details"], self.diagnostic] + [
            step["details"] for step in shown["steps"]
        ]
        details.append(diagnostic_text(self.import_state.get("diagnostic")))
        self.details.setText("\n".join(details))
        self.details.setVisible(self.details_button.isChecked())
