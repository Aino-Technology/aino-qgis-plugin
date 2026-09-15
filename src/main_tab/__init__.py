import json
import os
import re

import requests
from qgis.PyQt import QtWidgets
from qgis.PyQt import uic
from qgis.PyQt.QtCore import QVariant, QDate, Qt, QByteArray, QUrl
from qgis.PyQt.QtNetwork import QNetworkAccessManager, QNetworkRequest
from qgis.PyQt.QtWidgets import QMessageBox
from qgis.core import QgsProject

from src import API_LINK
from .helpers import (
    add_data,
    block_input_area,
    get_selected_options,
    set_button_enabled,
    toggle_button_state,
    update_days_label,
    update_labels,
    update_prompts_left_label,
    update_scroll_area,
)

FORM_CLASS_main, _ = uic.loadUiType(os.path.join(
    os.path.dirname(__file__), './ui_files/main.ui'))

PROJECT_URL_PATTERN = re.compile(r"https://app\.aino\.world/project/(\d+)")
IGNORED_ATTRIBUTES = {"iconCaption", "marker-color"}


def serialize_attribute(value):
    if isinstance(value, QVariant):
        if value.isNull():
            return None
        value = value.value()

    if isinstance(value, QDate):
        return value.toString(Qt.ISODate)

    return value


