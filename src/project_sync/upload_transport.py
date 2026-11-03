import base64
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from qgis.PyQt.QtCore import (
    QObject,
    QIODevice,
    QRunnable,
    QThreadPool,
    QUrl,
    pyqtSignal,
)
from qgis.PyQt.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest


class _RequestBody(QIODevice):
    def __init__(self, data, parent):
        super().__init__(parent)
        self.data = data
        self.position = 0
        self.open(QIODevice.OpenModeFlag.ReadOnly)

    def isSequential(self):
        return True

    def bytesAvailable(self):
        return len(self.data) - self.position + super().bytesAvailable()

    def readData(self, maximum):
        result = self.data[self.position : self.position + maximum]
        self.position += len(result)
        return result

    def reset(self):
        return False


class _FileSignals(QObject):
    finished = pyqtSignal(object, str)


class _FileJob(QRunnable):
    def __init__(self, work):
        super().__init__()
        self.work = work
        self.signals = _FileSignals()

    def run(self):
        try:
            self.signals.finished.emit(self.work(), "")
        except Exception as error:
            self.signals.finished.emit(None, str(error))


def _origin(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or any(ord(char) < 33 for char in url)
        or "\\" in url
    ):
        raise ValueError("Unsafe storage URL")
    if parsed.scheme != "https" and parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError("Storage requires HTTPS")
    return (
        parsed.scheme,
        parsed.hostname,
        parsed.port or (443 if parsed.scheme == "https" else 80),
    )


