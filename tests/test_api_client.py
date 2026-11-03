import base64
import importlib.util
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from qgis.PyQt.QtCore import QSettings, QThread, QTimer
from qgis.PyQt.QtWidgets import QWidget
from qgis.core import QgsProject


def token(account="account-a", generation=1):
    claims = (
        base64.urlsafe_b64encode(
            json.dumps({"sub": account, "generation": generation}).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    return f"header.{claims}.test-signature"


def test_async_bearer_callback_in_qt_thread_and_immutable_payload(
    client, local_api, wait_for, qgis_app
):
    session, api = client
    session.set_access_token(token())
    received = []
    threads = []
    ticks = []
    heartbeat = QTimer()
    heartbeat.timeout.connect(lambda: ticks.append(True))
    heartbeat.start(1)
    payload = {"request_id": str(uuid4()), "title": "QGIS"}
    expected = json.dumps(payload, separators=(",", ":")).encode()
    api.send(
        "POST",
        "/integrations/qgis/projects",
        payload,
        lambda result: (
            received.append(result),
            threads.append(QThread.currentThread()),
        ),
    )
    payload["title"] = "changed after send"
    wait_for(lambda: len(local_api.requests) == 1 and len(ticks) >= 3)
    assert not received
    request = local_api.requests[0]
    assert request["headers"]["Authorization"] == f"Bearer {token()}"
    assert request["body"] == expected
    local_api.respond(0, 200, {"project_id": 7})
    wait_for(lambda: bool(received))
    heartbeat.stop()
    assert received[0].ok and received[0].data == {"project_id": 7}
    assert threads == [qgis_app.thread()]


def test_login_uses_public_contract_and_does_not_block(client, local_api, wait_for):
    from src.login.login_tab import LoginDialog

    session, api = client
    owner = SimpleNamespace(session=session, api_client=api)
    dialog = LoginDialog(owner)
    dialog.username.setText("test@example.invalid")
    dialog.password.setText("test-password")
    accepted = []
    dialog.accepted.connect(lambda: accepted.append(True))
    ticks = []
    QTimer.singleShot(0, lambda: ticks.append(True))
    dialog.check_credentials()
    wait_for(lambda: len(local_api.requests) == 1 and bool(ticks))
    assert not accepted and not dialog.process_login.isEnabled()
    request = local_api.requests[0]
    assert request["path"] == "/public/auth/login"
    assert "Authorization" not in request["headers"]
    assert json.loads(request["body"]) == {
        "email": "test@example.invalid",
        "password": "test-password",
    }
    local_api.respond(0, 200, {"access_token": token(), "user": {"id": "account-a"}})
    wait_for(lambda: bool(accepted))
    assert session.account_id == "account-a" and session.access_token == token()
    assert dialog.password.text() == ""
    dialog.deleteLater()


def test_401_coalesces_login_and_replays_exact_request_identity(
    client, local_api, wait_for
):
    session, api = client
    session.set_access_token(token())
    login = []
    session.login_required.connect(lambda: login.append(True))
    results = []
    payload = {"request_id": str(uuid4()), "title": "project"}
    handles = [
        api.send("POST", "/integrations/qgis/projects", payload, results.append)
        for _ in range(2)
    ]
    wait_for(lambda: len(local_api.requests) == 2)
    for index in range(2):
        local_api.respond(index, 401, {"error": "expired"})
    wait_for(
        lambda: len(login) == 1 and all(handle.waiting_for_login for handle in handles)
    )
    assert not results
    session.set_access_token(token(generation=2))
    wait_for(lambda: len(local_api.requests) == 4)
    for index in range(2, 4):
        assert (
            local_api.requests[index]["body"] == local_api.requests[index - 2]["body"]
        )
        assert (
            local_api.requests[index]["headers"]["Authorization"]
            == f"Bearer {token(generation=2)}"
        )
        local_api.respond(index, 200, {"project_id": 1})
    wait_for(lambda: len(results) == 2)
    assert all(result.ok for result in results)


def test_late_401_keeps_new_token_and_replays_once(client, local_api, wait_for):
    session, api = client
    session.set_access_token(token())
    login = []
    session.login_required.connect(lambda: login.append(True))
    received = []
    api.send("GET", "/user/limits", None, received.append)
    wait_for(lambda: len(local_api.requests) == 1)
    session.set_access_token(token(generation=2))
    local_api.respond(0, 401, {"error": "old token expired"})
    wait_for(lambda: len(local_api.requests) == 2)
    assert session.access_token == token(generation=2) and not login
    local_api.respond(1, 401, {"error": "expired"})
    wait_for(lambda: bool(received))
    assert received[0].status == 401 and not received[0].ok
    assert len(local_api.requests) == 2


def test_account_change_never_replays_foreign_project_operation(
    client, local_api, wait_for
):
    session, api = client
    session.set_access_token(token())
    received = []
    handle = api.send(
        "POST",
        "/integrations/qgis/projects/9/snapshots",
        {"request_id": str(uuid4()), "layers": []},
        received.append,
    )
    wait_for(lambda: len(local_api.requests) == 1)
    local_api.respond(0, 401, {})
    wait_for(lambda: handle.waiting_for_login)
    session.set_access_token(token("account-b"))
    wait_for(lambda: bool(received))
    assert received[0].error == "account_changed"
    assert len(local_api.requests) == 1
    assert session.account_id == "account-b"


def test_abort_and_close_suppress_callbacks(client, local_api, wait_for):
    session, api = client
    session.set_access_token(token())
    received = []
    handle = api.send("GET", "/user/limits", None, received.append)
    wait_for(lambda: len(local_api.requests) == 1)
    handle.abort()
    api.send("GET", "/user/current_subscription", None, received.append)
    wait_for(lambda: len(local_api.requests) == 2)
    api.close()
    finished = []
    QTimer.singleShot(20, lambda: finished.append(True))
    wait_for(lambda: bool(finished))
    assert not received
    assert not api.pending_count


def test_cross_origin_absolute_urls_and_redirects_never_receive_bearer(
    client, local_api, wait_for
):
    session, api = client
    session.set_access_token(token())
    for path in (
        "https://files.example.invalid/export",
        "//files.example.invalid/export",
    ):
        with pytest.raises(ValueError, match="relative"):
            api.send("GET", path, None, lambda result: None)
    received = []
    api.send("GET", "/redirect", None, received.append)
    wait_for(lambda: len(local_api.requests) == 1)
    local_api.respond(0, 302, {}, {"Location": "http://127.0.0.1:1/secret"})
    wait_for(lambda: bool(received))
    assert received[0].status == 302 and received[0].error == "redirect_refused"
    assert len(local_api.requests) == 1


def test_limit_errors_are_server_authoritative_without_ai_query_gate(
    client, local_api, wait_for
):
    from src.main_tab import OsmParserDialogMain

    session, api = client
    session.set_access_token(token())
    dialog = OsmParserDialogMain(
        osm_parser=SimpleNamespace(session=session, api_client=api)
    )
    dialog.prompt_field.setPlainText("Keep my analysis prompt")
    dialog.update_interface_with_restrictions_start()
    wait_for(lambda: len(local_api.requests) == 2)
    for index, request in enumerate(local_api.requests):
        if request["path"] == "/user/limits":
            local_api.respond(
                index,
                200,
                {"limits": [{"name": "ai_query", "left_amount": 0, "total_amount": 0}]},
            )
        else:
            local_api.respond(
                index, 200, {"current_subscription": {"subscription_plan_code": "free"}}
            )
    wait_for(lambda: api.pending_count == 0)
    assert dialog.go_button.isEnabled() and not dialog.prompt_field.isReadOnly()
    assert dialog.prompt_field.toPlainText() == "Keep my analysis prompt"
    dialog.get_current_limits()
    wait_for(lambda: len(local_api.requests) == 3)
    local_api.respond(2, 402, {"message": "Insufficient analysis credits"})
    wait_for(lambda: "Insufficient analysis credits" in dialog.prompts_label.text())
    dialog.project_id = "9"
    session.set_access_token(token("account-b"))
    assert dialog.project_id is None
    dialog.close()
    dialog.deleteLater()


def test_plugin_fresh_locale_resources_and_unload(
    client, local_api, qgis_app, wait_for, monkeypatch
):
    from pathlib import Path
    import sys
    import src

    root = Path(__file__).resolve().parents[1]
    monkeypatch.setattr(src, "API_LINK", local_api.origin)
    spec = importlib.util.spec_from_file_location(
        "aino_plugin_test", root / "__init__.py", submodule_search_locations=[str(root)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    QSettings().remove("locale/userLocale")
    window = QWidget()
    removed = []
    iface = SimpleNamespace(
        mainWindow=lambda: window,
        addToolBarIcon=lambda action: None,
        addPluginToMenu=lambda *args: None,
        removePluginMenu=lambda *args: removed.append(True),
        removeToolBarIcon=lambda action: removed.append(True),
    )
    plugin = module.classFactory(iface)
    assert plugin.api_client.api_origin == local_api.origin
    plugin.initGui()
    assert len(plugin.actions) == 1 and not plugin.actions[0].icon().isNull()
    plugin.session.set_access_token(token())
    received = []
    plugin.api_client.send("GET", "/user/limits", None, received.append)
    wait_for(lambda: len(local_api.requests) == 1)
    plugin.unload()
    assert len(removed) == 2 and plugin.session.access_token is None
    assert not plugin.api_client.pending_count and not received
    window.deleteLater()


def test_existing_add_data_preserves_basic_vector_import(qgis_app):
    from src.main_tab.helpers import add_data

    project = QgsProject.instance()
    project.clear()
    data = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [1, 2]},
                "properties": {"name": "test"},
            }
        ],
    }
    add_data(json.dumps(data), "existing")
    layers = list(project.mapLayers().values())
    assert len(layers) == 1 and layers[0].featureCount() == 1
    assert next(layers[0].getFeatures())["name"] == "test"
    project.clear()


