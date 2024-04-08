from qgis.PyQt.QtWidgets import QDialog, QVBoxLayout, QLabel, QLineEdit, QPushButton, QMessageBox
from qgis.PyQt import uic
import os
from src import API_LINK

import requests


class LoginDialog(QDialog):
    def __init__(self, main_ui):
        super().__init__()
        uic.loadUi(os.path.join(
            os.path.dirname(__file__), 'ui_files/login.ui'), self)

        self.main_ui = main_ui
        self.process_login.clicked.connect(self.check_credentials)

    def check_credentials(self):
        login_data = {"email": self.username.text(), "password": self.password.text()}

        auth_url = f'{API_LINK}/public/auth/login'

        result = requests.post(auth_url, json=login_data)
        if result.status_code == 200:
            self.main_ui.bearer_token = result.json().get('access_token')
            self.accept()
        else:
            self.username.clear()
            self.password.clear()
