from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal

TERMINAL = {"completed", "failed", "cancelled"}
STATUSES = TERMINAL | {
    "queued",
    "planning",
    "running",
    "awaiting_input",
    "awaiting_confirmation",
}


class Scheduler(QObject):
    def __init__(self, parent):
        super().__init__(parent)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.callback = None
        self.timer.timeout.connect(self._fire)

    def schedule(self, seconds, callback):
        self.callback = callback
        self.timer.start(seconds * 1000)

    def stop(self):
        self.timer.stop()
        self.callback = None

    def _fire(self):
        callback, self.callback = self.callback, None
        if callback is not None:
            callback()


def valid_state(data, task_id):
    return (
        isinstance(data, dict)
        and data.get("task_id") == task_id
        and data.get("status") in STATUSES
        and type(data.get("revision")) is int
        and data["revision"] >= 0
        and isinstance(data.get("steps"), list)
    )


class PollingController(QObject):
    """One GET at a time; delays begin after delivery, including auth recovery."""

    updated = pyqtSignal(dict)
    unavailable = pyqtSignal(str)

    def __init__(self, api, parent=None, scheduler=None):
        super().__init__(parent)
        self.api = api
        self.scheduler = scheduler or Scheduler(self)
        self.task_id = None
        self.handle = None
        self.generation = 0
        self.failures = 0
        self.revision = -1
        owned = []
        self._owned = owned
        if scheduler is not None:
            self.destroyed.connect(scheduler.stop)

    def watch(self, task_id):
        if self.task_id == task_id:
            return
        self.stop()
        self.task_id = task_id
        self.revision = -1
        self.failures = 0
        self._poll()

    def stop(self):
        self.generation += 1
        self.scheduler.stop()
        if self.handle is not None:
            self.destroyed.disconnect(self.handle.abort)
            self.handle.abort()
        self.handle = None
        self._owned.clear()
        self.task_id = None

    def _poll(self):
        if self.task_id is None or (self.handle is not None and self.handle.pending):
            return
        generation = self.generation
        task_id = self.task_id
        self._owned[:] = [handle for handle in self._owned if handle.pending]

        def complete(response):
            if generation != self.generation:
                return
            self.destroyed.disconnect(self.handle.abort)
            self.handle = None
            self._owned.clear()
            if response.ok and valid_state(response.data, task_id):
                self.failures = 0
                data = response.data
                if data["revision"] >= self.revision:
                    self.revision = data["revision"]
                    self.updated.emit(data)
                    if generation != self.generation:
                        return
                    if data["status"] in TERMINAL:
                        self.stop()
                        return
                self.scheduler.schedule(4, self._poll)
            else:
                self.unavailable.emit(
                    response.message if not response.ok else "Invalid analysis response"
                )
                delay = min(4 * 2 ** min(self.failures, 4), 60)
                self.failures += 1
                self.scheduler.schedule(delay, self._poll)

        self.handle = self.api.send(
            "GET", f"/integrations/qgis/analyses/{task_id}", None, complete
        )
        self._owned.append(self.handle)
        self.destroyed.connect(self.handle.abort)
