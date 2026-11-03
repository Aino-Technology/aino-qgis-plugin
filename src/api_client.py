import json
from dataclasses import dataclass

from qgis.PyQt.QtCore import QByteArray, QObject, QThread, QUrl
from qgis.PyQt.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest


@dataclass(frozen=True)
class ApiResponse:
    """One HTTP completion, with status zero for a local or network failure."""

    status: int
    data: object = None
    error: str = ""

    @property
    def ok(self):
        return 200 <= self.status < 300 and not self.error

    @property
    def message(self):
        if isinstance(self.data, dict):
            for key in ("message", "detail", "error_description", "error"):
                value = self.data.get(key)
                if isinstance(value, str):
                    return value
        return self.error or f"HTTP {self.status}"


class RequestHandle:
    """Abort local delivery without cancelling an accepted server operation."""

    def __init__(self, client, method, path, body, callback, account_id, authenticated):
        self.client = client
        self.method = method
        self.path = path
        self.body = body
        self.callback = callback
        self.account_id = account_id
        self.authenticated = authenticated
        self.waiting_for_login = False
        self.reply = None
        self.sent_token = None
        self.retries = 0
        self.active = True

    def abort(self):
        if self.active:
            self.client._abort(self)


class ApiClient(QObject):
    """Send API requests on the Qt thread, retaining one same-account auth retry."""

    def __init__(self, session, api_origin, parent=None):
        super().__init__(parent)
        origin = QUrl(api_origin)
        if (
            origin.scheme() not in {"https", "http"}
            or not origin.host()
            or origin.userInfo()
            or origin.path() not in {"", "/"}
            or origin.hasQuery()
            or origin.hasFragment()
        ):
            raise ValueError(
                "API origin must contain only scheme, host and optional port"
            )
        if origin.scheme() == "http" and origin.host() not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("API origin must use HTTPS")
        if session.thread() != self.thread():
            raise RuntimeError("ApiClient and Session must share the Qt thread")
        self.session = session
        self.api_origin = api_origin.rstrip("/")
        self.manager = QNetworkAccessManager(self)
        self._pending = set()
        self._closed = False
        session.authenticated.connect(self._resume)
        session.account_changed.connect(self._account_changed)

    @property
    def pending_count(self):
        return len(self._pending)

    def send(self, method: str, path: str, payload: dict | None, callback):
        """Call callback(ApiResponse) once, unless aborted; paths must be relative to this API."""
        self._check_thread()
        if self._closed:
            raise RuntimeError("API client is closed")
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or path.startswith("//")
            or "\\" in path
            or any(ord(character) < 32 for character in path)
        ):
            raise ValueError(
                "API requests require a relative path beginning with one slash"
            )
        url = QUrl(self.api_origin + path)
        if not url.isValid() or url.hasFragment():
            raise ValueError(
                "API requests require a valid relative path without a fragment"
            )
        method = method.upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError("Unsupported HTTP method")
        body = (
            None
            if payload is None
            else json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
        )
        authenticated = path.split("?", 1)[0] != "/public/auth/login"
        handle = RequestHandle(
            self,
            method,
            path,
            body,
            callback,
            self.session.account_id if authenticated else None,
            authenticated,
        )
        self._pending.add(handle)
        if authenticated and not self.session.access_token:
            handle.waiting_for_login = True
            self.session.require_login()
        else:
            self._dispatch(handle)
        return handle

    def close(self):
        """Abort all local requests and suppress callbacks during plugin unload."""
        self._check_thread()
        self._closed = True
        for handle in tuple(self._pending):
            self._abort(handle)

    def _dispatch(self, handle):
        if not handle.active:
            return
        handle.waiting_for_login = False
        if handle.authenticated:
            if handle.account_id is None:
                handle.account_id = self.session.account_id
            if handle.account_id != self.session.account_id:
                self._complete(handle, ApiResponse(0, error="account_changed"))
                return
        request = QNetworkRequest(QUrl(self.api_origin + handle.path))
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.ManualRedirectPolicy,
        )
        request.setTransferTimeout(30000)
        request.setHeader(
            QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json"
        )
        handle.sent_token = self.session.access_token if handle.authenticated else None
        if handle.sent_token:
            request.setRawHeader(
                b"Authorization", f"Bearer {handle.sent_token}".encode()
            )
        body = QByteArray(handle.body or b"")
        handle.reply = self.manager.sendCustomRequest(
            request, handle.method.encode(), body
        )
        handle.reply.finished.connect(lambda: self._finished(handle))

    def _finished(self, handle):
        reply = handle.reply
        if not handle.active or reply is None:
            return
        handle.reply = None
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) or 0
        raw = bytes(reply.readAll())
        network_error = reply.error() != QNetworkReply.NetworkError.NoError
        reply.deleteLater()
        if handle.authenticated and status == 401:
            if handle.retries == 0:
                handle.retries += 1
                handle.waiting_for_login = True
                if (
                    self.session.access_token
                    and self.session.access_token != handle.sent_token
                ):
                    self._dispatch(handle)
                else:
                    self.session.require_login()
                return
            current_token_failed = self.session.access_token == handle.sent_token
            self._complete(handle, ApiResponse(401, error="authentication_failed"))
            if current_token_failed:
                self.session.require_login()
            return
        if 300 <= status < 400:
            self._complete(handle, ApiResponse(status, error="redirect_refused"))
            return
        if network_error and (not status or 200 <= status < 300):
            self._complete(handle, ApiResponse(status, error="network_error"))
            return
        try:
            data = json.loads(raw) if raw else None
        except (ValueError, UnicodeDecodeError):
            self._complete(handle, ApiResponse(status, error="invalid_json"))
            return
        error = (
            "" if 200 <= status < 300 else "http_error" if status else "network_error"
        )
        self._complete(handle, ApiResponse(status, data, error))

    def _resume(self):
        for handle in tuple(self._pending):
            if handle.waiting_for_login:
                self._dispatch(handle)

    def _account_changed(self, previous, current):
        for handle in tuple(self._pending):
            if (
                handle.authenticated
                and handle.account_id is not None
                and handle.account_id != current
            ):
                self._complete(handle, ApiResponse(0, error="account_changed"))

    def _complete(self, handle, response):
        if not handle.active:
            return
        callback = handle.callback
        self._abort(handle)
        callback(response)

    def _abort(self, handle):
        self._check_thread()
        handle.active = False
        handle.waiting_for_login = False
        handle.callback = None
        handle.body = None
        handle.sent_token = None
        self._pending.discard(handle)
        reply = handle.reply
        handle.reply = None
        if reply is not None:
            reply.abort()
            reply.deleteLater()

    def _check_thread(self):
        if QThread.currentThread() != self.thread():
            raise RuntimeError("ApiClient must be used from its Qt thread")
