import json
import os
import re

import requests
from PyQt5.QtNetwork import QNetworkAccessManager, QNetworkRequest
from qgis.PyQt import QtWidgets
from qgis.PyQt import uic
from qgis.PyQt.QtCore import QVariant, QDate, Qt, QByteArray, QUrl
from qgis.PyQt.QtWidgets import QMessageBox
from qgis.core import QgsProject

from src import API_LINK
from .helpers import add_data, update_scroll_area, get_selected_options, update_labels, toggle_button_state, \
    update_days_label, update_prompts_left_label, block_input_area

FORM_CLASS_main, _ = uic.loadUiType(os.path.join(
    os.path.dirname(__file__), './ui_files/main.ui'))


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
        self.upload_button.clicked.connect(self.upload_process)
        self.currentChanged.connect(lambda _: update_scroll_area(self))
        self.limits_response = None
        self.subscriptions_response = None
        self.trial_label.setVisible(False)

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
                        geojson = {
                            'type': 'FeatureCollection',
                            'features': []
                        }
                        attributes = layer.fields().names()

                        for feature in layer.getFeatures():
                            properties = {k: (None if isinstance(v, QVariant) and v.isNull() else v.toString(
                                Qt.ISODate) if isinstance(v, QDate) else v) for k, v in
                                          zip(attributes, [feature[prop] for prop in
                                                           [attribute for attribute in attributes if
                                                            attribute not in ['iconCaption', 'marker-color']]])}

                            geom = feature.geometry()
                            geojson['features'].append({
                                'type': 'Feature',
                                'properties': properties,
                                'geometry': json.loads(geom.asJson())
                            })

                        layer_sources.append({'name': name, 'feature_collection': geojson})
                else:
                    layer_sources.append(
                        {'name': name, 'feature_collection': {"type": "FeatureCollection", "features": []}})

            try:
                self.upload_layers(list(layer_sources), self.project_id)
            except Exception as e:
                toggle_button_state(self.upload_button)
                QMessageBox.information(self, "", str(e))

        else:
            QMessageBox.warning(None, "Warning", "Please, provide link to Aino project")

    def finish_upload(self, result):
        json_result = json.loads(result)
        text = json.loads(json_result['text'])
        update_labels(text, self)
        toggle_button_state(self.upload_button)

    def start_osm_thread(self):
        prompt = self.prompt_field.toPlainText()

        self.send_osm_request(prompt)

    def send_osm_request(self, prompt):
        toggle_button_state(self.go_button)
        api_url = f'{API_LINK}/public/ai_query/parser'
        request = QNetworkRequest(QUrl(api_url))
        request.setHeader(QNetworkRequest.ContentTypeHeader, "application/json")
        request.setRawHeader(b"Authorization", f"Bearer {self.token}".encode())

        data = QByteArray(json.dumps({'prompt': prompt}).encode())

        reply = self.manager.post(request, data)
        reply.finished.connect(lambda: self.on_osm_reply_finished(prompt))

    def on_osm_reply_finished(self, prompt):
        reply = self.sender()
        if reply.error():
            print(f"Error: {reply.errorString()}")
            return

        status_code = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        response_data = reply.readAll().data()

        try:
            text = response_data.decode()
        except UnicodeDecodeError:
            text = ''

        result = json.dumps({'status': status_code, 'text': text})
        self.finish_ai(result, prompt)

        reply.deleteLater()

    def finish_ai(self, result, prompt):
        toggle_button_state(self.go_button)

        try:
            result_json = json.loads(result)
            data = result_json['text']
            status = result_json['status']
            if status // 100 == 2:

                data_dict = json.loads(data)

                result_elements = len(data_dict['features'])
                add_data(data, prompt)
                self.update_interface_with_restrictions_start()
                update_scroll_area(self)
                if result_elements:
                    QMessageBox.information(self, "", f'Found {result_elements} elements')
                else:
                    QMessageBox.warning(self, "", "Nothing was found, try to reformulate your query")

            elif status // 100 == 4:
                QMessageBox.critical(self, "", f'Request error: {data}')
            elif status // 100 == 5:
                QMessageBox.critical(self, "", f'Response error: {data}')

        except Exception as e:
            QMessageBox.critical(self, "", f"An error occurred: {e}, try again")
            pass

    def upload_layers(self, features_with_names, project_id):
        api_url = f'{API_LINK}/public/project/files/bulk_add_geojson'
        request = QNetworkRequest(QUrl(api_url))
        request.setHeader(QNetworkRequest.ContentTypeHeader, "application/json")
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
        if reply.error():
            print(f"Error: {reply.errorString()}")
            return

        status_code = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        response_data = reply.readAll().data()

        try:
            text = response_data.decode()
        except UnicodeDecodeError:
            text = ''

        result = json.dumps({'status': status_code, 'text': text})
        self.finish_upload(result)

        reply.deleteLater()

    def process_result(self, response, request_type):
        if response.status_code == 200:  # HTTP OK
            json_response = response.json()
            if request_type == 'subscription':
                self.subscriptions_response = json_response
            elif request_type == 'limits':
                self.limits_response = json_response

            if self.subscriptions_response is not None and self.limits_response is not None:
                self.update_interface_with_restrictions_finish()

    def get_current_subscription(self):
        api_url = f'{API_LINK}/user/current_subscription'
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}"
        }
        response = requests.get(api_url, headers=headers)
        self.process_result(response, 'subscription')

    def get_current_limits(self):
        api_url = f'{API_LINK}/user/limits'
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}"
        }
        response = requests.get(api_url, headers=headers)
        self.process_result(response, 'limits')

    def update_interface_with_restrictions_finish(self):
        subscription_plan = self.subscriptions_response["current_subscription"]["subscription_plan_code"]
        if subscription_plan != "free":
            self.trial_label.setVisible(True)

        prompts_left = update_prompts_left_label(self, self.limits_response)
        update_days_label(self, self.subscriptions_response)

        if (subscription_plan == "free" or subscription_plan == "trial") and prompts_left == 0:
            block_input_area(self, "subscription")
        elif subscription_plan == "individual" and prompts_left == 0:
            block_input_area(self, "prompts")
        else:
            self.prompt_field.setReadOnly(False)
            self.prompt_field.clear()

    def update_interface_with_restrictions_start(self):
        self.get_current_subscription()
        self.get_current_limits()
