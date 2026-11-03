import os

from qgis.PyQt import uic
from qgis.PyQt import QtWidgets

# This loads your .ui file so that PyQt can populate your plugin with the elements from Qt Designer
FORM_CLASS_start, _ = uic.loadUiType(
    os.path.join(os.path.dirname(__file__), "./ui_files/signup.ui")
)


class SignupDialog(QtWidgets.QDialog):
    def __init__(self, osm_parser=None, parent=None):
        """Constructor."""
        super().__init__(parent)
        uic.loadUi(
            os.path.join(os.path.dirname(__file__), "./ui_files/signup.ui"), self
        )
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
        if self.main_ui.unloading:
            return
        self.hide()
        if self.main_ui.exec_login(self):
            self.accept()
        elif not self.main_ui.unloading:
            self.show()
