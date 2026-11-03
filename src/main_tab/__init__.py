import json
import os
import re

from qgis.PyQt import QtWidgets, uic
from qgis.PyQt.QtCore import QVariant, QDate, Qt
from qgis.PyQt.QtWidgets import QMessageBox
from qgis.core import QgsProject

from .helpers import (
    add_data,
    update_scroll_area,
    get_selected_options,
    update_labels,
    toggle_button_state,
)

_QT_POINTING_HAND_CURSOR = getattr(getattr(Qt, "CursorShape", Qt), "PointingHandCursor")
_QT_TOP_RIGHT_CORNER = getattr(getattr(Qt, "Corner", Qt), "TopRightCorner")

FORM_CLASS_main, _ = uic.loadUiType(
    os.path.join(os.path.dirname(__file__), "./ui_files/main.ui")
)


class OsmParserDialogMain(QtWidgets.QTabWidget, FORM_CLASS_main):
    def __init__(self, parent=None, osm_parser=None):
        super(OsmParserDialogMain, self).__init__(parent)
        self.project_id = None
        self.main_ui = osm_parser
        self.api_client = osm_parser.api_client
        self.session = osm_parser.session
        handles = []
        self._handles = handles
        self.destroyed.connect(lambda: [handle.abort() for handle in handles])
        self.session.account_changed.connect(self._account_changed)
        self.first_step = True
        self.setupUi(self)
        self.setup_account_controls()

        self.go_button.clicked.connect(self.start_osm_thread)
        self.upload_button.clicked.connect(self.upload_process)
        self.currentChanged.connect(lambda _: update_scroll_area(self))
        self.limits_response = None
        self._limit_errors = {}
        self.subscriptions_response = None
        self.trial_label.setVisible(False)
        self.trial_label.setText("Trial subscription")
        self.prompts_label.setGeometry(165, 102, 365, 36)
        self.prompts_label.setWordWrap(True)
        self.prompts_label.setText("Aino checks credits and limits on submission.")

    def upload_next(self):
        project_link = self.project_link_text.text()
        pattern = r"https://beta\.aino\.world/project/(\d+)"
        match = re.search(pattern, project_link)
        if match:
            self.project_id = match.group(1)
            update_scroll_area(self)

        else:
            QMessageBox.critical(self, "", "Please, provide project link")

    def upload_process(self):
        project_link = self.project_link_text.text()
        pattern = r"https://beta\.aino\.world/project/(\d+)"
        match = re.search(pattern, project_link)

        if match:
            toggle_button_state(self.upload_button)

            self.project_id = match.group(1)

            layer_names = get_selected_options(self)
            layer_sources = []
            for name in layer_names:
                layers = QgsProject.instance().mapLayersByName(name)

                if layers:
                    layer = layers[0]

                    if layer:
                        geojson = {"type": "FeatureCollection", "features": []}
                        attributes = layer.fields().names()

                        for feature in layer.getFeatures():
                            properties = {
                                k: (
                                    None
                                    if isinstance(v, QVariant) and v.isNull()
                                    else v.toString(Qt.DateFormat.ISODate)
                                    if isinstance(v, QDate)
                                    else v
                                )
                                for k, v in zip(
                                    attributes,
                                    [
                                        feature[prop]
                                        for prop in [
                                            attribute
                                            for attribute in attributes
                                            if attribute
                                            not in ["iconCaption", "marker-color"]
                                        ]
                                    ],
                                )
                            }

                            geom = feature.geometry()
                            geojson["features"].append(
                                {
                                    "type": "Feature",
                                    "properties": properties,
                                    "geometry": json.loads(geom.asJson()),
                                }
                            )

                        layer_sources.append(
                            {"name": name, "feature_collection": geojson}
                        )
                else:
                    layer_sources.append(
                        {
                            "name": name,
                            "feature_collection": {
                                "type": "FeatureCollection",
                                "features": [],
                            },
                        }
                    )

            try:
                self.upload_layers(list(layer_sources), self.project_id)
            except Exception as e:
                toggle_button_state(self.upload_button)
                QMessageBox.information(self, "", str(e))

        else:
            QMessageBox.warning(None, "Warning", "Please, provide link to Aino project")

    def finish_upload(self, result):
        json_result = json.loads(result)
        text = json.loads(json_result["text"])
        update_labels(text, self)
        toggle_button_state(self.upload_button)

    def start_osm_thread(self):
        prompt = self.prompt_field.toPlainText()

        self.send_osm_request(prompt)

    def send_osm_request(self, prompt):
        toggle_button_state(self.go_button)
        self._send(
            "POST",
            "/public/ai_query/parser",
            {"prompt": prompt},
            lambda result: self.finish_ai(self._legacy_result(result), prompt),
        )

    def _send(self, method, path, payload, callback):
        self._handles[:] = [handle for handle in self._handles if handle.active]
        handle = self.api_client.send(method, path, payload, callback)
        self._handles.append(handle)
        return handle

    def _legacy_result(self, result):
        data = result.data if result.ok else {"message": result.message}
        return json.dumps({"status": result.status or 503, "text": json.dumps(data)})

    def finish_ai(self, result, prompt):
        toggle_button_state(self.go_button)

        try:
            result_json = json.loads(result)
            data = result_json["text"]
            status = result_json["status"]
            if status // 100 == 2:
                data_dict = json.loads(data)

                result_elements = len(data_dict["features"])
                add_data(data, prompt)
                self.update_interface_with_restrictions_start()
                update_scroll_area(self)
                if result_elements:
                    QMessageBox.information(
                        self, "", f"Found {result_elements} elements"
                    )
                else:
                    QMessageBox.warning(
                        self, "", "Nothing was found, try to reformulate your query"
                    )

            elif status // 100 == 4:
                QMessageBox.critical(self, "", f"Request error: {data}")
            elif status // 100 == 5:
                QMessageBox.critical(self, "", f"Response error: {data}")

        except Exception as e:
            QMessageBox.critical(self, "", f"An error occurred: {e}, try again")
            pass

    def upload_layers(self, features_with_names, project_id):
        self._send(
            "POST",
            "/public/project/files/bulk_add_geojson",
            {"qgis_layers": features_with_names, "project_id": project_id},
            self._upload_finished,
        )

    def _upload_finished(self, result):
        if result.ok:
            self.finish_upload(self._legacy_result(result))
        else:
            self.upload_button.setEnabled(True)
            QMessageBox.warning(self, "Upload failed", result.message)

    def process_result(self, response, request_type):
        if not response.ok:
            self._limit_errors[request_type] = response.message
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
        self.trial_label.setVisible(
            subscription.get("subscription_plan_code") == "trial"
        )
        self.prompt_field.setReadOnly(False)
        if self._limit_errors:
            message = "; ".join(self._limit_errors.values())
            self.prompts_label.setText(message)
            self.prompts_label.setToolTip(message)
            self.prompts_label.setStyleSheet("color: red;")
        else:
            self.prompts_label.setText("Aino checks credits and limits on submission.")
            self.prompts_label.setStyleSheet("")
            self.prompts_label.setToolTip("")

    def update_interface_with_restrictions_start(self):
        self.get_current_subscription()
        self.get_current_limits()

    def _account_changed(self, previous, current):
        self.project_id = None
        self.project_link_text.clear()
        self.limits_response = None
        self._limit_errors = {}
        self.subscriptions_response = None
        self.prompts_label.clear()
        self.go_button.setEnabled(True)
        self.upload_button.setEnabled(True)

    def closeEvent(self, event):
        for handle in self._handles:
            handle.abort()
        self._handles.clear()
        self.go_button.setEnabled(True)
        self.upload_button.setEnabled(True)
        super().closeEvent(event)

    def setup_account_controls(self):
        """Add the signed-in account and sign-out action to the tab bar."""
        self.account_widget = QtWidgets.QWidget(self)
        self.account_widget.setObjectName("account_widget")
        account_layout = QtWidgets.QHBoxLayout(self.account_widget)
        account_layout.setContentsMargins(8, 4, 8, 4)
        account_layout.setSpacing(8)

        self.account_email_label = QtWidgets.QLabel("Signed in")
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


    def set_account_email(self, email):
        """Show the signed-in email and keep the full value available on hover."""
        account_email = (email or "").strip() or "Signed in"
        self.account_email_label.setText(account_email)
        self.account_email_label.setToolTip(account_email)


    def sign_out(self):
        self.hide()
        self.main_ui.user_email = None
        self.session.clear()
        self.set_account_email(None)
        self.main_ui.run()