class OsmParserDialogMain(QtWidgets.QTabWidget, FORM_CLASS_main):

    def __init__(self, parent=None, osm_parser=None):
        super(OsmParserDialogMain, self).__init__(parent)
        self.manager = QNetworkAccessManager()
        self.token = None
        self.project_id = None
        self.main_ui = osm_parser
        self.first_step = True
        self.setupUi(self)

        self.go_button.clicked.connect(self.start_osm_thread)
        self.prompt_field.textChanged.connect(self.on_prompt_changed)
        self.upload_button.clicked.connect(self.upload_process)
        self.currentChanged.connect(lambda _: update_scroll_area(self))
        self.limits_response = None
        self.subscriptions_response = None
        self.trial_label.setVisible(False)

    def set_request_status(self, message, state="ready"):
        colors = {
            "ready": "#667085",
            "working": "#315EFB",
            "success": "#157F3D",
            "error": "#B42318",
        }
        self.request_status_label.setText(message)
        self.request_status_label.setStyleSheet(
            f"color: {colors.get(state, colors['ready'])}; padding: 2px 0;"
        )
        self.go_button.setText("Working…" if state == "working" else "GO")

    def on_prompt_changed(self):
        if self.go_button.isEnabled():
            self.set_request_status("Ready — press GO to create a layer.")

    def upload_next(self):
        project_link = self.project_link_text.text()
        match = PROJECT_URL_PATTERN.search(project_link)
        if match:
            self.project_id = match.group(1)
            update_scroll_area(self)

        else:
            QMessageBox.critical(self, "", "Please, provide project link")

    def upload_process(self):

        project_link = self.project_link_text.text()
        match = PROJECT_URL_PATTERN.search(project_link)

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
                        geojson = {
                            'type': 'FeatureCollection',
                            'features': []
                        }
                        attributes = [
                            attribute
                            for attribute in layer.fields().names()
                            if attribute not in IGNORED_ATTRIBUTES
                        ]

                        for feature in layer.getFeatures():
                            properties = {
                                attribute: serialize_attribute(feature[attribute])
                                for attribute in attributes
                            }

                            geom = feature.geometry()
                            geojson['features'].append({
                                'type': 'Feature',
                                'properties': properties,
                                'geometry': json.loads(geom.asJson())
                            })

                        layer_sources.append({'name': name, 'feature_collection': geojson})
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
        try:
            json_result = json.loads(result)
            text = json.loads(json_result["text"])
            update_labels(text, self)
        except (TypeError, ValueError, KeyError) as error:
            QMessageBox.critical(self, "Upload failed", f"Invalid server response: {error}")
        finally:
            set_button_enabled(self.upload_button, True)

    def start_osm_thread(self):
        prompt = self.prompt_field.toPlainText()

        if not prompt.strip():
            self.set_request_status("Enter a map request first.", "error")
            QMessageBox.warning(
                self,
                "Add a map request",
                "Describe the places or features you want to add to QGIS.",
            )
            return

        self.send_osm_request(prompt)

    @staticmethod
    def format_request_error(response_text, status_code=None):
        """Turn an API error payload into a short, useful message."""
        try:
            payload = json.loads(response_text)
        except (TypeError, ValueError):
            payload = {}

        error_message = payload.get("error_message") or payload.get("message")
        if error_message:
            return str(error_message)

        error = payload.get("error")
        if error:
            return str(error)

        if status_code:
            return f"HTTP {status_code}"

        return "The request could not be completed. Please try again."

    @staticmethod
    def get_status_code(reply):
        """Read the HTTP status attribute on both Qt 5 and Qt 6."""
        legacy_attribute = getattr(QNetworkRequest, "HttpStatusCodeAttribute", None)
        if legacy_attribute is not None:
            status_code = reply.attribute(legacy_attribute)
        else:
            status_code = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)

        try:
            return int(status_code)
        except (TypeError, ValueError):
            return None

    def send_osm_request(self, prompt):
        set_button_enabled(self.go_button, False)
        self.set_request_status(
            f"Working… looking up {prompt.strip()!r} in OpenStreetMap.",
            "working",
        )
        api_url = f'{API_LINK}/public/ai_query/parser'
        request = QNetworkRequest(QUrl(api_url))
        request.setRawHeader(b"Content-Type", b"application/json")
        request.setRawHeader(b"Authorization", f"Bearer {self.token}".encode())

        data = QByteArray(json.dumps({'prompt': prompt}).encode())

        reply = self.manager.post(request, data)
        reply.finished.connect(lambda: self.on_osm_reply_finished(prompt))

    def on_osm_reply_finished(self, prompt):
        reply = self.sender()
        try:
            status_code = self.get_status_code(reply)
            response_data = reply.readAll().data()
            text = response_data.decode(errors="replace")

            if status_code is not None and 200 <= status_code < 300:
                result = json.dumps({"status": status_code, "text": text})
                self.finish_ai(result, prompt)
                return

            if reply.error() or status_code is not None:
                message = self.format_request_error(text, status_code)
                if not text:
                    message = reply.errorString()
                set_button_enabled(self.go_button, True)
                self.set_request_status(message, "error")
                QMessageBox.critical(self, "Request failed", message)
                return

            result = json.dumps({"status": status_code, "text": text})
            self.finish_ai(result, prompt)
        finally:
            reply.deleteLater()

    def finish_ai(self, result, prompt):
        set_button_enabled(self.go_button, True)

        try:
            result_json = json.loads(result)
            data = result_json['text']
            status = result_json['status']
            if status // 100 == 2:

                data_dict = json.loads(data)

                result_elements = len(data_dict.get('features', []))
                add_data(data, prompt)
                self.update_interface_with_restrictions_start()
                update_scroll_area(self)
                if result_elements:
                    self.set_request_status(
                        f"Added {result_elements} features to QGIS.",
                        "success",
                    )
                else:
                    self.set_request_status(
                        "No matching features found. Try a more specific request.",
                        "error",
                    )
                if result_elements:
                    QMessageBox.information(self, "", f'Found {result_elements} elements')
                else:
                    QMessageBox.warning(
                        self,
                        "",
                        "Nothing was found, try to reformulate your query",
                    )

            elif status // 100 == 4:
                message = self.format_request_error(data, status)
                self.set_request_status(message, "error")
                QMessageBox.critical(self, "Request failed", message)
            elif status // 100 == 5:
                message = self.format_request_error(data, status)
                self.set_request_status(message, "error")
                QMessageBox.critical(self, "Service error", message)

        except Exception as e:
            self.set_request_status(f"Request failed: {e}", "error")
            QMessageBox.critical(self, "", f"An error occurred: {e}, try again")
            pass

    def upload_layers(self, features_with_names, project_id):
        api_url = f'{API_LINK}/public/project/files/bulk_add_geojson'
        request = QNetworkRequest(QUrl(api_url))
        request.setRawHeader(b"Content-Type", b"application/json")
        request.setRawHeader(b"Authorization", f"Bearer {self.token}".encode())

        data = {
            'qgis_layers': features_with_names,
            'project_id': project_id
        }
        json_data = json.dumps(data).encode('utf-8')

        reply = self.manager.post(request, QByteArray(json_data))
        reply.finished.connect(self.on_upload_layers_finished)

    def on_upload_layers_finished(self):
        reply = self.sender()
        try:
            if reply.error():
                error_text = reply.readAll().data().decode(errors="replace")
                message = error_text or reply.errorString()
                set_button_enabled(self.upload_button, True)
                QMessageBox.critical(self, "Upload failed", message)
                return

            status_code = self.get_status_code(reply)
            response_data = reply.readAll().data()
            text = response_data.decode(errors="replace")
            result = json.dumps({"status": status_code, "text": text})
            self.finish_upload(result)
        finally:
            reply.deleteLater()

    def process_result(self, response, request_type):
        if response.status_code != 200:
            message = response.text or f"HTTP {response.status_code}"
            QMessageBox.critical(self, "Account information failed", message)
            return

        try:
            json_response = response.json()
        except ValueError as error:
            QMessageBox.critical(
                self,
                "Account information failed",
                f"Invalid server response: {error}",
            )
            return

        if request_type == "subscription":
            self.subscriptions_response = json_response
        elif request_type == "limits":
            self.limits_response = json_response

        if self.subscriptions_response is not None and self.limits_response is not None:
            self.update_interface_with_restrictions_finish()

    def get_current_subscription(self):
        api_url = f'{API_LINK}/user/current_subscription'
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}"
        }
        try:
            response = requests.get(api_url, headers=headers, timeout=30)
        except requests.RequestException as error:
            QMessageBox.critical(self, "Subscription request failed", str(error))
            return
        self.process_result(response, 'subscription')

    def get_current_limits(self):
        api_url = f'{API_LINK}/user/limits'
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}"
        }
        try:
            response = requests.get(api_url, headers=headers, timeout=30)
        except requests.RequestException as error:
            QMessageBox.critical(self, "Limits request failed", str(error))
            return
        self.process_result(response, 'limits')

    def update_interface_with_restrictions_finish(self):
        subscription = self.subscriptions_response.get("current_subscription")
        if not subscription:
            block_input_area(self, "subscription")
            return

        subscription_plan = subscription["subscription_plan_code"]
        if subscription_plan != "free":
            self.trial_label.setVisible(True)

        prompts_left = update_prompts_left_label(self, self.limits_response)
        update_days_label(self, self.subscriptions_response)

        if prompts_left == 0 and subscription_plan in {"free", "trial"}:
            block_input_area(self, "subscription")
        elif prompts_left == 0:
            block_input_area(self, "prompts")
        else:
            self.prompt_field.setReadOnly(False)
            self.prompt_field.clear()

    def update_interface_with_restrictions_start(self):
        self.subscriptions_response = None
        self.limits_response = None
        self.get_current_subscription()
        self.get_current_limits()