def test_cancelled_login_can_be_requested_again_without_losing_operation(
    client, local_api, wait_for
):
    from src.login.login_tab import LoginDialog

    session, api = client
    session.set_access_token(token())
    login = []
    session.login_required.connect(lambda: login.append(True))
    received = []
    handle = api.send(
        "POST",
        "/integrations/qgis/projects",
        {"request_id": str(uuid4())},
        received.append,
    )
    wait_for(lambda: len(local_api.requests) == 1)
    local_api.respond(0, 401, {})
    wait_for(lambda: handle.waiting_for_login)
    dialog = LoginDialog(SimpleNamespace(session=session, api_client=api))
    dialog.reject()
    session.require_login()
    assert len(login) == 2
    assert handle.waiting_for_login and not received
    dialog.deleteLater()


def test_destroyed_login_aborts_request_and_clears_password(
    client, local_api, wait_for, qgis_app
):
    from qgis.PyQt.QtCore import QCoreApplication, QEvent
    from src.login.login_tab import LoginDialog

    session, api = client
    dialog = LoginDialog(SimpleNamespace(session=session, api_client=api))
    dialog.username.setText("test@example.invalid")
    dialog.password.setText("ephemeral-password")
    dialog.check_credentials()
    wait_for(lambda: len(local_api.requests) == 1)
    assert not dialog.password.text()
    dialog.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qgis_app.processEvents()
    assert api.pending_count == 0 and session.access_token is None


