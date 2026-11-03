from qgis.PyQt.QtCore import QSettings, QTranslator, QCoreApplication
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

# Initialize Qt resources from file resources.py
from .resources import *
# Import the code for the dialog
# from .osm_parser_dialog import OsmParserDialogStart, OsmParserDialogMain

import os.path
import sys

plugin_dir = os.path.dirname(__file__)

if plugin_dir not in sys.path:
    sys.path.insert(0, plugin_dir)

from src import API_LINK
from src.api_client import ApiClient
from src.session import Session
from src.login.login_tab import LoginDialog
from src.login.signup_tab import SignupDialog
from src.login import exec_dialog
from src.main_tab import OsmParserDialogMain


class OsmParser:
    """QGIS Plugin Implementation."""

    def __init__(self, iface, api_origin=API_LINK):
        """Constructor.

        :param iface: An interface instance that will be passed to this class
            which provides the hook by which you can manipulate the QGIS
            application at run time.
        :type iface: QgsInterface
        """
        # Save reference to the QGIS interface
        self.main_dlg = None
        self.start_dlg = None
        self.iface = iface
        # initialize plugin directory
        self.plugin_dir = os.path.dirname(__file__)
        # initialize locale
        locale = (QSettings().value('locale/userLocale') or 'en')[0:2]
        locale_path = os.path.join(
            self.plugin_dir,
            'i18n',
            'OsmParser_{}.qm'.format(locale))

        if os.path.exists(locale_path):
            self.translator = QTranslator()
            self.translator.load(locale_path)
            QCoreApplication.installTranslator(self.translator)

        # Declare instance attributes
        self.actions = []
        self.menu = self.tr(u'&OSM Parser')

        # Check if plugin was started the first time in current QGIS session
        # Must be set in initGui() to survive plugin reloads
        self.first_start = None
        self.user_email = None
        self.session = Session()
        self.api_client = ApiClient(self.session, api_origin)
        self.session.login_required.connect(self._require_login)
        self._login_dialog = None
        self._auth_dialogs = set()
        self._unloading = False

    @property
    def unloading(self):
        return self._unloading

    @property
    def user_logged_in(self):
        return bool(self.session.access_token)

    @property
    def bearer_token(self):
        return self.session.access_token

    def _own_auth_dialog(self, dialog):
        self._auth_dialogs.add(dialog)
        dialog.destroyed.connect(lambda: self._auth_dialogs.discard(dialog))
        return dialog

    def _get_login_dialog(self, parent=None):
        if self._login_dialog is None:
            dialog = self._own_auth_dialog(LoginDialog(self, parent))
            self._login_dialog = dialog
            dialog.finished.connect(lambda result: self._login_closed(dialog))
        return self._login_dialog

    def _require_login(self):
        if self.unloading:
            return
        if self._login_dialog is None:
            self._get_login_dialog().open()
        self._login_dialog.raise_()
        self._login_dialog.activateWindow()

    def exec_login(self, parent=None):
        """Run the plugin-owned login dialog unless unload has started."""
        if self.unloading:
            return False
        return bool(exec_dialog(self._get_login_dialog(parent))) and not self.unloading

    def exec_signup(self, parent=None):
        """Run a plugin-owned signup dialog without reopening it after unload."""
        if self.unloading:
            return False
        dialog = self._own_auth_dialog(SignupDialog(self, parent))
        try:
            return bool(exec_dialog(dialog)) and not self.unloading
        finally:
            self._auth_dialogs.discard(dialog)
            dialog.deleteLater()

    def _login_closed(self, dialog):
        if self._login_dialog is dialog:
            self._login_dialog = None
        self._auth_dialogs.discard(dialog)
        dialog.deleteLater()

    # noinspection PyMethodMayBeStatic
    def tr(self, message):
        """Get the translation for a string using Qt translation API.

        We implement this ourselves since we do not inherit QObject.

        :param message: String for translation.
        :type message: str, QString

        :returns: Translated version of message.
        :rtype: QString
        """
        # noinspection PyTypeChecker,PyArgumentList,PyCallByClass
        return QCoreApplication.translate('OsmParser', message)

    def add_action(
            self,
            icon_path,
            text,
            callback,
            enabled_flag=True,
            add_to_menu=True,
            add_to_toolbar=True,
            status_tip=None,
            whats_this=None,
            parent=None):
        """Add a toolbar icon to the toolbar.

        :param icon_path: Path to the icon for this action. Can be a resource
            path (e.g. ':/plugins/foo/bar.png') or a normal file system path.
        :type icon_path: str

        :param text: Text that should be shown in menu items for this action.
        :type text: str

        :param callback: Function to be called when the action is triggered.
        :type callback: function

        :param enabled_flag: A flag indicating if the action should be enabled
            by default. Defaults to True.
        :type enabled_flag: bool

        :param add_to_menu: Flag indicating whether the action should also
            be added to the menu. Defaults to True.
        :type add_to_menu: bool

        :param add_to_toolbar: Flag indicating whether the action should also
            be added to the toolbar. Defaults to True.
        :type add_to_toolbar: bool

        :param status_tip: Optional text to show in a popup when mouse pointer
            hovers over the action.
        :type status_tip: str

        :param parent: Parent widget for the new action. Defaults None.
        :type parent: QWidget

        :param whats_this: Optional text to show in the status bar when the
            mouse pointer hovers over the action.

        :returns: The action that was created. Note that the action is also
            added to self.actions list.
        :rtype: QAction
        """

        icon = QIcon(icon_path)
        action = QAction(icon, text, parent)
        action.triggered.connect(callback)
        action.setEnabled(enabled_flag)

        if status_tip is not None:
            action.setStatusTip(status_tip)

        if whats_this is not None:
            action.setWhatsThis(whats_this)

        if add_to_toolbar:
            # Adds plugin icon to Plugins toolbar
            self.iface.addToolBarIcon(action)

        if add_to_menu:
            self.iface.addPluginToMenu(
                self.menu,
                action)

        self.actions.append(action)

        return action

    def initGui(self):
        """Create the menu entries and toolbar icons inside the QGIS GUI."""

        icon_path = ':/plugins/osm_parser/icon.svg'
        self.add_action(
            icon_path,
            text=self.tr(u'Parse OSM with AI'),
            callback=self.run,
            parent=self.iface.mainWindow())

        # will be set False in run()
        self.first_start = True

    def unload(self):
        """Removes the plugin menu item and icon from QGIS GUI."""
        if self.unloading:
            return
        self._unloading = True
        if self.main_dlg is not None:
            self.main_dlg.result_controller.close()
            self.main_dlg.analysis_controller.close()
            self.main_dlg.sync_controller.close()
        self.api_client.close()
        for dialog in tuple(self._auth_dialogs):
            dialog.close()
        self._auth_dialogs.clear()
        if self.main_dlg is not None:
            self.main_dlg.close()
        self.session.clear()
        for action in self.actions:
            self.iface.removePluginMenu(
                self.tr(u'&OSM Parser'),
                action)
            self.iface.removeToolBarIcon(action)

    def run(self):
        """Run method that performs all the real work"""

        if self.unloading:
            return

        # Create the dialog with elements (after translation) and keep reference
        # Only create GUI ONCE in callback, so that it will only load when the plugin is started
        if self.first_start:
            self.first_start = False
            self.main_dlg = OsmParserDialogMain(osm_parser=self)

        # show the dialog
        if not self.user_logged_in:
            result = self.exec_login()
            if self.unloading:
                return

            if result:
                self.main_dlg.update_interface_with_restrictions_start()
                self.main_dlg.set_account_email(self.user_email)
                self.main_dlg.show()

        else:
            self.main_dlg.update_interface_with_restrictions_start()
            self.main_dlg.show()
