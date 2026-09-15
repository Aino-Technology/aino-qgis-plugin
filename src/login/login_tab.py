import os
import plistlib
import subprocess
import sys

import requests
from qgis.PyQt import uic
from qgis.PyQt.QtCore import QEvent, Qt
from qgis.PyQt.QtWidgets import QDialog, QMessageBox

from src import API_LINK


class LoginDialog(QDialog):
    def __init__(self, main_ui):
        super().__init__()
        uic.loadUi(os.path.join(
            os.path.dirname(__file__), './ui_files/login.ui'), self)

        self.main_ui = main_ui
        self.process_login.clicked.connect(self.check_credentials)
        self.signup_button.clicked.connect(self.handle_signup)
        self._text_replacements = self.load_macos_text_replacements()
        self.username.installEventFilter(self)

    @staticmethod
    def load_macos_text_replacements():
        """Read enabled macOS text replacements without hard-coding user data."""
        if sys.platform != "darwin":
            return {}

        try:
            result = subprocess.run(
                ["defaults", "export", "NSGlobalDomain", "-"],
                check=True,
                capture_output=True,
            )
            preferences = plistlib.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            return {}
        if not isinstance(preferences, dict):
            return {}

        replacements = {}
        for item in preferences.get("NSUserDictionaryReplacementItems", []):
            if not isinstance(item, dict) or not item.get("on"):
                continue

            shortcut = item.get("replace")
            replacement = item.get("with")
            if shortcut and replacement is not None:
                replacements[str(shortcut)] = str(replacement)
        return replacements

    def expand_username_replacement(self):
        """Expand the macOS replacement immediately before the username cursor."""
        text = self.username.text()
        cursor_position = self.username.cursorPosition()
        text_before_cursor = text[:cursor_position]

        for shortcut, replacement in sorted(
                self._text_replacements.items(),
                key=lambda item: len(item[0]),
                reverse=True):
            if not text_before_cursor.endswith(shortcut):
                continue

            shortcut_start = cursor_position - len(shortcut)
            previous_character = text_before_cursor[:shortcut_start][-1:]
            if (shortcut[0].isalnum() and previous_character and
                    (previous_character.isalnum() or previous_character == "_")):
                continue

            self.username.setSelection(shortcut_start, len(shortcut))
            self.username.insert(replacement)
            self.username.setCursorPosition(shortcut_start + len(replacement))
            return True

        return False

    def eventFilter(self, watched, event):
        if watched is self.username and event.type() == QEvent.KeyPress:
            trigger_keys = {
                Qt.Key_Space,
                Qt.Key_Tab,
                Qt.Key_Return,
                Qt.Key_Enter,
            }
            trigger_characters = ",.;:!?)]}"
            if event.key() in trigger_keys or event.text() in trigger_characters:
                self.expand_username_replacement()

        return super().eventFilter(watched, event)

    @staticmethod
    def format_login_error(response):
        """Turn an auth response into a safe, human-readable message."""
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        error_code = str(payload.get("error", "")).lower()
        error_description = str(payload.get("error_description", "")).lower()
        if error_code in {"invalid_grant", "invalid_credentials"} or \
                "invalid login credentials" in error_description:
            return "The email or password is incorrect. Check your details and try again."

        message = payload.get("message") or payload.get("error_description")
        if isinstance(message, dict):
            message = message.get("message") or message.get("detail")
        if message:
            return str(message)

        if response.status_code in {400, 401, 403}:
            return "The email or password is incorrect. Check your details and try again."

        return "Unable to log in right now. Please try again."

    def show_login_error(self, message, icon=QMessageBox.Warning):
        """Show a readable login error independent of the QGIS theme."""
        message_box = QMessageBox(self)
        message_box.setIcon(icon)
        message_box.setWindowTitle("Login failed")
        message_box.setTextFormat(Qt.PlainText)
        message_box.setText(message)
        message_box.setStandardButtons(QMessageBox.Ok)
        message_box.setStyleSheet(
            "QMessageBox { background-color: #FFFFFF; color: #111827; }"
            "QMessageBox QLabel { background-color: transparent; color: #111827; }"
            "QMessageBox QPushButton { background-color: #111827; color: #FFFFFF; "
            "border: 1px solid #111827; border-radius: 8px; min-width: 72px; "
            "padding: 6px 16px; }"
            "QMessageBox QPushButton:hover { background-color: #315EFB; "
            "border-color: #315EFB; }"
            "QMessageBox QPushButton:focus { border: 2px solid #315EFB; }"
        )
        message_box.exec()

    def handle_signup(self, _checked=False):
        """Open web sign-up and return to this login flow afterwards."""
        import webbrowser

        from . import exec_dialog
        from .signup_tab import SignupDialog

        self.hide()
        webbrowser.open("https://app.aino.world/auth", new=0, autoraise=True)
        signup_dialog = SignupDialog(self.main_ui)
        if exec_dialog(signup_dialog):
            self.accept()
        else:
            self.show()

    def check_credentials(self):
        self.expand_username_replacement()
        login_data = {"email": self.username.text().strip(), "password": self.password.text()}

        auth_url = f'{API_LINK}/public/auth/login'

        try:
            result = requests.post(auth_url, json=login_data, timeout=30)
        except requests.RequestException as error:
            self.show_login_error(str(error), QMessageBox.Critical)
            return

        if result.status_code == 200:
            try:
                access_token = result.json().get("access_token")
            except ValueError as error:
                self.show_login_error(f"Invalid server response: {error}", QMessageBox.Critical)
                return

            if not access_token:
                self.show_login_error(
                    "The server did not return an access token.",
                    QMessageBox.Critical,
                )
                return

            self.main_ui.bearer_token = access_token
            self.main_ui.user_email = login_data["email"]
            self.accept()
        else:
            self.show_login_error(self.format_login_error(result))