def test_window_close_aborts_local_requests_without_server_cancel(
    client, local_api, wait_for
):
    from src.main_tab import OsmParserDialogMain

    session, api = client
    session.set_access_token(token())
    dialog = OsmParserDialogMain(
        osm_parser=SimpleNamespace(session=session, api_client=api)
    )
    dialog.get_current_limits()
    wait_for(lambda: len(local_api.requests) == 1)
    dialog.close()
    assert api.pending_count == 0
    assert len(local_api.requests) == 1 and local_api.requests[0]["method"] == "GET"
    dialog.deleteLater()


def test_server_errors_and_malformed_json_complete_once(client, local_api, wait_for):
    session, api = client
    session.set_access_token(token())
    received = []
    api.send("GET", "/user/limits", None, received.append)
    wait_for(lambda: len(local_api.requests) == 1)
    local_api.respond(
        0, 409, {"message": "Snapshot changed", "code": "qgis_request_conflict"}
    )
    wait_for(lambda: len(received) == 1)
    assert received[0].status == 409 and received[0].message == "Snapshot changed"
    assert received[0].data["code"] == "qgis_request_conflict"
    api.send("GET", "/user/limits", None, received.append)
    wait_for(lambda: len(local_api.requests) == 2)
    local_api.respond(1, 200, raw=b"not JSON")
    wait_for(lambda: len(received) == 2)
    assert received[1].error == "invalid_json" and not received[1].ok


