import os
import plistlib
import subprocess
import sys

from qgis.PyQt import uic
from qgis.PyQt.QtCore import QEvent, Qt
from qgis.PyQt.QtGui import QPixmap
from qgis.PyQt.QtWidgets import QDialog, QMessageBox, QLabel



_QT_KEY = getattr(Qt, "Key", Qt)
_QT_TEXT_FORMAT = getattr(Qt, "TextFormat", Qt)
_QT_KEY_SPACE = getattr(_QT_KEY, "Key_Space")
_QT_KEY_TAB = getattr(_QT_KEY, "Key_Tab")
_QT_KEY_RETURN = getattr(_QT_KEY, "Key_Return")
_QT_KEY_ENTER = getattr(_QT_KEY, "Key_Enter")
_QT_PLAIN_TEXT = getattr(_QT_TEXT_FORMAT, "PlainText")


class LoginDialog(QDialog):
    def __init__(self, main_ui):
        super().__init__()
        uic.loadUi(os.path.join(
            os.path.dirname(__file__), './ui_files/login.ui'), self)
        self.label_2.setText("")
        self.label_2.setPixmap(QPixmap(os.path.join(
            os.path.dirname(__file__), '../../Welcome.svg')))
        self.label_2.setScaledContents(True)

        self.main_ui = main_ui
        self._request = None
        self._login_email = None
        self.error_label = QLabel(self)
        self.error_label.setWordWrap(True)
        self.error_label.setTextFormat(_QT_PLAIN_TEXT)
        self.error_label.setGeometry(30, 510, 421, 65)
        self.error_label.setStyleSheet("color: #B42318; background: white;")
        self.finished.connect(self._cleanup)
        handles = []
        self._handles = handles
        self.destroyed.connect(lambda: [handle.abort() for handle in handles])
        self.process_login.clicked.connect(self.check_credentials)
        self.signup_button.clicked.connect(self.handle_signup)
        self._text_replacements = self.load_macos_text_replacements()
        self.username.installEventFilter(self)

    @staticmethod
    def message_box_icon(name):
        """Return a QMessageBox icon on both Qt 5 and Qt 6."""
        icon_enum = getattr(QMessageBox, "Icon", QMessageBox)
        return getattr(icon_enum, name)

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
                _QT_KEY_SPACE,
                _QT_KEY_TAB,
                _QT_KEY_RETURN,
                _QT_KEY_ENTER,
            }
            trigger_characters = ",.;:!?)]}"
            if event.key() in trigger_keys or event.text() in trigger_characters:
                self.expand_username_replacement()

        return super().eventFilter(watched, event)

    @staticmethod
    def format_login_error(response):
        """Turn an auth response into a safe, human-readable message."""
        try:
            payload = response.data
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

        if response.status in {400, 401, 403}:
            return "The email or password is incorrect. Check your details and try again."

        return "Unable to log in right now. Please try again."

    def show_login_error(self, message, icon=None):
        """Show a readable login error independent of the QGIS theme."""
        message_box = QMessageBox(self)
        if icon is None:
            icon = self.message_box_icon("Warning")
        message_box.setIcon(icon)
        message_box.setWindowTitle("Login failed")
        message_box.setTextFormat(_QT_PLAIN_TEXT)
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
        if self._request and self._request.active:
            return
        self.expand_username_replacement()
        self._login_email = self.username.text().strip()
        self.process_login.setEnabled(False)
        self.error_label.clear()
        self._request = self.main_ui.api_client.send(
            "POST",
            "/public/auth/login",
            {"email": self._login_email, "password": self.password.text()},
            self._login_finished,
        )
        self._handles.append(self._request)
        self.password.clear()


    def _login_finished(self, result):
        self._request = None
        self._handles.clear()
        self.process_login.setEnabled(True)
        if result.ok and isinstance(result.data, dict):
            try:
                self.main_ui.session.set_access_token(result.data.get("access_token"))
            except ValueError as error:
                self.error_label.setText(str(error))
                return
            self.main_ui.user_email = self._login_email
            if getattr(self.main_ui, "main_dlg", None) is not None:
                self.main_ui.main_dlg.set_account_email(self._login_email)
            self.accept()
        else:
            self.error_label.setText(self.format_login_error(result))


    def _cleanup(self, result):
        if not result:
            self.main_ui.session.cancel_login()
        if self._request:
            self._request.abort()
            self._request = None
        self._handles.clear()
        self.password.clear()


    def closeEvent(self, event):
        self._cleanup(0)
        super().closeEvent(event)

