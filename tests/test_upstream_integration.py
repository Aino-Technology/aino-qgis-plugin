import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QCoreApplication, QEvent
from qgis.PyQt.QtWidgets import QMessageBox

from src.api_client import ApiResponse
from src.login.login_tab import LoginDialog
from src.main_tab import OsmParserDialogMain
from src.project_sync.recovery import RecoveryJournal
from test_api_client import token


def test_login_banner_replacements_and_trimmed_async_login(client, local_api, wait_for):
    session, api = client
    owner = SimpleNamespace(session=session, api_client=api)
    dialog = LoginDialog(owner)
    dialog._text_replacements = {"@@": "person@example.invalid"}
    assert not dialog.label_2.pixmap().isNull()
    assert not dialog.label_3.geometry().intersects(dialog.username.geometry())
    assert not dialog.label_4.geometry().intersects(dialog.password.geometry())
    dialog.username.setText("  @@")
    dialog.password.setText("synthetic-password")
    dialog.check_credentials()
    wait_for(lambda: len(local_api.requests) == 1)
    assert (
        json.loads(local_api.requests[0]["body"])["email"] == "person@example.invalid"
    )
    local_api.respond(0, 400, {"error": "invalid_grant"})
    wait_for(lambda: dialog.process_login.isEnabled())
    popup = dialog.findChildren(QMessageBox)[0]
    assert "incorrect" in popup.text()
    popup.button(QMessageBox.StandardButton.Ok).click()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert sip.isdeleted(popup)
    dialog.password.setText("synthetic-password")
    dialog.check_credentials()
    wait_for(lambda: len(local_api.requests) == 2)
    local_api.respond(1, 200, {"access_token": token()})
    wait_for(lambda: session.account_id == "account-a")
    assert owner.user_email == "person@example.invalid"
    dialog.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert sip.isdeleted(dialog)


@pytest.mark.parametrize(
    "payload, expected",
    [
        (
            {"limits": [{"name": "agent_analysis", "type": "unlimited"}]},
            "Unlimited analysis requests",
        ),
        (
            {
                "limits": [
                    {
                        "name": "agent_analysis",
                        "type": "limited",
                        "left_amount": 0,
                        "total_amount": 20,
                    }
                ]
            },
            "0/20 analyses",
        ),
        (
            {
                "limits": [
                    {
                        "name": "ai_query",
                        "type": "limited",
                        "left_amount": 0,
                        "total_amount": 20,
                    }
                ]
            },
            "checks credits",
        ),
        ([], "Invalid account information response"),
    ],
)
def test_subscription_shapes_remain_informational(client, payload, expected):
    session, api = client
    dialog = OsmParserDialogMain(
        osm_parser=SimpleNamespace(session=session, api_client=api)
    )
    dialog.prompt_field.setPlainText("Keep this prompt")
    dialog.process_result(ApiResponse(200, payload), "limits")
    dialog.process_result(
        ApiResponse(200, {"current_subscription": None}), "subscription"
    )
    assert expected in dialog.prompts_label.text()
    assert dialog.go_button.isEnabled() and not dialog.prompt_field.isReadOnly()
    assert dialog.prompt_field.toPlainText() == "Keep this prompt"
    dialog.close()
    dialog.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert sip.isdeleted(dialog)


