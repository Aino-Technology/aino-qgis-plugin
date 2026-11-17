"""Usage events for Aino's product analytics (Mixpanel), so a sign-in from QGIS is counted next to
the web app's. Sending is best effort: it never blocks the plugin, and a failure is dropped."""

import base64
import configparser
import json
import os
import platform
import time
import uuid

from qgis.PyQt.QtCore import QByteArray, QUrl
from qgis.PyQt.QtNetwork import QNetworkAccessManager, QNetworkRequest

from src import MIXPANEL_TOKEN

MIXPANEL_TRACK_URL = "https://api.mixpanel.com/track"

_manager = None


def plugin_version():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "metadata.txt")
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
        return parser.get("general", "version", fallback="")
    except (OSError, configparser.Error):
        return ""


def qgis_version():
    try:
        from qgis.core import Qgis

        return Qgis.QGIS_VERSION
    except (ImportError, AttributeError):
        return ""


def track(event, account_id, properties=None):
    """Send one event for the signed-in account; the web app identifies people by the same id."""
    if not MIXPANEL_TOKEN or not account_id:
        return
    global _manager
    if _manager is None:
        _manager = QNetworkAccessManager()
    payload = {
        "event": event,
        "properties": {
            "token": MIXPANEL_TOKEN,
            "distinct_id": account_id,
            "time": int(time.time() * 1000),
            "$insert_id": uuid.uuid4().hex,
            "source": "qgis",
            "plugin_version": plugin_version(),
            "qgis_version": qgis_version(),
            "$os": platform.system(),
            **(properties or {}),
        },
    }
    body = "data=" + base64.b64encode(json.dumps([payload]).encode("utf-8")).decode("ascii")
    request = QNetworkRequest(QUrl(MIXPANEL_TRACK_URL))
    request.setHeader(
        QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/x-www-form-urlencoded"
    )
    reply = _manager.post(request, QByteArray(body.encode("ascii")))
    reply.finished.connect(reply.deleteLater)
