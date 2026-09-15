import os

import requests
from qgis.PyQt import uic
from qgis.PyQt.QtWidgets import QDialog, QMessageBox

from src import API_LINK


class LoginDialog(QDialog):
    def __init__(self, main_ui):
        super().__init__()
        uic.loadUi(os.path.join(
            os.path.dirname(__file__), './ui_files/login.ui'), self)

        self.main_ui = main_ui
        self.process_login.clicked.connect(self.check_credentials)

    def check_credentials(self):
        login_data = {"email": self.username.text(), "password": self.password.text()}

        auth_url = f'{API_LINK}/public/auth/login'

        try:
            result = requests.post(auth_url, json=login_data, timeout=30)
        except requests.RequestException as error:
            QMessageBox.critical(self, "Login failed", str(error))
            return

        if result.status_code == 200:
            try:
                access_token = result.json().get("access_token")
            except ValueError as error:
                QMessageBox.critical(self, "Login failed", f"Invalid server response: {error}")
                return

            if not access_token:
                QMessageBox.critical(
                    self,
                    "Login failed",
                    "The server did not return an access token",
                )
                return

            self.main_ui.bearer_token = access_token
            self.accept()
        else:
            message = result.text or f"HTTP {result.status_code}"
            QMessageBox.warning(self, "Login failed", message)
