import copy
from pathlib import Path
from uuid import uuid4

from qgis.PyQt.QtCore import QObject, pyqtSignal

from src.project_sync.recovery import project_identity

from .polling import TERMINAL


ACTIVE = {"capturing", "synchronizing", "starting", "analysis", "recapture"}


class RequestController(QObject):
    """Coordinate a frozen local request through the existing sync and analysis journals."""

    changed = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, project, session, journal, sync, analysis, capture, parent=None):
        super().__init__(parent)
        self.project, self.session, self.journal = project, session, journal
        self.sync, self.analysis, self.capture = sync, analysis, capture
        self.state = {}
        self.identity = self.account = None
        self.generation = 0
        self._job = None
        self._closed = False
        sync.ready.connect(self._ready)
        sync.failed.connect(self._sync_failed)
        analysis.changed.connect(self._analysis_changed)
        session.account_changed.connect(self._account_changed)

    @property
    def busy(self):
        return self.state.get("phase") in ACTIVE

    def _valid(self, fence=None):
        current = (
            self.generation,
            self.account,
            self.identity,
            self.state.get("intent_id"),
        )
        return (
            not self._closed
            and self.account == self.session.account_id
            and self.identity is not None
            and self.identity == project_identity(self.project, create=False)
            and (fence is None or fence == current)
        )

    def _scope(self, create=False):
        if self._closed or not self.session.account_id:
            raise ValueError("Sign in before starting or restoring a request")
        identity = project_identity(self.project, create=create)
        account = self.session.account_id
        if (identity, account) != (self.identity, self.account):
            self.pause()
            self.state = {}
            self.identity = self.account = None
            saved = self.journal.load(identity, account) if identity else {}
            self.identity, self.account = identity, account
            self.state = copy.deepcopy((saved or {}).get("request") or {})
        return identity is not None

    def _save(self):
        self.journal.update_sections(self.identity, self.account, request=self.state)
        self.changed.emit(copy.deepcopy(self.state))

    def start(self, prompt: str, area: dict | None, layer_ids: list[str]) -> None:
        """Accept one immutable intent; repeated GO while active has no effect."""
        self._scope(create=True)
        if self.busy:
            return
        if not prompt.strip():
            raise ValueError("Enter an analysis request")
        saved = self.journal.load(self.identity, self.account) or {}
        analysis = saved.get("analysis") or {}
        if analysis.get("action_intent") or (
            (analysis.get("start_intent") or analysis.get("task_id"))
            and (analysis.get("latest") or {}).get("status") not in TERMINAL
        ):
            raise ValueError("Resume or cancel the saved analysis first")
        sync = saved.get("sync") or {}
        if sync and sync.get("phase") not in {"ready", "failed"}:
            raise ValueError(
                "Retry the saved synchronization before starting another request"
            )
        self.generation += 1
        self.state = {
            "intent_id": str(uuid4()),
            "phase": "capturing",
            "prompt": prompt,
            "area": copy.deepcopy(area),
            "layer_ids": list(layer_ids),
            "project_title": (
                self.project.title()
                or Path(self.project.fileName()).stem
                or prompt.strip().splitlines()[0][:120]
            )[:500],
        }
        self._save()
        self._capture()

    def _capture(self):
        fence = (self.generation, self.account, self.identity, self.state["intent_id"])

        def failed(message):
            if self._valid(fence) and self.state.get("phase") == "capturing":
                self._release_capture()
                self.state.update(phase="recapture", error=str(message))
                self._save()
                self.failed.emit(str(message))

        def finished(layers):
            if not self._valid(fence) or self.state.get("phase") != "capturing":
                return
            self.state["phase"] = "synchronizing"
            self._save()
            try:
                self.sync.prepare(
                    self.project,
                    layers,
                    None,
                    context={
                        key: copy.deepcopy(self.state[key])
                        for key in ("prompt", "area")
                    }
                    | {"request_intent_id": self.state["intent_id"]},
                    project_title=self.state["project_title"],
                )
                if self._matching_sync(self.sync.state):
                    self.state["sync_operation_id"] = self.sync.state["operation_id"]
                    self._save()
            except (ValueError, OSError) as error:
                saved = (self.journal.load(self.identity, self.account) or {}).get(
                    "sync"
                )
                if not self._matching_sync(saved):
                    self.state["phase"] = "recapture"
                self.state["error"] = str(error)
                self._save()
                self.failed.emit(str(error))
            finally:
                self._release_capture()

        try:
            job = self.capture(
                copy.deepcopy(self.state["area"]),
                list(self.state["layer_ids"]),
                finished,
                failed,
            )
            if self.state.get("phase") == "capturing" and self._valid(fence):
                self._job = job
            elif job is not None:
                job.cancel()
        except (ValueError, OSError) as error:
            failed(str(error))

    def _release_capture(self):
        job, self._job = self._job, None
        if job is not None:
            job.cancel()

    def _sync_failed(self, message):
        if (
            self._valid()
            and self.state.get("phase") == "synchronizing"
            and self._matching_sync(self.sync.state)
        ):
            if self.sync.state.get("phase") == "failed":
                self.state["phase"] = "recapture"
            self.state["error"] = message
            self._save()
            self.failed.emit(message)

    def _matching_sync(self, sync):
        return (
            bool(sync)
            and (sync.get("context") or {}).get("request_intent_id")
            == self.state.get("intent_id")
            and (
                not self.state.get("sync_operation_id")
                or self.state["sync_operation_id"] == sync.get("operation_id")
            )
        )

    def _ready(self, snapshot):
        if (
            not self._valid()
            or self.state.get("phase") != "synchronizing"
            or not self._matching_sync(self.sync.state)
        ):
            return
        self.state.update(
            phase="starting",
            snapshot_id=snapshot["id"],
            project_id=snapshot["project_id"],
            sync_operation_id=self.sync.state["operation_id"],
        )
        self._save()
        self._start_analysis()

    def _matching_analysis(self, analysis):
        intent = (
            analysis.get("start_intent")
            or (analysis.get("last_rejection") or {}).get("request")
            or {}
        )
        return all(
            key in self.state and intent.get(key) == self.state[key]
            for key in ("project_id", "snapshot_id", "prompt", "area")
        )

    def _start_analysis(self):
        saved = (self.journal.load(self.identity, self.account) or {}).get(
            "analysis"
        ) or {}
        if self._matching_analysis(saved):
            self.analysis.restore()
        elif self.state.get("cancel_requested"):
            self.state["phase"] = "cancelled"
            self._save()
        else:
            self.analysis.start(
                self.state["project_id"],
                self.state["snapshot_id"],
                self.state["prompt"],
                self.state["area"],
            )

    def _analysis_changed(self, analysis):
        if (
            not self._valid()
            or self.state.get("phase") not in {"starting", "analysis"}
            or not self._matching_analysis(analysis)
        ):
            return
        latest = analysis.get("latest") or {}
        if latest.get("status") in TERMINAL:
            self.state["phase"] = latest["status"]
        elif (
            not analysis.get("start_intent")
            and (analysis.get("last_rejection") or {}).get("operation") == "start"
        ):
            self.state["phase"] = "rejected"
        else:
            self.state["phase"] = "analysis"
        self._save()
        if (
            self.state.get("cancel_requested")
            and analysis.get("task_id")
            and latest.get("status") not in TERMINAL
            and not analysis.get("action_intent")
            and not self.state.get("cancel_submitted")
        ):
            self.analysis.cancel()
            self.state["cancel_submitted"] = True
            self._save()

    def restore(self):
        """Reconcile durable stages without recapturing or generating another start identity."""
        if not self._scope():
            self.changed.emit({})
            return
        if self._job is not None:
            return
        phase = self.state.get("phase")
        saved = self.journal.load(self.identity, self.account) or {}
        if phase == "capturing" or (
            phase == "synchronizing" and not self._matching_sync(saved.get("sync"))
        ):
            self.state.update(
                phase="recapture",
                error="Preparation was interrupted. Retry to capture the selected layers again.",
            )
            self._save()
        elif phase == "synchronizing":
            self.sync.restore(self.project)
        elif phase in {"starting", "analysis"}:
            self._start_analysis()
        else:
            self.sync.restore(self.project)
            self.analysis.restore()
        self.changed.emit(copy.deepcopy(self.state))

    def retry(self):
        """Explicitly repeat incomplete capture; all remote retries reuse saved identities."""
        self._scope()
        if self.state.get("phase") == "recapture":
            self.generation += 1
            for key in ("error", "sync_operation_id", "snapshot_id", "project_id"):
                self.state.pop(key, None)
            self.state["phase"] = "capturing"
            self._save()
            self._capture()
        else:
            self.restore()

    def cancel(self):
        """Persist cancellation until an uncertain server start has been reconciled."""
        if not self._valid():
            return
        if self.state.get("phase") in {"starting", "analysis"}:
            self.state["cancel_requested"] = True
            self._save()
            self._start_analysis()
        elif self.busy:
            self.pause()
            self.state["phase"] = "cancelled"
            self._save()
        elif self.analysis.state.get("task_id"):
            self.analysis.cancel()

    def pause(self):
        """Invalidate local callbacks, preserving all durable mutation identities."""
        self.generation += 1
        self._release_capture()
        self.sync.pause()
        self.analysis.pause()

    def _account_changed(self, previous, current):
        self.pause()
        self.state = {}
        self.identity = self.account = None
        self.changed.emit({})

    def close(self):
        """Dispose local work without cancelling the server task."""
        self.pause()
        self._closed = True
        for signal, callback in (
            (self.sync.ready, self._ready),
            (self.sync.failed, self._sync_failed),
            (self.analysis.changed, self._analysis_changed),
            (self.session.account_changed, self._account_changed),
        ):
            try:
                signal.disconnect(callback)
            except (TypeError, RuntimeError):
                pass
