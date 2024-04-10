import os

from qgis.PyQt import uic
from qgis.PyQt import QtWidgets
from .login_tab import LoginDialog
from .signup_tab import SignupDialog

try:
    import webbrowser
except ModuleNotFoundError:
    import pip

    pip.main(['install', 'webbrowser'])
    import webbrowser

# This loads your .ui file so that PyQt can populate your plugin with the elements from Qt Designer
FORM_CLASS_start, _ = uic.loadUiType(os.path.join(
    os.path.dirname(__file__), './ui_files/start.ui'))


class OsmParserDialogStart(QtWidgets.QDialog, FORM_CLASS_start):

    def __init__(self, parent=None, osm_parser=None):
        """Constructor."""
        super(OsmParserDialogStart, self).__init__(parent)
        # Set up the user interface from Designer through FORM_CLASS.
        # After self.setupUi() you can access any designer object by doing
        # self.<objectname>, and you can use autoconnect slots - see
        # http://qt-project.org/doc/qt-4.8/designer-using-a-ui-file.html
        # #widgets-and-dialogs-with-auto-connect
        self.setupUi(self)
        self.main_ui = osm_parser
        self.login_button.clicked.connect(self.handle_login_logout)
        self.signup.clicked.connect(self.handle_signup)

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

    def handle_signup(self):
        self.hide()
        auth_url = 'https://beta.aino.world/auth'
        webbrowser.open(auth_url, new=0, autoraise=True)
        signup_dialog = SignupDialog(self.main_ui)
        if signup_dialog.exec_():
            self.main_ui.user_logged_in = True
            signup_dialog.hide()
            self.accept()