def test_sign_out_aborts_old_callbacks_and_keeps_account_journal(
    client, local_api, wait_for, tmp_path
):
    session, api = client
    session.set_access_token(token())
    journal = RecoveryJournal(tmp_path)
    identity = str(uuid4())
    journal.update_sections(
        identity, "account-a", analysis={"task_id": "synthetic-task"}
    )
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    reopened = []
    owner = SimpleNamespace(
        session=session,
        api_client=api,
        user_email="a@example.invalid",
        recovery_journal=journal,
        run=lambda: reopened.append(True),
    )
    dialog = OsmParserDialogMain(osm_parser=owner)
    dialog.set_account_email(owner.user_email)
    dialog.update_interface_with_restrictions_start()
    wait_for(lambda: len(local_api.requests) == 2)
    dialog.sign_out_button.click()
    assert reopened == [True]
    assert session.access_token is None and session.account_id is None
    assert owner.user_email is None and dialog.account_email_label.text() == "Signed in"
    assert (
        dialog.sync_controller.state is None and dialog.analysis_controller.state == {}
    )
    assert not dialog.result_controller.active
    wait_for(lambda: api.pending_count == 0)
    assert dialog.prompts_label.text() == ""
    assert before and before == {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    dialog.close()
    dialog.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert sip.isdeleted(dialog)


def test_empty_prompt_does_not_start_analysis(client, local_api):
    session, api = client
    dialog = OsmParserDialogMain(
        osm_parser=SimpleNamespace(session=session, api_client=api)
    )
    dialog.prompt_field.setPlainText("   ")
    dialog.go_button.click()
    assert "Enter an analysis request" in dialog.request_status_label.text()
    assert not local_api.requests
    dialog.close()
    dialog.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert sip.isdeleted(dialog)


@pytest.mark.parametrize("remaining", [2, -1])
def test_subscription_dates_accept_nonfractional_utc(client, remaining):
    from datetime import datetime, timedelta, timezone

    session, api = client
    dialog = OsmParserDialogMain(
        osm_parser=SimpleNamespace(session=session, api_client=api)
    )
    now = datetime.now(timezone.utc)
    subscription = {
        "subscription_plan_code": "individual",
        "start_date": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expiration_date": (now + timedelta(days=remaining)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }
    dialog.process_result(
        ApiResponse(200, {"current_subscription": subscription}), "subscription"
    )
    if remaining > 0:
        assert "days left" in dialog.trial_label.text()
        assert not dialog.trial_label.isHidden()
    else:
        assert dialog.trial_label.isHidden()
    assert not dialog.prompt_field.isReadOnly()
    dialog.close()
    dialog.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert sip.isdeleted(dialog)


def test_plugin_owned_signout_and_unload_complete_deferred_destruction(
    local_api, wait_for, qgis_app, monkeypatch
):
    import importlib.util
    import sys
    from pathlib import Path
    from qgis.PyQt.QtWidgets import QMainWindow
    import src

    root = Path(__file__).resolve().parents[1]
    monkeypatch.setattr(src, "API_LINK", local_api.origin)
    name = f"aino_upstream_lifecycle_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        name, root / "__init__.py", submodule_search_locations=[str(root)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    window = QMainWindow()
    iface = SimpleNamespace(
        mainWindow=lambda: window,
        addDockWidget=window.addDockWidget,
        removeDockWidget=window.removeDockWidget,
        addToolBarIcon=lambda action: None,
        addPluginToMenu=lambda *args: None,
        removePluginMenu=lambda *args: None,
        removeToolBarIcon=lambda action: None,
    )
    plugin = module.classFactory(iface)
    plugin.initGui()
    plugin._require_login()
    early_login = plugin._login_dialog
    assert early_login.parent() is window
    early_login.close()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert sip.isdeleted(early_login) and plugin._login_dialog is None
    plugin.session.set_access_token(token())
    plugin.user_email = "synthetic@example.invalid"
    plugin.run()
    dialog = plugin.main_dlg
    dock = plugin.dock
    wait_for(lambda: len(local_api.requests) == 2)
    controllers = (
        dialog.request_controller,
        dialog.result_controller,
        dialog.analysis_controller,
        dialog.sync_controller,
    )
    timer = dialog.analysis_controller.polling.scheduler.timer
    monkeypatch.setattr(
        dialog, "update_interface_with_restrictions_start", lambda: None
    )
    restored = []
    monkeypatch.setattr(
        dialog.request_controller, "restore", lambda: restored.append(True)
    )
    plugin.run()
    dock.hide()
    qgis_app.processEvents()
    plugin.run()
    qgis_app.processEvents()
    assert plugin.dock is dock and plugin.main_dlg is dialog
    assert controllers == (
        dialog.request_controller,
        dialog.result_controller,
        dialog.analysis_controller,
        dialog.sync_controller,
    )
    assert dialog.analysis_controller.polling.scheduler.timer is timer
    assert not restored and len(plugin.actions) == 1
    login = plugin._get_login_dialog()
    assert login.parent() is dock
    login.close()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    reopened = []
    monkeypatch.setattr(plugin, "run", lambda: reopened.append(True))
    dialog.sign_out()
    assert reopened == [True]
    plugin.session.set_access_token(token())
    capture = SimpleNamespace(cancelled=False)

    def cancel_capture():
        capture.cancelled = True

    capture.cancel = cancel_capture
    dialog.request_controller.capture = lambda *args: capture
    dialog.request_controller.start("parks", None, [])
    dock.hide()
    qgis_app.processEvents()
    assert not capture.cancelled
    assert not dialog.request_controller._closed
    assert not dialog.analysis_controller._closed
    assert not dialog.sync_controller._closed
    assert not dialog.result_controller.closed
    dock.show()
    qgis_app.processEvents()
    assert dialog.request_controller.busy and not capture.cancelled
    close_order = []
    for name, controller in (
        ("request", dialog.request_controller),
        ("result", dialog.result_controller),
        ("analysis", dialog.analysis_controller),
        ("sync", dialog.sync_controller),
    ):
        close = controller.close

        def traced_close(name=name, close=close):
            close_order.append(name)
            close()

        monkeypatch.setattr(controller, "close", traced_close)
    plugin.unload()
    assert close_order == ["request", "result", "analysis", "sync"]
    assert plugin.main_dlg is None and plugin.dock is None
    assert capture.cancelled
    assert dialog.request_controller._closed
    assert dialog.analysis_controller._closed
    assert dialog.sync_controller._closed
    assert dialog.result_controller.closed
    assert not dialog.analysis_controller.polling.scheduler.timer.isActive()
    assert plugin.api_client.pending_count == 0
    destroyed = []
    dialog.destroyed.connect(lambda: destroyed.append(True))
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qgis_app.processEvents()
    assert sip.isdeleted(dialog) and destroyed == [True]
    assert sip.isdeleted(dock)
    assert plugin.session.account_id is None
    from qgis.core import QgsProject

    QgsProject.instance().clear()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
