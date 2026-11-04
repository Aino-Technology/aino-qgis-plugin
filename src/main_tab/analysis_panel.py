from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import Qt

from src.analysis.actions import ActionPanel, WrappingLabel, report_text


class AnalysisPanel(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetMinimumSize)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.stage = self._label("Ready — enter your request and press GO.")
        self.notice = self._label("")
        self.rejection = self._label("")
        self.steps = self._label("")
        self.explanation = self._label("")
        self.actions = ActionPanel(self)
        layout.addWidget(self.actions)
        self.report = QtWidgets.QTextBrowser(self)
        self.report.setMinimumHeight(140)
        layout.addWidget(self.report)
        self.import_notice = self._label("")
        self.set_state({})

    def _label(self, text):
        label = WrappingLabel(text, self)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        self.layout().addWidget(label)
        label.setVisible(bool(text))
        return label

    def set_state(self, state):
        latest = state.get("latest") or {}
        pending = latest.get("pending_action") or None
        explanation = pending.get("text", "") if pending else ""
        explanation = explanation if isinstance(explanation, str) else ""
        self.explanation.setText(explanation)
        self.explanation.setVisible(bool(explanation))
        self.actions.set_action(pending, bool(state.get("action_intent")))
        self.actions.setVisible(bool(pending))
        report = report_text((state.get("results") or {}).get("report"))
        summary = latest.get("summary")
        text = report or (summary if isinstance(summary, str) else "")
        self.report.setPlainText(text if text != explanation else "")
        self.report.setVisible(bool(self.report.toPlainText()))

    def set_notice(self, message):
        self.notice.setText(message)
        self.notice.setVisible(bool(message))

    def set_import_state(self, state):
        message = state.get("message", "")
        self.import_notice.setText(message if isinstance(message, str) else "")
        self.import_notice.setVisible(bool(self.import_notice.text()))
