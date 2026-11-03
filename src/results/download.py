import hashlib
import os
import tempfile
from pathlib import Path
from urllib.parse import urlencode

from qgis.PyQt.QtCore import QObject, QThread, QTimer, QUrl, pyqtSignal
from qgis.PyQt.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from .storage import build_storage, read_document


class _StorageWorker(QThread):
    completed = pyqtSignal(object, str)

    def __init__(self, temporary, target, result):
        super().__init__()
        self.temporary, self.target, self.result = temporary, target, result

    def run(self):
        try:
            read_document(self.temporary, self.result)
            if self.isInterruptionRequested():
                return
            os.replace(self.temporary, self.target)
            directory_fd = os.open(Path(self.target).parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            storage = build_storage(
                self.target, self.result, self.isInterruptionRequested
            )
            if not self.isInterruptionRequested():
                self.completed.emit(dict(local_path=self.target, storage=storage), "")
        except Exception as error:
            if not self.isInterruptionRequested():
                self.completed.emit(None, str(error))
        finally:
            Path(self.temporary).unlink(missing_ok=True)


class ResultDownloader(QObject):
    progress = pyqtSignal(str)

    def __init__(self, api, parent=None):
        super().__init__(parent)
        self.api = api
        self.manager = QNetworkAccessManager(self)
        self.reply = None
        self.handle = None
        self.file = None
        self.temporary = None
        self.worker = None
        self.callback = None
        self.generation = 0
        self.active = False
        api.session.account_changed.connect(self._account_changed)
        self._workers = set()
        workers = self._workers
        self.destroyed.connect(
            lambda: [worker.requestInterruption() for worker in workers]
        )

    def download_results(self, task_id: str, manifest: dict, target_dir: str, callback):
        self.abort()
        self.active = True
        self.account = self.api.session.account_id
        self.callback = callback
        self.layers = list(manifest["layers"])
        self.index = 0
        self.outputs = []
        self.directory = Path(target_dir) / hashlib.sha256(task_id.encode()).hexdigest()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._next()
        return self

    def _next(self):
        if self.index == len(self.layers):
            self._finish(self.outputs, "")
            return
        self.result = self.layers[self.index]
        self.refreshes = 0
        self._export()

    def _export(self):
        self.progress.emit(f"Downloading output {self.index + 1} of {len(self.layers)}")
        generation = self.generation
        path = "/project/dataset/export?" + urlencode(
            {
                "dataset_id": self.result["dataset_id"],
                "expected_version": self.result["version"],
            }
        )
        self.handle = self.api.send(
            "GET", path, None, lambda response: self._signed(response, generation)
        )

    def _signed(self, response, generation):
        if generation != self.generation or not self.active:
            return
        self.handle = None
        if (
            not response.ok
            or not isinstance(response.data, dict)
            or not isinstance(response.data.get("url"), str)
        ):
            self._finish(None, response.message or "Export did not return a URL")
            return
        url = QUrl(response.data["url"])
        if (
            not url.isValid()
            or url.userInfo()
            or url.hasFragment()
            or not url.host()
            or (
                url.scheme() != "https"
                and not (
                    url.scheme() == "http"
                    and url.host() in {"127.0.0.1", "localhost", "::1"}
                )
            )
        ):
            self._finish(None, "Unsafe export URL")
            return
        try:
            descriptor, self.temporary = tempfile.mkstemp(
                prefix=".download-", dir=self.directory
            )
            self.file = os.fdopen(descriptor, "wb")
        except OSError as error:
            self._finish(None, str(error))
            return
        self.received = 0
        request = QNetworkRequest(url)
        request.setTransferTimeout(30000)
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.ManualRedirectPolicy,
        )
        request.setRawHeader(b"Accept-Encoding", b"identity")
        self.reply = self.manager.get(request)
        self.reply.setReadBufferSize(1024 * 1024)
        self.reply.readyRead.connect(lambda: self._read(generation))
        self.reply.finished.connect(lambda: self._downloaded(generation))

    def _read(self, generation):
        if generation != self.generation or self.reply is None or self.file is None:
            return
        try:
            chunk = bytes(self.reply.readAll())
            self.file.write(chunk)
            self.received += len(chunk)
        except OSError as error:
            self._finish(None, str(error))

    def _discard(self):
        if self.file is not None:
            self.file.close()
            self.file = None
        if self.temporary:
            Path(self.temporary).unlink(missing_ok=True)
            self.temporary = None

    def _downloaded(self, generation):
        if generation != self.generation or self.reply is None:
            return
        self._read(generation)
        if self.reply is None:
            return
        reply, self.reply = self.reply, None
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) or 0
        length = bytes(reply.rawHeader(b"Content-Length"))
        error = reply.error() != QNetworkReply.NetworkError.NoError
        reply.deleteLater()
        if status in {401, 403, 404, 410} and self.refreshes == 0:
            self._discard()
            self.refreshes += 1
            self._export()
            return
        if (
            status != 200
            or error
            or not self.received
            or (length and (not length.isdigit() or int(length) != self.received))
        ):
            self._finish(
                None, f"Output download incomplete (HTTP {status}); retry saved results"
            )
            return
        try:
            self.file.flush()
            os.fsync(self.file.fileno())
            self.file.close()
            self.file = None
            key = f"{self.result['dataset_id']}-{self.result['version']}-{self.result['content_hash']}"
            target = str(
                self.directory / (hashlib.sha256(key.encode()).hexdigest() + ".geojson")
            )
            worker = _StorageWorker(self.temporary, target, self.result)
            self.temporary = None
            self.worker = worker
            self._workers.add(worker)
            worker.generation = generation
            worker.completed.connect(self._worker_completed)
            workers = self._workers
            worker.finished.connect(lambda: workers.discard(worker))
            worker.finished.connect(worker.deleteLater)
            worker.start()
        except Exception as error:
            self._finish(None, str(error))

    def _worker_completed(self, output, message):
        self._prepared(output, message, self.sender().generation)

    def _prepared(self, output, message, generation):
        if (
            generation != self.generation
            or not self.active
            or self.account != self.api.session.account_id
        ):
            return
        self.worker = None
        if message:
            self._finish(None, message)
            return
        self.outputs.append(dict(result=self.result, **output))
        self.index += 1
        QTimer.singleShot(0, self._advance)

    def _advance(self):
        if self.active:
            self._next()

    def _finish(self, outputs, message):
        callback = self.callback
        self.callback = None
        self.abort()
        if callback:
            callback(outputs, message)

    def _account_changed(self, previous, current):
        self.abort()

    def abort(self):
        self.generation += 1
        self.active = False
        self.callback = None
        if self.handle:
            self.handle.abort()
            self.handle = None
        reply, self.reply = self.reply, None
        if reply:
            reply.abort()
            reply.deleteLater()
        if self.worker:
            self.worker.requestInterruption()
            self.worker = None
        self._discard()

    def close(self):
        self.abort()
        try:
            self.api.session.account_changed.disconnect(self._account_changed)
        except TypeError:
            pass


def download_results(
    task_id: str, manifest: dict, target_dir: str, callback, *, api, parent=None
):
    """Download pinned output versions without sending API credentials to signed URLs."""
    downloader = ResultDownloader(api, parent)
    downloader.download_results(task_id, manifest, target_dir, callback)
    return downloader
