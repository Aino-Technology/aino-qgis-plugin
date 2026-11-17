import base64
import json
from urllib.parse import parse_qs

import src.analytics as analytics


def _event(request):
    data = parse_qs(request["body"].decode())["data"][0]
    return json.loads(base64.b64decode(data))[0]


def test_sends_nothing_without_a_token(local_api, monkeypatch, qgis_app):
    monkeypatch.setattr(analytics, "MIXPANEL_TOKEN", "")
    monkeypatch.setattr(analytics, "MIXPANEL_TRACK_URL", local_api.origin + "/track")
    analytics.track("Authorized", "account-1")
    for _ in range(20):
        qgis_app.processEvents()
    assert local_api.requests == []


def test_sends_nothing_without_an_account(local_api, monkeypatch, qgis_app):
    monkeypatch.setattr(analytics, "MIXPANEL_TOKEN", "token")
    monkeypatch.setattr(analytics, "MIXPANEL_TRACK_URL", local_api.origin + "/track")
    analytics.track("Authorized", None)
    for _ in range(20):
        qgis_app.processEvents()
    assert local_api.requests == []


def test_login_event_names_the_account_and_qgis(local_api, monkeypatch, wait_for):
    monkeypatch.setattr(analytics, "MIXPANEL_TOKEN", "token")
    monkeypatch.setattr(analytics, "MIXPANEL_TRACK_URL", local_api.origin + "/track")
    analytics.track("Authorized", "account-1", {"method": "password"})
    wait_for(lambda: len(local_api.requests) == 1)

    request = local_api.requests[0]
    assert request["method"] == "POST"
    assert request["path"] == "/track"
    event = _event(request)
    assert event["event"] == "Authorized"
    properties = event["properties"]
    assert properties["distinct_id"] == "account-1"
    assert properties["token"] == "token"
    assert properties["source"] == "qgis"
    assert properties["method"] == "password"
    assert properties["plugin_version"]
    local_api.respond(0, data=1)
