import copy
from uuid import UUID, uuid4

from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.core import QgsProject

from src.project_sync.recovery import RecoveryJournal, project_identity

from .polling import PollingController, TERMINAL, valid_state


def _definite_rejection(response, operation, first_attempt):
    if response.error not in {"", "http_error"} or not isinstance(response.data, dict):
        return False
    signature = (
        response.status,
        response.data.get("result"),
        response.data.get("domain"),
        response.data.get("error_code"),
    )
    if first_attempt and signature == (
        400,
        "bad_request",
        "api_gateway",
        "qgis_request_validation",
    ):
        return True
    return operation == "action" and signature in {
        (403, "forbidden", "billing", "insufficient_credits"),
        (409, "conflict", "core", "qgis_action_conflict"),
    }


def _response_message(response):
    value = (
        response.data.get("error_message") if isinstance(response.data, dict) else None
    )
    return (
        value.split("\n\n", 1)[0]
        if isinstance(value, str) and value.strip()
        else response.message
    )


class AnalysisController(QObject):
    """Recover one explicit analysis intent and its exact unresolved action body."""

    changed = pyqtSignal(dict)
    status = pyqtSignal(str)
    results_ready = pyqtSignal(dict)

    def __init__(self, api, journal=None, project=None, parent=None, scheduler=None):
        super().__init__(parent)
        self.api = api
        self.journal = journal or RecoveryJournal()
        self.project = project if project is not None else QgsProject.instance()
        self.state = {}
        self.identity = None
        self.account = None
        self.generation = 0
        self._closed = False
        self._handles = []
        owned = self._handles
        self.destroyed.connect(lambda: [handle.abort() for handle in owned])
        self.polling = PollingController(api, self, scheduler)
        self.polling.updated.connect(self._updated)
        self.polling.unavailable.connect(
            lambda message: self.status.emit(
                "Connection interrupted; polling will retry. " + message
            )
        )
        api.session.account_changed.connect(self._account_changed)

    def bind(self, project):
        self.pause()
        self.project = project
        self.identity = None
        self.account = None
        self.state = {}
        self.changed.emit({})

    def _scope(self, create=True):
        if self._closed:
            raise ValueError("Analysis controller is closed")
        account = self.api.session.account_id
        if not account:
            raise ValueError("Sign in before starting or restoring analysis")
        identity = project_identity(self.project, create=create)
        if identity is None:
            return None
        if (identity, account) != (self.identity, self.account):
            self.pause()
            self.identity = self.account = None
            self.state = {}
            saved = copy.deepcopy(
                (self.journal.load(identity, account) or {}).get("analysis") or {}
            )
            self.identity, self.account = identity, account
            self.state = saved
        return identity, account

    def _save(self):
        self.journal.update_sections(self.identity, self.account, analysis=self.state)

    def start(self, project_id, snapshot_id, prompt, area=None):
        self._scope()
        if not prompt.strip():
            raise ValueError("Enter an analysis request")
        latest = self.state.get("latest") or {}
        if (self.state.get("start_intent") or self.state.get("task_id")) and latest.get(
            "status"
        ) not in TERMINAL:
            raise ValueError(
                "Resume or cancel the current analysis before starting another"
            )
        if self.state.get("action_intent"):
            raise ValueError(
                "Resolve the pending action before starting another analysis"
            )
        self.pause()
        self.state = {
            "start_dispatched": False,
            "start_intent": {
                "request_id": str(uuid4()),
                "project_id": project_id,
                "snapshot_id": snapshot_id,
                "prompt": prompt,
                "area": copy.deepcopy(area),
            },
        }
        self._save()
        self.changed.emit(copy.deepcopy(self.state))
        self._start()

    def restore(self, task_id=None):
        if self._scope(create=task_id is not None) is None:
            return
        if task_id is not None:
            task_id = str(UUID(task_id))
            if self.state.get("task_id") not in {None, task_id}:
                raise ValueError(
                    "This project has another analysis; restore its saved task"
                )
            self.state["task_id"] = task_id
            self._save()
        self.changed.emit(copy.deepcopy(self.state))
        if any(handle.pending for handle in self._handles):
            return
        if self.state.get("action_intent"):
            self._action()
        elif self.state.get("task_id"):
            if self.state.get("results") is not None:
                self.results_ready.emit(copy.deepcopy(self.state["results"]))
            latest = self.state.get("latest") or {}
            if latest.get("status") in TERMINAL:
                if latest.get("results_ready") and self.state.get("results") is None:
                    self._results()
            else:
                self.polling.watch(self.state["task_id"])
        elif self.state.get("start_intent"):
            self._start()

    def _send(self, method, path, body, callback):
        generation, account, identity = self.generation, self.account, self.identity
        self._handles[:] = [handle for handle in self._handles if handle.pending]

        def complete(response):
            if (
                generation != self.generation
                or account != self.api.session.account_id
                or identity != project_identity(self.project, create=False)
            ):
                return
            try:
                callback(response)
            except (OSError, ValueError, KeyError, TypeError) as error:
                self.pause()
                self.status.emit("Recovery requires attention: " + str(error))

        self._handles.append(self.api.send(method, path, body, complete))

    def _begin_dispatch(self, operation):
        first_attempt = self.state.get(operation + "_dispatched") is False
        self.state[operation + "_dispatched"] = True
        self._save()
        return first_attempt

    def _reject(self, operation, response):
        next_state = copy.deepcopy(self.state)
        request = next_state.pop(operation + "_intent")
        next_state.pop(operation + "_dispatched", None)
        next_state["last_rejection"] = {
            "operation": operation,
            "request": request,
            "status": response.status,
            "error_code": response.data["error_code"],
            "domain": response.data["domain"],
            "message": _response_message(response),
        }
        self.journal.update_sections(self.identity, self.account, analysis=next_state)
        self.state = next_state
        self.changed.emit(copy.deepcopy(self.state))
        self.status.emit("Submission rejected: " + _response_message(response))

    def _start(self):
        first_attempt = self._begin_dispatch("start")
        self.status.emit("Analysis: submitting saved request…")

        def complete(response):
            if _definite_rejection(response, "start", first_attempt):
                self._reject("start", response)
                return
            if not response.ok:
                self.status.emit(
                    "Start response unavailable. Resume to retry the same request. "
                    + response.message
                )
                return
            data = response.data
            task_id = str(UUID(data["task_id"]))
            if data["project_id"] != self.state["start_intent"]["project_id"]:
                raise ValueError("Unexpected project in start response")
            self.state.update(task_id=task_id, project_url=data["project_url"])
            self._save()
            self.changed.emit(copy.deepcopy(self.state))
            if not self.state.get("action_intent"):
                self.polling.watch(task_id)

        self._send(
            "POST", "/integrations/qgis/analyses", self.state["start_intent"], complete
        )

    def _updated(self, data):
        if (
            self.account != self.api.session.account_id
            or self.identity != project_identity(self.project, create=False)
        ):
            return
        latest = self.state.get("latest") or {}
        if data["revision"] < latest.get("revision", -1):
            return
        self.state["latest"] = copy.deepcopy(data)
        try:
            self._save()
        except (ValueError, OSError) as error:
            self.pause()
            self.status.emit("Cannot save analysis recovery: " + str(error))
            return
        self.changed.emit(copy.deepcopy(self.state))
        self.status.emit(
            "Analysis: "
            + data["status"].replace("_", " ")
            + (". " + str(data["error"]) if data.get("error") else "")
        )
        if data.get("results_ready") and self.state.get("results") is None:
            self._results()

    def submit_action(self, action):
        self._scope()
        if self.state.get("action_intent"):
            raise ValueError(
                "Resume the pending submission before sending another action"
            )
        kind = action.get("kind")
        pending = (self.state.get("latest") or {}).get("pending_action")
        if kind != "cancel" and (
            not pending
            or action.get("action_id") != pending["action_id"]
            or kind != pending["kind"]
        ):
            raise ValueError("This action is no longer current; refresh the analysis")
        if not self.state.get("task_id"):
            raise ValueError("Restore the start response before sending an action")
        intent = copy.deepcopy(action)
        intent["request_id"] = str(uuid4())
        intent.setdefault("payload", {})
        self.state["action_intent"] = intent
        self.state["action_dispatched"] = False
        self.state.pop("last_rejection", None)
        self._save()
        self.changed.emit(copy.deepcopy(self.state))
        self._action()

    def _action(self):
        first_attempt = self._begin_dispatch("action")
        self.polling.stop()
        task = self.state["task_id"]

        def complete(response):
            if _definite_rejection(response, "action", first_attempt):
                self._reject("action", response)
                self.polling.watch(task)
            elif response.ok and valid_state(response.data, task):
                self.state.pop("action_intent", None)
                self.state.pop("action_dispatched", None)
                self._save()
                self._updated(response.data)
                if (self.state.get("latest") or {}).get("status") not in TERMINAL:
                    self.polling.watch(task)
            else:
                self.status.emit(
                    "Action response unavailable. Resume to retry the saved answer. "
                    + response.message
                )

        self._send(
            "POST",
            f"/integrations/qgis/analyses/{task}/actions",
            self.state["action_intent"],
            complete,
        )

    def cancel(self):
        self.submit_action({"kind": "cancel", "payload": {}})

    def _results(self):
        if any(handle.pending for handle in self._handles):
            return
        task = self.state["task_id"]

        def complete(response):
            if not response.ok:
                self.status.emit(
                    "Results unavailable. Resume to fetch them again. "
                    + response.message
                )
                return
            data = response.data
            if (
                not isinstance(data, dict)
                or data.get("task_id") != task
                or not isinstance(data.get("layers"), list)
            ):
                raise ValueError("Invalid result manifest")
            self.state["results"] = copy.deepcopy(data)
            self._save()
            self.changed.emit(copy.deepcopy(self.state))
            self.results_ready.emit(copy.deepcopy(data))

        self._send("GET", f"/integrations/qgis/analyses/{task}/results", None, complete)

    def pause(self):
        self.generation += 1
        self.polling.stop()
        for handle in self._handles:
            handle.abort()
        self._handles.clear()

    def close(self):
        self.pause()
        self._closed = True
        try:
            self.api.session.account_changed.disconnect(self._account_changed)
        except (TypeError, RuntimeError):
            pass

    def _account_changed(self, previous, current):
        self.pause()
        self.state = {}
        self.account = self.identity = None
        self.changed.emit({})
