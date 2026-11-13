import hashlib
import json
import os
import tempfile

from qgis.core import QgsProject
from qgis.PyQt import QtWidgets, uic
from qgis.PyQt.QtCore import Qt, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import QMessageBox

from src.analysis.actions import report_text
from src.analysis.controller import AnalysisController
from src.analysis.polling import TERMINAL
from src.analysis.request_capture import RequestCapture
from src.analysis.request_controller import RequestController
from src.project_sync.controller import SyncController
from src.project_sync.layers import SnapshotCapture
from src.project_sync.styles import RenderReference
from src.results.controller import ResultController
from src import ADD_CREDITS_URL

from .analysis_panel import CREDITS_PAUSE_ERROR, AnalysisPanel
from .area_panel import AreaPanel, ManualAreaRequired
from .helpers import (
    analysis_limits_text,
    credits_left,
    update_days_label,
    update_scroll_area,
)

_QT_POINTING_HAND_CURSOR = getattr(getattr(Qt, "CursorShape", Qt), "PointingHandCursor")
_QT_TOP_RIGHT_CORNER = getattr(getattr(Qt, "Corner", Qt), "TopRightCorner")

FORM_CLASS_main, _ = uic.loadUiType(
    os.path.join(os.path.dirname(__file__), "./ui_files/main.ui")
)


class AccountEmailLabel(QtWidgets.QLabel):
    def setText(self, text):
        super().setText(text)
        self.setMinimumHeight(max(0, self.heightForWidth(self.width())))

    def resizeEvent(self, event):
        self.setMinimumHeight(max(0, self.heightForWidth(event.size().width())))
        super().resizeEvent(event)