def test_limits_error_survives_later_subscription_success(client, local_api, wait_for):
    from src.main_tab import OsmParserDialogMain

    session, api = client
    session.set_access_token(token())
    dialog = OsmParserDialogMain(
        osm_parser=SimpleNamespace(session=session, api_client=api)
    )
    dialog.update_interface_with_restrictions_start()
    wait_for(lambda: len(local_api.requests) == 2)
    limits = next(
        index
        for index, request in enumerate(local_api.requests)
        if request["path"] == "/user/limits"
    )
    subscription = 1 - limits
    local_api.respond(limits, 402, {"message": "Credits unavailable"})
    wait_for(lambda: dialog.prompts_label.text() == "Credits unavailable")
    local_api.respond(
        subscription, 200, {"current_subscription": {"subscription_plan_code": "free"}}
    )
    wait_for(lambda: api.pending_count == 0)
    assert dialog.prompts_label.text() == "Credits unavailable"
    dialog.close()
    dialog.deleteLater()


def test_completed_handle_abort_remains_safe_after_client_destruction(
    client, local_api, wait_for, qgis_app
):
    from qgis.PyQt.QtCore import QCoreApplication, QEvent

    session, api = client
    session.set_access_token(token())
    received = []
    handle = api.send("GET", "/user/limits", None, received.append)
    wait_for(lambda: len(local_api.requests) == 1)
    local_api.respond(0, 200, {})
    wait_for(lambda: bool(received))
    api.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    handle.abort()


def test_metadata_accepts_actual_host_version(qgis_app):
    from configparser import ConfigParser
    from pathlib import Path
    from qgis.core import Qgis

    metadata = ConfigParser()
    metadata.read(Path(__file__).resolve().parents[1] / "metadata.txt")
    minimum = metadata["general"]["qgisMinimumVersion"]
    maximum = metadata["general"].get("qgisMaximumVersion", minimum[0] + ".99")
    current = tuple(map(int, Qgis.QGIS_VERSION.split("-")[0].split(".")))
    lower = tuple(map(int, minimum.split(".")))
    upper = tuple(map(int, maximum.split(".")))
    assert lower <= current <= upper


def test_truncated_success_body_is_a_network_failure(client, local_api, wait_for):
    session, api = client
    session.set_access_token(token())
    received = []
    api.send("GET", "/user/limits", None, received.append)
    wait_for(lambda: len(local_api.requests) == 1)
    local_api.respond(0, 200, {}, {"Content-Length": "100"})
    wait_for(lambda: bool(received))
    assert not received[0].ok and received[0].error == "network_error"


def test_account_change_detaches_all_old_requests_before_reentrant_callback(
    client, local_api, wait_for
):
    session, api = client
    session.set_access_token(token())
    completions = []
    fresh = []
    callbacks_active = []
    callback_errors = []
    handles = []

    def completed(index, response):
        completions.append((index, response, session.account_id))
        if len(completions) != 1:
            return
        try:
            callbacks_active.append([handle.active for handle in handles])
            local_api.respond(1 - index, 200, {"owner": "account-a"})
            api.send("GET", "/fresh-account-operation", None, fresh.append)
            wait_for(lambda: len(local_api.requests) == 3)
            local_api.respond(2, 200, {"owner": "account-b"})
            wait_for(lambda: bool(fresh))
        except Exception as error:
            callback_errors.append(error)

    for index in range(2):
        handles.append(
            api.send(
                "GET",
                f"/old-operation-{index}",
                None,
                lambda response, index=index: completed(index, response),
            )
        )
    wait_for(lambda: len(local_api.requests) == 2)
    session.set_access_token(token("account-b"))
    assert not callback_errors
    assert callbacks_active == [[False, False]]
    assert len(completions) == 2
    assert all(
        response.error == "account_changed" and owner == "account-b"
        for _, response, owner in completions
    )
    assert fresh[0].ok and fresh[0].data == {"owner": "account-b"}
    assert (
        local_api.requests[2]["headers"]["Authorization"]
        == f"Bearer {token('account-b')}"
    )


