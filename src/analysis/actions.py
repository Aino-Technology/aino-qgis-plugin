import math
import re

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import QUrl, Qt, pyqtSignal
from qgis.PyQt.QtGui import QDesktopServices


def _text(value):
    return value if isinstance(value, str) else ""


def report_text(report):
    """Render known narrative shapes as text without interpreting server HTML."""
    if isinstance(report, str):
        return re.sub(r"(?m)^\s*\{widget:[^{}\n]+\}\s*$", "", report).strip()
    if not isinstance(report, dict):
        return ""
    if not any(
        key in report for key in ("executive_summary", "recommendation", "conclusion")
    ):
        return _text(report.get("text"))
    sections = []
    for key, title in [
        ("executive_summary", "Executive summary"),
        ("recommendation", "Recommendation"),
    ]:
        if _text(report.get(key)):
            sections.append(title + "\n" + report[key])
    for key, title, fields in [
        ("kpi", "Criteria", ("indicator", "threshold")),
        ("top_candidates", "Candidates", ("rank", "location", "score", "key_reason")),
        ("risks", "Risks", ("severity", "risk")),
    ]:
        lines = []
        values = report.get(key)
        for row in values if isinstance(values, list) else []:
            if isinstance(row, dict):
                parts = [
                    str(row[field])
                    for field in fields
                    if isinstance(row.get(field), (str, int, float))
                    and not isinstance(row[field], bool)
                ]
                if parts:
                    lines.append(" • " + " — ".join(parts))
        if lines:
            sections.append(title + "\n" + "\n".join(lines))
    if _text(report.get("conclusion")):
        sections.append("Conclusion\n" + report["conclusion"])
    method = report.get("methodology")
    if isinstance(method, dict):
        lines = []
        for key, title in [("layers", "Layers"), ("filters", "Filters")]:
            values = method.get(key)
            if isinstance(values, list):
                texts = [value for value in values if isinstance(value, str)]
                if texts:
                    lines.append(title + ": " + "; ".join(texts))
        if _text(method.get("score_formula")):
            lines.append("Score formula: " + method["score_formula"])
        if lines:
            sections.append("Methodology\n" + "\n".join(lines))
    if report.get("parcels_available") is False:
        sections.append(
            "Parcel data unavailable; parcel-dependent criteria are not verified.\n"
            + _text(report.get("parcels_unavailable_reason"))
        )
    return "\n\n".join(sections)


class ActionPanel(QtWidgets.QWidget):
    submitted = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.layout = QtWidgets.QVBoxLayout(self)
        self.pending = None
        self.rows = []
        self.groups = []
        self.blocked = False
        self.error = None
        self.submit_button = None

    def _label(self, text):
        label = QtWidgets.QLabel(text, self)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        self.layout.addWidget(label)
        return label

    def set_action(self, pending, busy=False):
        if self.pending == pending:
            self.setEnabled(not busy)
            return
        while self.layout.count():
            item = self.layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.pending = pending
        for group in self.groups:
            group.deleteLater()
        self.groups = []
        self.rows = []
        self.blocked = False
        self.submit_button = None
        self.error = None
        self.setEnabled(not busy)
        if not pending:
            return
        if isinstance(pending.get("text"), str):
            self._label(pending["text"])
        if pending.get("expires_at"):
            self._label("Server deadline: " + str(pending["expires_at"]))
        if pending.get("kind") == "confirm_sources":
            self._label(
                "Confirm all listed sources. Rates are per unit; total usage and final charge are not estimated or locked here."
            )
            info = {
                row.get("tag"): row
                for row in pending.get("source_info", [])
                if isinstance(row, dict)
            }
            for tag in pending.get("sources", []):
                row = info.get(tag, {})
                rate, unit = row.get("credits_per_unit"), row.get("unit")
                known = (
                    type(rate) in {int, float}
                    and math.isfinite(rate)
                    and rate >= 0
                    and isinstance(unit, str)
                    and unit
                )
                self._label(
                    f"{tag}: {rate:g} credits / {unit}"
                    if known
                    else f"{tag}: price unavailable"
                )
        for item in pending.get("items", []):
            if (
                not isinstance(item, dict)
                or not item.get("id")
                or not isinstance(item.get("question"), str)
            ):
                self.blocked = True
                continue
            if _text(item.get("header")):
                self._label(item["header"])
            self._label(item["question"])
            group = QtWidgets.QButtonGroup(self)
            self.groups.append(group)
            group.setExclusive(not item.get("multi_select", False))
            choices = []
            for option in item.get("options", []):
                button = (
                    QtWidgets.QCheckBox(self)
                    if item.get("multi_select")
                    else QtWidgets.QRadioButton(self)
                )
                label_text = _text(option.get("label"))
                button.setAccessibleName(label_text)
                group.addButton(button)
                row = QtWidgets.QWidget(self)
                row_layout = QtWidgets.QHBoxLayout(row)
                label = QtWidgets.QLabel(label_text, row)
                label.setTextFormat(Qt.TextFormat.PlainText)
                label.setWordWrap(True)
                row_layout.addWidget(button)
                row_layout.addWidget(label, 1)
                self.layout.addWidget(row)
                if _text(option.get("description")):
                    self._label(option["description"])
                hint = option.get("action")
                if hint in {"draw_polygon", "draw_radius", "use_viewport"}:
                    self.blocked = True
                    self._label(
                        "This question requests a new area, but this analysis has a fixed area. Open Aino or cancel and prepare a new analysis."
                    )
                elif hint == "contact_us":
                    button.clicked.connect(
                        lambda checked: QDesktopServices.openUrl(
                            QUrl("mailto:hello@aino.world")
                        )
                        if checked
                        else None
                    )
                choices.append((option.get("id"), button))
            text = None
            if item.get("allow_free_text", True):
                text = QtWidgets.QPlainTextEdit(self)
                text.setPlaceholderText("Your answer or requested changes")
                text.setMaximumHeight(110)
                self.layout.addWidget(text)
            self.rows.append((item, choices, text))
        self.error = self._label("")
        labels = {
            "answer": "Submit answers",
            "approve_plan": "Submit plan choice" if self.rows else "Approve plan",
            "confirm_sources": "Confirm all sources",
        }
        self.submit_button = QtWidgets.QPushButton(
            labels.get(pending.get("kind"), "Submit"), self
        )
        self.submit_button.setEnabled(not self.blocked)
        self.submit_button.clicked.connect(self._submit)
        self.layout.addWidget(self.submit_button)

    def action(self):
        if not self.pending or self.blocked:
            raise ValueError("This question cannot be submitted here")
        answers = []
        for item, choices, editor in self.rows:
            selected = [key for key, button in choices if button.isChecked()]
            text = editor.toPlainText().strip() if editor else ""
            if not selected and not text:
                raise ValueError("Answer every question")
            if (
                self.pending["kind"] == "approve_plan"
                and "edit" in selected
                and not text
            ):
                raise ValueError("Describe the changes before choosing Edit")
            answers.append(
                {
                    "question_id": item["id"],
                    "option_ids": selected,
                    "text": text or None,
                }
            )
        return {
            "kind": self.pending["kind"],
            "action_id": self.pending["action_id"],
            "payload": {"answers": answers} if answers else {},
        }

    def _submit(self):
        try:
            self.submitted.emit(self.action())
        except ValueError as error:
            self.error.setText(str(error))
