import base64
import json

from qgis.PyQt.QtCore import QObject, QThread, pyqtSignal


class Session(QObject):
    """Keep credentials and account routing identity in memory for this plugin session."""

    login_required = pyqtSignal()
    authenticated = pyqtSignal()
    account_changed = pyqtSignal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._access_token = None
        self._account_id = None
        self._login_pending = False

    @property
    def access_token(self):
        return self._access_token

    @property
    def account_id(self):
        return self._account_id

    def set_access_token(self, token: str):
        """Accept a token from login; its unverified subject is only local bookkeeping."""
        self._check_thread()
        try:
            encoded = token.split(".")[1]
            claims = json.loads(
                base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            )
            account_id = claims["sub"]
            if not isinstance(account_id, str) or not account_id:
                raise ValueError
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as error:
            raise ValueError(
                "Login did not return a token with an account identity"
            ) from error
        previous = self._account_id
        self._access_token = token
        self._account_id = account_id
        self._login_pending = False
        if previous != account_id:
            self.account_changed.emit(previous, account_id)
        self.authenticated.emit()

    def require_login(self):
        """Coalesce authentication recovery while retaining the previous account identity."""
        self._check_thread()
        self._access_token = None
        if not self._login_pending:
            self._login_pending = True
            self.login_required.emit()

    def cancel_login(self):
        """Allow another login prompt after dismissal without losing pending operations."""
        self._check_thread()
        self._login_pending = False

    def clear(self):
        """Forget credentials and account identity without persisting them."""
        self._check_thread()
        previous = self._account_id
        self._access_token = None
        self._account_id = None
        self._login_pending = False
        if previous is not None:
            self.account_changed.emit(previous, None)

    def _check_thread(self):
        if QThread.currentThread() != self.thread():
            raise RuntimeError("Session must be used from its Qt thread")
