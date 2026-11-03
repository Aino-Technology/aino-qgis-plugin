import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QEventLoop, QSettings, QTimer
from qgis.PyQt.QtNetwork import QHostAddress, QTcpServer
from qgis.core import QgsApplication, QgsProject

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def qgis_app():
    with tempfile.TemporaryDirectory(prefix="aino-qgis-profile-") as directory:
        os.environ["XDG_CONFIG_HOME"] = directory
        os.environ["QGIS_CUSTOM_CONFIG_PATH"] = directory
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(
            QSettings.Format.IniFormat, QSettings.Scope.UserScope, directory
        )
        app = QgsApplication([], True, directory)
        app.initQgis()
        yield app
        QgsProject.instance().clear()
        app.processEvents()
        app.exitQgis()


@pytest.fixture
def wait_for(qgis_app):
    def wait(predicate, timeout=3000):
        if predicate():
            return
        loop = QEventLoop()
        poll = QTimer()
        poll.setInterval(5)
        poll.timeout.connect(lambda: loop.quit() if predicate() else None)
        deadline = QTimer()
        deadline.setSingleShot(True)
        deadline.timeout.connect(loop.quit)
        poll.start()
        deadline.start(timeout)
        loop.exec()
        poll.stop()
        deadline.stop()
        assert predicate(), "Qt event-loop condition timed out"

    return wait


class LocalApi:
    def __init__(self):
        self.server = QTcpServer()
        assert self.server.listen(QHostAddress.SpecialAddress.LocalHost, 0)
        self.origin = f"http://127.0.0.1:{self.server.serverPort()}"
        self.requests = []
        self.sockets = []
        self.server.newConnection.connect(self.accept)

    def accept(self):
        while self.server.hasPendingConnections():
            connection = self.server.nextPendingConnection()
            self.sockets.append(connection)
            buffer = bytearray()
            connection.readyRead.connect(lambda c=connection, b=buffer: self.read(c, b))

    def read(self, connection, buffer):
        buffer.extend(bytes(connection.readAll()))
        if b"\r\n\r\n" not in buffer:
            return
        header, body = bytes(buffer).split(b"\r\n\r\n", 1)
        lines = header.decode().split("\r\n")
        headers = dict(line.split(": ", 1) for line in lines[1:])
        if len(body) < int(headers.get("Content-Length", "0")):
            return
        method, path, _ = lines[0].split(" ")
        self.requests.append(
            {
                "method": method,
                "path": path,
                "headers": headers,
                "body": body,
                "socket": connection,
            }
        )
        connection.readyRead.disconnect()

    def respond(self, index, status=200, data=None, headers=None, raw=None):
        connection = self.requests[index]["socket"]
        body = raw if raw is not None else json.dumps(data).encode()
        values = {
            "Content-Type": "application/json",
            "Connection": "close",
            "Content-Length": str(len(body)),
            **(headers or {}),
        }
        head = f"HTTP/1.1 {status} Response\r\n" + "".join(
            f"{k}: {v}\r\n" for k, v in values.items()
        )
        connection.write(head.encode() + b"\r\n" + body)
        connection.disconnectFromHost()

    def close(self):
        for connection in self.sockets:
            connection.abort()
            connection.deleteLater()
        self.server.close()
        self.server.deleteLater()


@pytest.fixture
def local_api(qgis_app):
    api = LocalApi()
    yield api
    api.close()


@pytest.fixture
def client(local_api, qgis_app):
    assert importlib.util.find_spec("src.session"), (
        "Task7 requires the memory-only Session"
    )
    assert importlib.util.find_spec("src.api_client"), (
        "Task7 requires the asynchronous ApiClient"
    )
    from src.api_client import ApiClient
    from src.session import Session

    session = Session()
    api = ApiClient(session, local_api.origin)
    yield session, api
    if not sip.isdeleted(api):
        api.close()
        api.deleteLater()
    session.deleteLater()
    qgis_app.processEvents()
