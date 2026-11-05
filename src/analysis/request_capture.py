import tempfile

from qgis.PyQt.QtCore import QObject

from src.project_sync.layers import SnapshotCapture
from src.project_sync.styles import RenderReference


class RequestCapture(QObject):
    """Own accepted capture independently of mutable form and preview state."""

    def __init__(
        self, project, canvas, directory, layer_ids, finished, failed, parent=None
    ):
        super().__init__(parent)
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(prefix="request-", dir=directory)
        self.connections = []
        self.done = False
        self.layer_ids = set(layer_ids)
        self.job = None
        self.failed_callback = failed
        try:
            self.job = SnapshotCapture(
                project,
                layer_ids,
                self.directory.name,
                reference=RenderReference(canvas.mapSettings()) if layer_ids else None,
                parent=self,
            )
            self.job.finished.connect(lambda outputs: self._finished(outputs, finished))
            self.job.failed.connect(self._failed)
            for layer_id in layer_ids:
                layer = project.mapLayer(layer_id)
                for name in (
                    "dataChanged",
                    "rendererChanged",
                    "styleChanged",
                    "nameChanged",
                    "crsChanged",
                ):
                    self._watch(getattr(layer, name))
            project.layersWillBeRemoved.connect(self._removed)
            self.removal_signal = project.layersWillBeRemoved
            self._watch(project.cleared)
            self.job.start()
        except Exception:
            self.cancel()
            raise

    def _watch(self, signal):
        signal.connect(self._invalidated)
        self.connections.append(signal)

    def _removed(self, ids):
        if self.layer_ids.intersection(ids):
            self._invalidated()

    def _disconnect(self):
        if hasattr(self, "removal_signal"):
            try:
                self.removal_signal.disconnect(self._removed)
            except (TypeError, RuntimeError):
                pass
        for signal in self.connections:
            try:
                signal.disconnect(self._invalidated)
            except (TypeError, RuntimeError):
                pass
        self.connections.clear()

    def _invalidated(self, *args):
        self._failed(
            "Selected source data or presentation changed during preparation. Retry to capture it again."
        )

    def _failed(self, message):
        if self.done:
            return
        self.cancel()
        self.failed_callback(message)

    def _finished(self, outputs, finished):
        if not self.done:
            self.done = True
            self._disconnect()
            finished(outputs)

    def cancel(self):
        """Release capture resources after adoption, interruption, or invalidation."""
        self.done = True
        self._disconnect()
        if self.job is not None:
            self.job.cancel()
            for stream in self.job.streams:
                stream.close(remove=True)
        self.directory.cleanup()
        self.deleteLater()
