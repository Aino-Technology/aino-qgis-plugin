import os

from qgis.PyQt import uic
from qgis.PyQt import QtWidgets
from qgis.PyQt.QtWidgets import QDialog, QVBoxLayout, QLabel, QLineEdit, QPushButton, QMessageBox, QVBoxLayout
from .login_tab import LoginDialog

# This loads your .ui file so that PyQt can populate your plugin with the elements from Qt Designer
FORM_CLASS_start, _ = uic.loadUiType(os.path.join(
    os.path.dirname(__file__), './ui_files/signup.ui'))


class SignupDialog(QtWidgets.QDialog):

    def __init__(self, osm_parser=None):
        """Constructor."""
        super().__init__()
        uic.loadUi(os.path.join(
            os.path.dirname(__file__), './ui_files/signup.ui'), self)        # Set up the user interface from Designer through FORM_CLASS.
        # After self.setupUi() you can access any designer object by doing
        # self.<objectname>, and you can use autoconnect slots - see
        # http://qt-project.org/doc/qt-4.8/designer-using-a-ui-file.html
        # #widgets-and-dialogs-with-auto-connect
        self.main_ui = osm_parser
        self.login_button.clicked.connect(self.handle_login_logout)

    def handle_login_logout(self):
        if self.main_ui.user_logged_in:
            pass
        else:
            self.login()

    def login(self):
        self.hide()
        login_dialog = LoginDialog(self.main_ui)
        if login_dialog.exec_():
            self.main_ui.user_logged_in = True
            login_dialog.hide()
            self.accept()