class OsmParserDialogMain(QtWidgets.QTabWidget, FORM_CLASS_main):
    inputs_prepared = pyqtSignal(dict, list)
    snapshot_ready = pyqtSignal(dict)
    analysis_results_ready = pyqtSignal(dict)

    def __init__(self, parent=None, osm_parser=None):
        super(OsmParserDialogMain, self).__init__(parent)
        self.project_id = None
        self.main_ui = osm_parser
        self.api_client = osm_parser.api_client
        self.session = osm_parser.session
        handles = []
        self._handles = handles
        self._shutdown = False
        self.destroyed.connect(lambda: [handle.abort() for handle in handles])
        self.session.account_changed.connect(self._account_changed)
        self.first_step = True
        self.setupUi(self)
        self.setup_account_controls()
        self._setup_inputs()
        self._setup_sync()
        self._setup_analysis()
        self._setup_request()

        self.go_button.clicked.connect(self.start_osm_thread)
        self.prompt_field.textChanged.connect(self._prompt_changed)
        self.upload_button.clicked.connect(self.upload_process)
        self.currentChanged.connect(lambda _: update_scroll_area(self))
        self.limits_response = None
        self._limit_errors = {}
        self._awaiting_credits = False
        self.subscriptions_response = None
        self.trial_label.setVisible(False)
        self.trial_label.setText("Trial subscription")
        self._setup_request_layout()
        self.prompts_label.setWordWrap(True)
        self.prompts_label.setTextFormat(Qt.TextFormat.PlainText)
        self.prompts_label.setText("Aino checks credits and limits on submission.")

    def _setup_request_layout(self):
        layout = QtWidgets.QVBoxLayout(self.main_tab)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        for label in (
            self.label,
            self.label_3,
            self.prompts_label,
            self.trial_label,
            self.request_status_label,
        ):
            label.setWordWrap(True)
            label.setMinimumWidth(0)
            label.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Ignored,
                QtWidgets.QSizePolicy.Policy.Preferred,
            )
        self.prompt_field.setMinimumHeight(140)
        self.go_button.setMinimumHeight(42)
        self.request_add_credits = QtWidgets.QPushButton("Add credits", self.main_tab)
        self.request_add_credits.setToolTip("Opens Aino in your browser")
        self.request_add_credits.setVisible(False)
        self.request_add_credits.clicked.connect(self._open_add_credits)
        for widget in (
            self.label,
            self.label_3,
            self.trial_label,
            self.prompts_label,
            self.request_add_credits,
            self.prompt_field,
            self.go_button,
            self.request_status_label,
        ):
            layout.addWidget(widget)
        layout.addStretch()
        self.tabBar().setUsesScrollButtons(True)

    def _setup_inputs(self):
        self._capture = None
        self._capture_directory = None
        self._capture_connections = []
        self._capture_generation = 0
        self.prepared_inputs = None
        map_canvas = getattr(getattr(self.main_ui, "iface", None), "mapCanvas", None)
        canvas = map_canvas() if callable(map_canvas) else None
        self.area_panel = AreaPanel(canvas, QgsProject.instance(), self)
        for widget in (
            self.area_label,
            self.area_mode,
            self.center_button,
            self.radius_input,
            self.center_label,
            self.inputs_label,
            self.input_layers,
        ):
            widget.hide()
        self.inputs_layout.insertWidget(0, self.area_panel)
        self.area_mode = self.area_panel.mode
        self.radius_input = self.area_panel.radius
        self.input_layers = self.area_panel.inputs
        self.input_layers.itemChanged.connect(self._save_draft_inputs)
        self.area_panel.selection_changed.connect(self._discard_capture)
        self.preview_inputs_button.clicked.connect(lambda: self.prepare_inputs())
        self.cancel_capture_button.clicked.connect(self._discard_capture)

    def _refresh_input_layers(self, *args):
        self._discard_capture()
        self.area_panel.refresh_layers()

    def selected_input_ids(self):
        return self.area_panel.selected_input_ids()

    def _restore_map_tool(self):
        self.area_panel.cancel_capture()

    def _watch_capture(self, signal):
        signal.connect(self._discard_capture)
        self._capture_connections.append(signal)

    def _discard_capture(self, *args):
        self._capture_generation += 1
        for signal in self._capture_connections:
            try:
                signal.disconnect(self._discard_capture)
            except (TypeError, RuntimeError):
                pass
        self._capture_connections.clear()
        if self._capture is not None:
            self._capture.cancel()
            for stream in self._capture.streams:
                stream.close(remove=True)
            self._capture.deleteLater()
            self._capture = None
        if self._capture_directory is not None:
            self._capture_directory.cleanup()
            self._capture_directory = None
        self.prepared_inputs = None
        if hasattr(self, "sync_inputs_button"):
            self.sync_inputs_button.setEnabled(False)
        self.preview_inputs_button.setEnabled(True)
        self.cancel_capture_button.setEnabled(False)
        self.capture_status.setText(
            "Select optional vector layers. GO prepares them automatically."
        )

    def prepare_inputs(self, directory=None):
        self._discard_capture()
        try:
            canvas = self.main_ui.iface.mapCanvas()
            project = QgsProject.instance()
            ids = self.selected_input_ids()
            layers = [project.mapLayer(layer_id) for layer_id in ids]
            area = self.area_panel.selection() or {}
            reference = RenderReference(canvas.mapSettings())
            if directory is None:
                previews = self.sync_controller.journal.directory / "previews"
                previews.mkdir(parents=True, exist_ok=True)
                self._capture_directory = tempfile.TemporaryDirectory(
                    prefix="aino-inputs-", dir=previews
                )
                directory = self._capture_directory.name
            job = SnapshotCapture(
                project, ids, directory, reference=reference, parent=self
            )
            self._capture = job
            generation = self._capture_generation
            job.finished.connect(
                lambda outputs: self._inputs_finished(generation, area, outputs)
            )
            job.failed.connect(self._inputs_failed)
            job.progress.connect(
                lambda count: self.capture_status.setText(
                    f"Preparing {count} features…"
                )
            )
            for layer in layers:
                for name in [
                    "dataChanged",
                    "repaintRequested",
                    "rendererChanged",
                    "styleChanged",
                    "selectionChanged",
                    "nameChanged",
                    "crsChanged",
                ]:
                    self._watch_capture(getattr(layer, name))
            self._watch_capture(canvas.extentsChanged)
            self._watch_capture(canvas.destinationCrsChanged)
            self._watch_capture(project.layerTreeRoot().visibilityChanged)
            self._watch_capture(project.layerTreeRoot().layerOrderChanged)
            self.preview_inputs_button.setEnabled(False)
            self.cancel_capture_button.setEnabled(True)
            self.capture_status.setText("Preparing input files…")
            job.start()
            return job
        except Exception as error:
            self._inputs_failed(str(error))
            return None

    def _inputs_finished(self, generation, area, outputs):
        if generation != self._capture_generation:
            return
        self.prepared_inputs = (area, outputs)
        warnings = [
            f"{output['name']}: {warning}"
            for output in outputs
            for warning in output["warnings"]
        ]
        message = f"Ready: {len(outputs)} layers, {sum(output['feature_count'] for output in outputs)} features."
        if warnings:
            message += " Style simplifications: " + "; ".join(warnings)
        self.capture_status.setText(message)
        self.capture_status.setToolTip(message)
        self.preview_inputs_button.setEnabled(True)
        self.cancel_capture_button.setEnabled(False)
        if hasattr(self, "sync_inputs_button"):
            self.sync_inputs_button.setEnabled(True)
            self.sync_inputs_button.setText(
                "Continue with style simplifications and sync"
                if warnings
                else "Sync prepared inputs"
            )
        self.inputs_prepared.emit(area, outputs)

    def _inputs_failed(self, message):
        self._discard_capture()
        self.capture_status.setText(f"Cannot prepare inputs: {message}")

    def _setup_sync(self):
        self.sync_controller = SyncController(
            self.api_client,
            journal=getattr(self.main_ui, "recovery_journal", None),
            parent=self,
        )
        self.sync_inputs_button = QtWidgets.QPushButton("Sync prepared inputs", self)
        self.sync_inputs_button.setEnabled(False)
        self.resume_sync_button = QtWidgets.QPushButton("Resume synchronization", self)
        self.relink_button = QtWidgets.QPushButton("Retry existing link", self)
        self.detach_button = QtWidgets.QPushButton("Detach and create a new link", self)
        self.sync_notice = QtWidgets.QLabel(self)
        self.sync_notice.setWordWrap(True)
        self.sync_status = QtWidgets.QLabel(self)
        self.sync_status.setWordWrap(True)
        for widget in (
            self.sync_inputs_button,
            self.resume_sync_button,
            self.relink_button,
            self.detach_button,
            self.sync_notice,
            self.sync_status,
        ):
            self.inputs_layout.addWidget(widget)
        self.sync_inputs_button.clicked.connect(self._sync_prepared)
        self.resume_sync_button.clicked.connect(self._resume_sync)
        self.relink_button.clicked.connect(
            lambda: self.sync_controller.retry_link(QgsProject.instance())
        )
        self.detach_button.clicked.connect(self._detach_sync)
        self.sync_controller.status.connect(self.sync_status.setText)
        self.sync_controller.notice.connect(self._sync_notice)
        self.sync_controller.failed.connect(self._sync_failed)
        self.sync_controller.relink_required.connect(self._sync_relink)
        self.sync_controller.ready.connect(self._sync_ready)
        self.relink_button.hide()
        self.detach_button.hide()
        self.removeTab(self.indexOf(self.upload_tab))
        QgsProject.instance().cleared.connect(self._project_sync_cleared)
        QgsProject.instance().readProject.connect(self._resume_sync)
        self.session.authenticated.connect(self._resume_sync)

    def _sync_prepared(self):
        if self.prepared_inputs is None:
            return
        area, outputs = self.prepared_inputs
        try:
            self.sync_controller.prepare(
                QgsProject.instance(), outputs, None, context={"area": area}
            )
            self.sync_inputs_button.setEnabled(False)
            self._discard_capture()
        except (ValueError, OSError) as error:
            self._sync_failed(str(error))

    def _resume_sync(self):
        if not self.session.account_id or not hasattr(self, "request_controller"):
            return
        try:
            self.request_controller.restore()
        except (ValueError, OSError) as error:
            self._sync_failed(str(error))
        finally:
            self._restore_draft_inputs()

    def _draft_input_key(self):
        account = self.session.account_id
        return (
            "draft_inputs/account_" + hashlib.sha256(account.encode()).hexdigest()
            if account
            else None
        )

    def _save_draft_inputs(self, *args):
        key = self._draft_input_key()
        if key:
            QgsProject.instance().writeEntry(
                "aino", key, json.dumps(self.selected_input_ids())
            )

    def _restore_draft_inputs(self, accepted_fallback=True):
        key = self._draft_input_key()
        value, exists = (
            QgsProject.instance().readEntry("aino", key, "") if key else ("", False)
        )
        try:
            selected = (
                json.loads(value)
                if exists
                else self.request_controller.state.get("layer_ids", [])
                if key and accepted_fallback
                else []
            )
        except (ValueError, TypeError):
            selected = []
        if not isinstance(selected, list):
            selected = []
        self.input_layers.blockSignals(True)
        for index in range(self.input_layers.count()):
            item = self.input_layers.item(index)
            item.setCheckState(
                Qt.CheckState.Checked
                if item.data(Qt.ItemDataRole.UserRole) in selected
                else Qt.CheckState.Unchecked
            )
        self.input_layers.blockSignals(False)
        if exists and selected != self.selected_input_ids():
            self._save_draft_inputs()

    def _sync_notice(self, message):
        self.sync_notice.setText(
            "Save the QGIS project to keep its Aino link."
            if message.startswith("Save the QGIS project")
            else "The saved Aino link needs attention."
            if message
            else ""
        )
        if hasattr(self, "analysis_panel"):
            self.analysis_panel.save_notice.setText(self.sync_notice.text())
            self.analysis_panel.save_notice.setVisible(bool(message))
            self.analysis_panel.set_diagnostic(
                {
                    "code": "project_link_unsaved"
                    if message.startswith("Save the QGIS project")
                    else "project_link_invalid"
                }
                if message
                else {}
            )

    def _sync_failed(self, message):
        self.sync_status.setText(message)

    def _sync_relink(self, message):
        self.sync_status.setText(
            message + " Restore access and retry, or explicitly create a new link."
        )
        self.relink_button.show()
        self.detach_button.show()

    def _detach_sync(self):
        answer = QMessageBox.question(
            self,
            "Create a separate Aino project?",
            "The next synchronization will create a new Aino link. Existing analysis and recovery records will remain with the previous link.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.request_controller.pause()
            self.sync_controller.detach(QgsProject.instance())
            self.analysis_controller.bind(QgsProject.instance())
            self.project_id = None
            self.project_link_text.clear()
            self.relink_button.hide()
            self.detach_button.hide()
            self._discard_capture()
            self.request_controller.restore()
            self.sync_status.setText("Detached. Press GO to create a new link.")

    def _sync_ready(self, snapshot):
        self.project_id = snapshot["project_id"]
        self.project_link_text.setText(
            self.sync_controller.state["binding"]["project_url"]
        )
        self.sync_status.setText("Snapshot ready for analysis.")
        self.detach_button.show()
        self.snapshot_ready.emit(snapshot)

    def _project_sync_cleared(self):
        if hasattr(self, "request_controller"):
            self.request_controller.pause()
        self.sync_controller.pause()
        if hasattr(self, "analysis_controller"):
            self.analysis_controller.bind(QgsProject.instance())
        self.sync_controller.state = None
        self.area_panel.reset()
        self.project_id = None
        self.project_link_text.clear()
        self.sync_status.clear()
        self._resume_sync()

    def upload_process(self):
        self.setCurrentWidget(self.inputs_tab)

    def _setup_analysis(self):
        self.analysis_controller = AnalysisController(
            self.api_client,
            journal=self.sync_controller.journal,
            project=QgsProject.instance(),
            parent=self,
        )
        self.result_controller = ResultController(
            self.api_client,
            journal=self.sync_controller.journal,
            project=QgsProject.instance(),
            parent=self,
        )
        self.analysis_panel = AnalysisPanel(self)
        self.analysis_tab = self.analysis_panel
        layout = self.analysis_panel.layout()
        self.analysis_stage = self.analysis_panel.stage
        self.analysis_steps = self.analysis_panel.steps
        self.analysis_status = self.analysis_panel.notice
        self.analysis_rejection = self.analysis_panel.rejection
        self.analysis_actions = self.analysis_panel.actions
        self.analysis_report = self.analysis_panel.report
        self.analysis_resume = QtWidgets.QPushButton(
            "Resume / retry saved request", self
        )
        self.results_retry = QtWidgets.QPushButton("Retry saved result import", self)
        self.results_retry.setEnabled(False)
        self.analysis_cancel = QtWidgets.QPushButton("Cancel analysis", self)
        self.analysis_link = QtWidgets.QPushButton("Open in Aino", self)
        self.analysis_link.setEnabled(False)
        self.analysis_url = None
        for widget in (
            self.analysis_resume,
            self.results_retry,
            self.analysis_cancel,
            self.analysis_link,
        ):
            layout.addWidget(widget)
        self.addTab(self.analysis_tab, "Analysis")
        self.analysis_controller.changed.connect(self._analysis_changed)
        self.analysis_controller.status.connect(self.analysis_panel.set_notice)
        self.analysis_controller.diagnostic.connect(self.analysis_panel.set_diagnostic)
        self.analysis_controller.results_ready.connect(self._analysis_results)
        self.analysis_actions.submitted.connect(self._submit_analysis_action)
        self.analysis_resume.clicked.connect(self._resume_analysis)
        self.results_retry.clicked.connect(self._retry_results)
        self.result_controller.changed.connect(self.analysis_panel.set_import_state)
        self.result_controller.failed.connect(
            lambda message: self.results_retry.setEnabled(True)
        )
        self.result_controller.completed.connect(
            lambda ids: self.results_retry.setEnabled(False)
        )
        self.result_controller.completed.connect(self.area_panel.results_imported)
        self.analysis_cancel.clicked.connect(self._cancel_analysis)
        self.analysis_link.clicked.connect(self._open_analysis)
        self.analysis_panel.add_credits.clicked.connect(self._open_add_credits)
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.applicationStateChanged.connect(self._application_state_changed)

    def _open_add_credits(self):
        # Credits are bought in the browser; the balance is re-read when QGIS is active again.
        self._awaiting_credits = True
        QDesktopServices.openUrl(QUrl(ADD_CREDITS_URL))

    def _application_state_changed(self, state):
        if (
            self._awaiting_credits
            and state == Qt.ApplicationState.ApplicationActive
            and self.session.account_id
            and not self._shutdown
        ):
            self.get_current_limits()

    def _resume_analysis(self):
        if not self.session.account_id:
            return
        try:
            self.request_controller.retry()
        except (ValueError, OSError) as error:
            self.analysis_panel.set_error(str(error))

    def _analysis_changed(self, state):
        if not state or state.get("results") is None:
            self.result_controller.pause()
            self.results_retry.setEnabled(False)
        self.area_panel.update_analysis(
            self.api_client, state, self.analysis_controller.identity
        )
        rejection = state.get("last_rejection") or {}
        out_of_credits = rejection.get("error_code") == "insufficient_credits"
        if out_of_credits:
            text = "Not enough credits. Add credits, then submit your answer again."
        elif rejection:
            text = "Last submission rejected. " + (
                "Correct the request and press GO."
                if rejection.get("operation") == "start"
                else "Review your answer and retry, or cancel."
            )
        else:
            text = ""
        self.analysis_rejection.setText(text)
        self.analysis_panel.set_state(state)
        self.analysis_rejection.setVisible(bool(rejection))
        paused = ((state.get("latest") or {}).get("error")) == CREDITS_PAUSE_ERROR
        self.analysis_panel.add_credits.setVisible(out_of_credits or paused)
        self.analysis_url = state.get("project_url")
        url = QUrl(self.analysis_url or "")
        self.analysis_link.setEnabled(
            url.scheme() == "https" and bool(url.host()) and not url.userInfo()
        )

        if hasattr(self, "request_controller"):
            self._request_changed(self.request_controller.state)

    def _show_report(self, report, summary=None):
        if summary == self.analysis_panel.explanation.text():
            summary = None
        text = report_text(report) or (
            "Preview\n" + summary
            if isinstance(summary, str) and summary
            else "No narrative was returned. Open the analysis in Aino for available details."
        )
        if text == self.analysis_panel.explanation.text():
            text = ""
        self.analysis_report.setPlainText(text)
        self.analysis_report.setVisible(bool(text))

    def _analysis_results(self, manifest):
        self.analysis_panel.set_state(self.analysis_controller.state)
        self._show_report(
            manifest.get("report"),
            (self.analysis_controller.state.get("latest") or {}).get("summary"),
        )
        self.analysis_results_ready.emit(manifest)
        self._retry_results()

    def _retry_results(self):
        state = self.analysis_controller.state
        manifest = state.get("results")
        if manifest is None:
            return
        try:
            project_id = (state.get("start_intent") or {}).get("project_id") or (
                state.get("latest") or {}
            ).get("project_id")
            self.results_retry.setEnabled(False)
            self.result_controller.start(
                manifest,
                project_id,
                RenderReference(self.main_ui.iface.mapCanvas().mapSettings())
                if manifest["layers"]
                else None,
            )
        except (ValueError, OSError):
            self.analysis_panel.set_import_state(
                {"status": "failed", "diagnostic": {"code": "import_failed"}}
            )
            self.results_retry.setEnabled(True)

    def _submit_analysis_action(self, action):
        try:
            self.analysis_controller.submit_action(action)
        except (ValueError, OSError) as error:
            self.analysis_panel.set_error(str(error))

    def _cancel_analysis(self):
        if (
            QMessageBox.question(
                self,
                "Cancel analysis?",
                "Cancel this analysis on the server? Closing the window keeps it running.",
            )
            == QMessageBox.StandardButton.Yes
        ):
            try:
                self.request_controller.cancel()
            except (ValueError, OSError) as error:
                self.analysis_panel.set_error(str(error))

    def _open_analysis(self):
        if self.analysis_link.isEnabled():
            QDesktopServices.openUrl(QUrl(self.analysis_url))

    def _prompt_changed(self):
        if self.go_button.isEnabled():
            self.request_status_label.setText(
                "Ready — enter your request and press GO."
            )

    def start_osm_thread(self):
        prompt = self.prompt_field.toPlainText()
        if not prompt.strip():
            self.request_status_label.setText("Enter an analysis request first.")
            return
        self.send_osm_request(prompt)

    def _setup_request(self):
        self.request_controller = RequestController(
            QgsProject.instance(),
            self.session,
            self.sync_controller.journal,
            self.sync_controller,
            self.analysis_controller,
            self._capture_request,
            self,
        )
        self.request_controller.changed.connect(self._request_changed)
        self.request_controller.failed.connect(self.analysis_panel.set_error)
        self.preview_inputs_button.hide()
        self.cancel_capture_button.hide()
        self.sync_inputs_button.hide()
        self.resume_sync_button.hide()
        self._resume_sync()
        self._request_changed(self.request_controller.state)

    def _capture_request(self, area, layer_ids, finished, failed):
        return RequestCapture(
            QgsProject.instance(),
            self.main_ui.iface.mapCanvas(),
            self.sync_controller.journal.directory / "captures",
            layer_ids,
            finished,
            failed,
            self.request_controller,
        )

    def _request_changed(self, state):
        analysis = self.analysis_controller.state
        latest = analysis.get("latest") or {}
        active = self.request_controller.busy or (
            bool(analysis.get("start_intent") or analysis.get("task_id"))
            and latest.get("status") not in TERMINAL
        )
        self.go_button.setEnabled(not active and not analysis.get("action_intent"))
        self.go_button.setText("Working…" if active else "GO")
        self.analysis_cancel.setEnabled(
            active
            and not state.get("cancel_requested")
            and not analysis.get("action_intent")
        )
        self.analysis_resume.setEnabled(
            active
            or bool(state.get("error"))
            or self.sync_controller.busy
            or bool(
                analysis.get("task_id")
                and latest.get("results_ready")
                and analysis.get("results") is None
            )
        )
        self.analysis_panel.set_request_state(state)
        self.request_status_label.setText(self.analysis_panel.stage.text())

    def send_osm_request(self, prompt):
        if not self.session.access_token:
            self.analysis_panel.set_notice("Sign in, then press GO to send your request.")
            self.session.require_login()
            return
        try:
            ids = self.selected_input_ids()
            area = self.area_panel.selection()
            self.request_controller.start(prompt, area, ids)
            self.setCurrentWidget(self.analysis_tab)
        except ManualAreaRequired as error:
            self.analysis_panel.set_notice(error.guidance)
            self.area_panel.message.setText(error.guidance)
            self.setCurrentWidget(self.inputs_tab)
        except (ValueError, OSError, KeyError) as error:
            self.analysis_panel.set_error(str(error))
            self.setCurrentWidget(self.analysis_tab)

    def _send(self, method, path, payload, callback):
        self._handles[:] = [handle for handle in self._handles if handle.pending]
        handle = self.api_client.send(method, path, payload, callback)
        self._handles.append(handle)
        return handle

    def process_result(self, response, request_type):
        if not response.ok or not isinstance(response.data, dict):
            self._limit_errors[request_type] = (
                response.message
                if not response.ok
                else "Invalid account information response"
            )
            self.update_interface_with_restrictions_finish()
            return
        self._limit_errors.pop(request_type, None)
        if request_type == "subscription":
            self.subscriptions_response = response.data
        elif request_type == "limits":
            self.limits_response = response.data
        self.update_interface_with_restrictions_finish()

    def get_current_subscription(self):
        return self._send(
            "GET",
            "/user/current_subscription",
            None,
            lambda response: self.process_result(response, "subscription"),
        )

    def get_current_limits(self):
        return self._send(
            "GET",
            "/user/limits",
            None,
            lambda response: self.process_result(response, "limits"),
        )

    def update_interface_with_restrictions_finish(self):
        subscription = (self.subscriptions_response or {}).get(
            "current_subscription"
        ) or {}
        if not isinstance(subscription, dict):
            subscription = {}
        self.trial_label.setVisible(
            subscription.get("subscription_plan_code") == "trial"
        )
        self.trial_label.setText("Trial subscription")
        if subscription.get("subscription_plan_code") != "free" and subscription.get(
            "expiration_date"
        ):
            try:
                update_days_label(self, {"current_subscription": subscription})
                self.trial_label.setVisible(
                    subscription.get("subscription_plan_code") == "trial"
                    or self.trial_label.text() != "Trial subscription"
                )
            except (KeyError, TypeError, ValueError):
                pass
        self.prompt_field.setReadOnly(False)
        if self._limit_errors:
            message = "; ".join(self._limit_errors.values())
            self.prompts_label.setText(message)
            self.prompts_label.setToolTip(message)
            self.prompts_label.setStyleSheet("color: red;")
        else:
            self.prompts_label.setText(analysis_limits_text(self.limits_response))
            self.prompts_label.setStyleSheet("")
            self.prompts_label.setToolTip("")
        left = credits_left(self.limits_response)
        # Some accounts start with no credits at all, so say so before the first GO rather
        # than after a request stops.
        empty = left is not None and left <= 0
        if empty:
            self.prompts_label.setText(
                "Your account has no credits. Add credits to run requests."
            )
        else:
            self._awaiting_credits = False
        self.request_add_credits.setVisible(empty)

    def update_interface_with_restrictions_start(self):
        for handle in self._handles:
            handle.abort()
        self._handles.clear()
        self.subscriptions_response = None
        self.limits_response = None
        self._limit_errors.clear()
        self.get_current_subscription()
        self.get_current_limits()

    def _account_changed(self, previous, current):
        self.area_panel.reset()
        self._restore_draft_inputs(accepted_fallback=False)
        self.main_ui.user_email = None
        self.set_account_email(None)
        self._discard_capture()
        self._restore_map_tool()
        self.project_id = None
        self.project_link_text.clear()
        self.sync_status.clear()
        self.sync_notice.clear()
        self.relink_button.hide()
        self.detach_button.hide()
        self.limits_response = None
        self._limit_errors = {}
        self._awaiting_credits = False
        self.request_add_credits.setVisible(False)
        self.subscriptions_response = None
        self.prompts_label.clear()
        self.upload_button.setEnabled(True)

    def showEvent(self, event):
        super().showEvent(event)
        self.area_panel.set_preview_visible(True)

    def suspend_map_interaction(self):
        self.area_panel.set_preview_visible(False)
        self._restore_map_tool()

    def shutdown_map_interaction(self):
        self.area_panel.close()

    def shutdown(self):
        if self._shutdown:
            return
        self._shutdown = True
        self.request_controller.close()
        self.result_controller.close()
        self.analysis_controller.close()
        self.sync_controller.close()
        self.shutdown_map_interaction()
        self._discard_capture()
        for handle in self._handles:
            handle.abort()
        self._handles.clear()
        for signal, slot in (
            (self.session.account_changed, self._account_changed),
            (self.session.authenticated, self._resume_sync),
            (QgsProject.instance().cleared, self._project_sync_cleared),
            (QgsProject.instance().readProject, self._resume_sync),
        ):
            signal.disconnect(slot)

    def hideEvent(self, event):
        self.suspend_map_interaction()
        super().hideEvent(event)

    def closeEvent(self, event):
        self.area_panel.set_preview_visible(False)
        self._discard_capture()
        self._restore_map_tool()
        for handle in self._handles:
            handle.abort()
        self._handles.clear()
        super().closeEvent(event)

    def setup_account_controls(self):
        """Add the signed-in account and sign-out action to the tab bar."""
        self.account_widget = QtWidgets.QWidget(self)
        self.account_widget.setObjectName("account_widget")
        account_layout = QtWidgets.QHBoxLayout(self.account_widget)
        account_layout.setContentsMargins(8, 4, 8, 4)
        account_layout.setSpacing(8)

        self.account_email_label = AccountEmailLabel("Signed in")
        self.account_email_label.setTextFormat(Qt.TextFormat.PlainText)
        self.account_email_label.setObjectName("account_email_label")
        self.account_email_label.setMaximumWidth(175)
        self.account_email_label.setWordWrap(False)

        self.sign_out_button = QtWidgets.QPushButton("Sign out")
        self.sign_out_button.setObjectName("sign_out_button")
        self.sign_out_button.setCursor(_QT_POINTING_HAND_CURSOR)
        self.sign_out_button.setToolTip("Sign out of this Aino account")
        self.sign_out_button.clicked.connect(self.sign_out)

        account_layout.addWidget(self.account_email_label)
        account_layout.addWidget(self.sign_out_button)
        self.account_widget.setStyleSheet(
            "QLabel { color: #667085; font-size: 11px; }"
            "QPushButton { color: #B42318; background: transparent; "
            "border: 1px solid #E5E7EB; border-radius: 8px; padding: 4px 8px; }"
            "QPushButton:hover { color: #8A1C13; background: #FEF3F2; "
            "border-color: #FDA29B; }"
            "QPushButton:focus { border: 2px solid #315EFB; }"
        )
        self.setCornerWidget(self.account_widget, _QT_TOP_RIGHT_CORNER)
        self.set_account_email(None)

    def set_account_email(self, email):
        """Show the signed-in email and keep the full value available on hover."""
        signed_in = bool(self.session.access_token)
        account_email = (email or "").strip() if signed_in else ""
        account_email = account_email or ("Signed in" if signed_in else "Not signed in")
        self.sign_out_button.setText("Sign out" if signed_in else "Sign in")
        display_email = "\u200b".join(account_email) if email else account_email
        self.account_email_label.setText(display_email)
        self.account_email_label.setToolTip(account_email)
        self.account_email_label.setAccessibleName(account_email)

    def sign_out(self):
        if not self.session.access_token:
            self.main_ui.run()
            return
        container = getattr(self.main_ui, "dock", None)
        if container is None:
            container = self
        container.hide()
        self.main_ui.user_email = None
        self.session.clear()
        self.set_account_email(None)
        self.main_ui.run()