def test_completion_checks_identity_during_earlier_account_change_listener(
    local_api, wait_for, qgis_app
):
    from src.api_client import ApiClient
    from src.session import Session

    session = Session()
    received = []
    callback_errors = []

    def earlier_listener(previous, current):
        if previous and current != previous:
            try:
                local_api.respond(0, 200, {"owner": "account-a"})
                wait_for(lambda: bool(received))
            except Exception as error:
                callback_errors.append(error)

    session.account_changed.connect(earlier_listener)
    api = ApiClient(session, local_api.origin)
    try:
        session.set_access_token(token())
        api.send("GET", "/old-operation", None, received.append)
        wait_for(lambda: len(local_api.requests) == 1)
        session.set_access_token(token("account-b"))
        assert not callback_errors
        assert len(received) == 1 and received[0].error == "account_changed"
        assert not received[0].ok
    finally:
        api.close()
        api.deleteLater()
        session.deleteLater()


@pytest.mark.parametrize("entry", ["start", "signup"])
def test_unload_closes_nested_owned_auth_dialogs_without_reopening(
    local_api, wait_for, qgis_app, monkeypatch, entry
):
    from pathlib import Path
    import sys
    import webbrowser
    import src
    from qgis.PyQt import sip
    from src.login.login_tab import LoginDialog
    from src.login.signup_tab import SignupDialog

    root = Path(__file__).resolve().parents[1]
    monkeypatch.setattr(src, "API_LINK", local_api.origin)
    monkeypatch.setattr(webbrowser, "open", lambda *args, **kwargs: True)
    name = f"aino_plugin_modal_test_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        name, root / "__init__.py", submodule_search_locations=[str(root)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    window = QWidget()
    unrelated = QWidget()
    unrelated.show()
    iface = SimpleNamespace(
        mainWindow=lambda: window,
        addToolBarIcon=lambda action: None,
        addPluginToMenu=lambda *args: None,
        removePluginMenu=lambda *args: None,
        removeToolBarIcon=lambda action: None,
    )
    plugin = module.classFactory(iface)
    plugin.initGui()
    owned = []
    visible_after_unload = []
    errors = []

    def visible(widget):
        return not sip.isdeleted(widget) and widget.isVisible()

    def stop_from_login():
        try:
            dialog = next(
                widget
                for widget in qgis_app.topLevelWidgets()
                if isinstance(widget, LoginDialog) and widget.isVisible()
            )
            owned.append(dialog)
            dialog.username.setText("test@example.invalid")
            dialog.password.setText("test-password")
            dialog.check_credentials()
            wait_for(lambda: len(local_api.requests) == 1)
            plugin.unload()
            dialog.check_credentials()
            plugin.session.require_login()
            plugin.run()
            visible_after_unload.append(visible(dialog))
            if visible(dialog):
                dialog.reject()
        except Exception as error:
            errors.append(error)
            plugin.unload()
            for dialog in owned:
                if visible(dialog):
                    dialog.reject()

    def signup_login():
        try:
            dialog = next(
                widget
                for widget in qgis_app.topLevelWidgets()
                if isinstance(widget, SignupDialog) and widget.isVisible()
            )
            owned.append(dialog)
            QTimer.singleShot(0, stop_from_login)
            dialog.login()
        except Exception as error:
            errors.append(error)
            plugin.unload()

    def begin():
        owned.append(plugin.start_dlg)
        if entry == "start":
            QTimer.singleShot(0, stop_from_login)
            plugin.start_dlg.login()
        else:
            QTimer.singleShot(0, signup_login)
            plugin.start_dlg.handle_signup()

    try:
        QTimer.singleShot(0, begin)
        plugin.run()
        assert not errors
        assert visible_after_unload == [False]
        assert all(not visible(dialog) for dialog in owned)
        assert not visible(plugin.main_dlg)
        assert unrelated.isVisible()
        assert plugin.api_client.pending_count == 0
        assert plugin.session.access_token is None
        assert len(local_api.requests) == 1
    finally:
        for dialog in owned + [plugin.main_dlg, unrelated, window]:
            if dialog is not None and not sip.isdeleted(dialog):
                dialog.close()
                dialog.deleteLater()
        plugin.api_client.close()