class UploadTransport(QObject):
    """Scoped TUS transport; all credentials and session Locations remain memory-only."""

    def __init__(self, parent=None, *, chunk_size=5 * 1024 * 1024):
        super().__init__(parent)
        self.manager = QNetworkAccessManager(self)
        self.chunk_size = chunk_size
        self._generation = 0
        self._reply = None
        self._jobs = set()
        self._callback = None
        self._token = None
        self._location = None

    def start(self, path, content_hash, issued, callback, *, before_request=None):
        """Upload only to the trusted API-issued endpoint and same-origin session URL."""
        self.abort()
        self._callback = callback
        self._before_request = before_request
        try:
            if (
                issued.get("protocol") != "tus"
                or not issued.get("upload_token")
                or issued.get("bucket") != "heatmap-dataset"
            ):
                raise ValueError("Invalid scoped upload authorization")
            self._endpoint = issued["upload_url"]
            self._approved_origin = _origin(self._endpoint)
            if urlsplit(self._endpoint).query:
                raise ValueError("Storage endpoint must not contain query credentials")
            self._token = issued["upload_token"]
            self._expiry = datetime.fromisoformat(
                issued["expires_at"].replace("Z", "+00:00")
            )
            if self._expiry.tzinfo is None:
                raise ValueError("Upload expiration must include a timezone")
            self._path = Path(path)
            self._offset = 0
            self._sent_end = 0
            self._recoveries = 0
            metadata = {
                "bucketName": issued["bucket"],
                "objectName": issued["file_path"],
                "contentType": "application/geo+json",
            }
            self._metadata = ",".join(
                f"{key} {base64.b64encode(value.encode()).decode()}"
                for key, value in metadata.items()
            )
            path = self._path
            maximum = issued["max_size_bytes"]

            def verify():
                if path.is_symlink():
                    raise ValueError("Staged file is a symbolic link")
                before = path.stat()
                if before.st_size > maximum:
                    raise ValueError("Captured input exceeds the upload limit")
                digest = hashlib.sha256()
                with path.open("rb") as file:
                    while block := file.read(1024 * 1024):
                        digest.update(block)
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (
                    after.st_size,
                    after.st_mtime_ns,
                ) or digest.hexdigest() != content_hash:
                    raise ValueError("Staged input changed after capture")
                return after.st_size, after.st_mtime_ns

            self._file_job(verify, self._verified)
        except (KeyError, ValueError, TypeError, OSError) as error:
            self._finish(str(error))

    def abort(self):
        """Abort local transfer without persisting credentials or deleting server objects."""
        self._generation += 1
        self._callback = None
        self._token = None
        self._location = None
        reply, self._reply = self._reply, None
        if reply is not None:
            reply.abort()
            reply.deleteLater()

    def _file_job(self, work, callback):
        generation = self._generation
        job = _FileJob(work)
        self._jobs.add(job)

        def completed(result, error):
            self._jobs.discard(job)
            if generation != self._generation or self._callback is None:
                return
            if error:
                self._finish(error)
            else:
                callback(result)

        job.signals.finished.connect(completed)
        QThreadPool.globalInstance().start(job)

    def _verified(self, result):
        self._size, self._mtime = result
        self._request(
            "POST",
            self._endpoint,
            {"Upload-Length": str(self._size), "Upload-Metadata": self._metadata},
        )

    def _request(self, method, url, headers=None, body=b""):
        try:
            if datetime.now(timezone.utc) >= self._expiry:
                raise ValueError(
                    "Upload authorization expired; a new upload path is required"
                )
            if _origin(url) != self._approved_origin:
                raise ValueError("Cross-origin storage Location refused")
            if self._before_request is not None:
                self._before_request(method, self._offset, len(body))
            request = QNetworkRequest(QUrl(url))
            request.setAttribute(
                QNetworkRequest.Attribute.RedirectPolicyAttribute,
                QNetworkRequest.RedirectPolicy.ManualRedirectPolicy,
            )
            request.setTransferTimeout(30000)
            for key, value in {
                "Tus-Resumable": "1.0.0",
                "Authorization": f"Bearer {self._token}",
                **(headers or {}),
            }.items():
                request.setRawHeader(key.encode(), value.encode())
            request.setAttribute(
                QNetworkRequest.Attribute.DoNotBufferUploadDataAttribute, True
            )
            request.setHeader(
                QNetworkRequest.KnownHeaders.ContentLengthHeader, len(body)
            )
            device = _RequestBody(body, self)
            reply = self.manager.sendCustomRequest(request, method.encode(), device)
            reply.finished.connect(device.deleteLater)
            self._reply = reply
            generation = self._generation
            reply.finished.connect(lambda: self._finished(reply, method, generation))
        except (ValueError, TypeError, OSError) as error:
            self._finish(str(error))

    def _finished(self, reply, method, generation):
        if generation != self._generation or reply is not self._reply:
            return
        self._reply = None
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) or 0
        failed = reply.error() != QNetworkReply.NetworkError.NoError
        location = bytes(reply.rawHeader(b"Location")).decode("utf-8", "replace")
        offset = bytes(reply.rawHeader(b"Upload-Offset")).decode("ascii", "replace")
        length = bytes(reply.rawHeader(b"Upload-Length")).decode("ascii", "replace")
        reply.deleteLater()
        if 300 <= status < 400:
            self._finish("Storage redirect refused")
            return
        if method in {"PATCH", "HEAD"} and (
            status == 409
            or status >= 500
            or not status
            or (200 <= status < 300 and failed)
        ):
            self._recoveries += 1
            if self._recoveries <= 3:
                self._request("HEAD", self._location)
            else:
                self._finish("Cannot reconcile storage offset")
            return
        if failed or not 200 <= status < 300:
            self._finish(
                f"Storage {method} failed (HTTP {status}); a new upload path is required"
            )
            return
        if method == "POST":
            try:
                if not location:
                    raise ValueError("Storage did not return a session Location")
                candidate = urljoin(self._endpoint, location)
                if _origin(candidate) != self._approved_origin:
                    raise ValueError("Cross-origin storage Location refused")
                self._location = candidate
            except ValueError as error:
                self._finish(str(error))
                return
        else:
            if (
                not offset.isascii()
                or not offset.isdecimal()
                or not self._offset <= int(offset) <= self._sent_end
                or (method == "PATCH" and int(offset) != self._sent_end)
                or (length and (not length.isdecimal() or int(length) != self._size))
            ):
                self._finish("Invalid server upload offset or length")
                return
            acknowledged = int(offset)
            if acknowledged > self._offset:
                self._recoveries = 0
            self._offset = acknowledged
        if self._offset == self._size:
            self._finish("")
        else:
            self._next_chunk()

    def _next_chunk(self):
        path, offset, size, mtime = self._path, self._offset, self._size, self._mtime
        chunk_size = self.chunk_size

        def read():
            stat = path.stat()
            if path.is_symlink() or (stat.st_size, stat.st_mtime_ns) != (size, mtime):
                raise ValueError("Staged input changed during upload")
            with path.open("rb") as file:
                file.seek(offset)
                data = file.read(min(chunk_size, size - offset))
            if len(data) != min(chunk_size, size - offset):
                raise ValueError("Staged input became truncated")
            return data

        self._file_job(read, self._patch)

    def _patch(self, data):
        self._sent_end = self._offset + len(data)
        self._request(
            "PATCH",
            self._location,
            {
                "Content-Type": "application/offset+octet-stream",
                "Upload-Offset": str(self._offset),
            },
            data,
        )

    def _finish(self, error):
        callback = self._callback
        self.abort()
        if callback:
            callback(error)
