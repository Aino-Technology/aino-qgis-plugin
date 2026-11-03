import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QCoreApplication, QEvent, QTimer, Qt
from qgis.PyQt.QtWidgets import QMessageBox, QTextEdit

from src.api_client import ApiResponse


@pytest.fixture
def owned_login(local_api, monkeypatch, qgis_app):
    import src

    monkeypatch.setattr(src, "API_LINK", local_api.origin)
    root = Path(__file__).resolve().parents[1]
    name = f"aino_login_error_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        name, root / "__init__.py", submodule_search_locations=[str(root)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    plugin = module.classFactory(SimpleNamespace())
    login = plugin._get_login_dialog()
    login.open()
    titles = []
    set_window_title = QMessageBox.setWindowTitle

    def record_window_title(box, title):
        titles.append(title)
        set_window_title(box, title)

    monkeypatch.setattr(QMessageBox, "setWindowTitle", record_window_title)
    yield plugin, login, titles
    plugin.unload()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert sip.isdeleted(login)


@pytest.mark.parametrize(
    "status,payload,raw,expected,icon",
    [
        (
            400,
            {"error": "invalid_grant"},
            None,
            "email or password is incorrect",
            "Warning",
        ),
        (200, {}, None, "did not return an access token", "Critical"),
        (200, [], None, "Invalid server response", "Critical"),
        (200, {"access_token": "not-a-token"}, None, "account identity", "Critical"),
        (200, None, b"invalid-json", "Invalid server response", "Critical"),
        (0, None, None, "Unable to connect", "Critical"),
    ],
)
def test_login_errors_keep_upstream_popup_without_blocking(
    owned_login, local_api, wait_for, status, payload, raw, expected, icon
):
    plugin, login, titles = owned_login
    login.username.setText("synthetic@example.invalid")
    login.password.setText("synthetic-password")
    login.check_credentials()
    wait_for(lambda: len(local_api.requests) == 1)
    if status:
        local_api.respond(0, status, payload, raw=raw)
    else:
        local_api.respond(0, 200, {}, {"Content-Length": "100"})
    wait_for(
        lambda: plugin.api_client.pending_count == 0 and login.process_login.isEnabled()
    )
    boxes = login.findChildren(QMessageBox)
    assert len(boxes) == 1, "Upstream login error must remain an owned QMessageBox"
    box = boxes[0]
    assert box.parent() is login and box.isVisible()
    assert titles == ["Login failed"]
    if sys.platform != "darwin":
        assert box.windowTitle() == "Login failed"
    assert box.textFormat() == Qt.TextFormat.PlainText
    assert expected in box.text()
    assert box.icon() == getattr(QMessageBox.Icon, icon)
    assert box.standardButtons() == QMessageBox.StandardButton.Ok
    ticks = []
    QTimer.singleShot(0, lambda: ticks.append(True))
    wait_for(lambda: bool(ticks))
    box.button(QMessageBox.StandardButton.Ok).click()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert sip.isdeleted(box)
    assert not plugin.session.access_token


@pytest.mark.parametrize("close", ["login", "unload"])
def test_active_error_popup_closes_with_owner_and_does_not_duplicate(
    owned_login, wait_for, close
):
    plugin, login, titles = owned_login
    error = ApiResponse(400, {"error": "invalid_grant"}, "http_error")
    login._login_finished(error)
    boxes = login.findChildren(QMessageBox)
    assert len(boxes) == 1, "Login errors need the upstream popup"
    box = boxes[0]
    destroyed = []
    box.destroyed.connect(lambda: destroyed.append(True))
    login._login_finished(error)
    assert login.findChildren(QMessageBox) == [box]
    login.check_credentials()
    assert plugin.api_client.pending_count == 0
    if close == "login":
        login.close()
    else:
        plugin.unload()
    assert not box.isVisible()
    login._login_finished(error)
    assert login.findChildren(QMessageBox) == [box]
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert sip.isdeleted(box) and sip.isdeleted(login)
    assert destroyed == [True]
    assert plugin.api_client.pending_count == 0
    assert not plugin._auth_dialogs


def test_long_server_error_is_complete_plain_text_in_popup(owned_login):
    _, login, _ = owned_login
    message = "<b>literal error</b> " + "synthetic detail " * 400 + "END-OF-ERROR"
    login._login_finished(ApiResponse(403, {"message": message}, "http_error"))
    boxes = login.findChildren(QMessageBox)
    assert len(boxes) == 1
    box = boxes[0]
    assert box.textFormat() == Qt.TextFormat.PlainText
    assert box.text() == message or box.detailedText() == message
    assert box.detailedText() == message
    assert box.findChild(QTextEdit).toPlainText() == message
