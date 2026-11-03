import copy
import hashlib
from pathlib import Path

from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal
from qgis.core import QgsProject

from src.project_sync.managed import watch_managed_output
from src.project_sync.recovery import RecoveryJournal, project_identity
from src.project_sync.styles import canonical_bytes

from .download import ResultDownloader
from .importer import ImportJob


class ResultController(QObject):
    status = pyqtSignal(str)
    completed = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, api, journal=None, project=None, parent=None):
        super().__init__(parent)
        self.api = api
        self.journal = journal or RecoveryJournal()
        self.project = project if project is not None else QgsProject.instance()
        self.downloader = ResultDownloader(api, self)
        self.downloader.progress.connect(self.status)
        self.generation = 0
        self.active = False
        self.job = None
        self.closed = False
        self.project.cleared.connect(self.pause)
        self.project.layersAdded.connect(self._watch_layers)
        self._watch_layers(list(self.project.mapLayers().values()))
        api.session.account_changed.connect(self._account_changed)

    def _watch_layers(self, layers):
        for layer in layers:
            watch_managed_output(layer)

    def start(self, manifest, project_id, reference):
        if self.closed:
            return
        identity = project_identity(self.project, create=False)
        account = self.api.session.account_id
        if (
            not identity
            or not account
            or type(project_id) is not int
            or project_id <= 0
        ):
            raise ValueError(
                "An authenticated linked project is required for output import"
            )
        fingerprint = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
        if self.active and (
            self.identity,
            self.account,
            self.fingerprint,
            self.project_id,
        ) == (
            identity,
            account,
            fingerprint,
            project_id,
        ):
            return
        self.pause()
        self.identity, self.account, self.fingerprint = identity, account, fingerprint
        self.project_id, self.reference = project_id, reference
        self.task_id = manifest["task_id"]
        self.manifest = copy.deepcopy(manifest)
        self.imports = copy.deepcopy(
            (self.journal.load(identity, account) or {}).get("imports") or {}
        )
        tasks = self.imports.setdefault("tasks", {})
        if (
            self.task_id in tasks
            and tasks[self.task_id]["manifest_hash"] != fingerprint
        ):
            raise ValueError(
                "Saved result manifest changed; refusing to replace imported output"
            )
        self.state = tasks.setdefault(
            self.task_id,
            dict(
                manifest_hash=fingerprint,
                project_id=project_id,
                outputs=[],
                layer_ids=[],
                status="pending",
            ),
        )
        if self.state.get("project_id") != project_id:
            raise ValueError("Saved result belongs to another Aino project")
        self.state["status"] = "pending"
        self._save()
        self.active = True
        generation = self.generation
        outputs = self.state.get("outputs", [])
        if len(outputs) == len(manifest["layers"]) and all(
            Path(part["path"]).is_file()
            for output in outputs
            for part in output["storage"]
        ):
            self._downloaded(outputs, "", generation)
        else:
            scoped = copy.deepcopy(manifest)
            for item in scoped["layers"]:
                item.update(
                    account_id=account,
                    client_project_id=identity,
                    project_id=project_id,
                    task_id=self.task_id,
                )
            directory = self.journal.scope(identity, account) / "results"
            self.downloader.download_results(
                self.task_id,
                scoped,
                str(directory),
                lambda outputs, error: self._downloaded(outputs, error, generation),
            )

    def _valid(self, generation):
        return (
            self.active
            and generation == self.generation
            and self.account == self.api.session.account_id
            and self.identity == project_identity(self.project, create=False)
        )

    def _save(self):
        self.journal.update_sections(self.identity, self.account, imports=self.imports)

    def _downloaded(self, outputs, error, generation):
        if not self._valid(generation):
            return
        if error:
            self._failure(error)
            return
        try:
            self.state["outputs"] = outputs
            self.state["status"] = "importing"
            self._save()
            self.index = 0
            self.layer_ids = []
            self._next(generation)
        except Exception as error:
            self._failure(str(error))

    def _next(self, generation):
        if not self._valid(generation):
            return
        try:
            if self.index == len(self.state["outputs"]):
                self.state.update(status="complete", layer_ids=self.layer_ids)
                self._save()
                self.active = False
                warnings = [
                    warning
                    for layer in self.manifest["layers"]
                    for warning in layer.get("style_warnings", [])
                ]
                warnings.extend(
                    warning
                    for output in self.state["outputs"]
                    for part in output["storage"]
                    for warning in part.get("warnings", [])
                )
                self.status.emit(
                    (
                        "Results imported. Save the QGIS project to keep these layers."
                        if self.layer_ids
                        else "Analysis completed — text result"
                    )
                    + ("\n" + "\n".join(warnings) if warnings else "")
                )
                self.completed.emit(self.layer_ids)
                return
            output = self.state["outputs"][self.index]
            self.status.emit(
                f"Importing output {self.index + 1} of {len(self.state['outputs'])}"
            )
            self.job = ImportJob(
                self.project,
                output["result"],
                output["storage"],
                self.reference,
                self,
                valid=lambda: self._valid(generation),
            )
            self.job.finished.connect(lambda ids: self._imported(ids, generation))
            self.job.failed.connect(self._failure)
            self.job.start()
        except Exception as error:
            self._failure(str(error))

    def _imported(self, ids, generation):
        if not self._valid(generation):
            return
        self.layer_ids.extend(ids)
        self.index += 1
        self.job.deleteLater()
        self.job = None
        QTimer.singleShot(0, lambda: self._next(generation))

    def _failure(self, message):
        self.pause()
        self.status.emit("Import interrupted. Retry saved results: " + message)
        self.failed.emit(message)

    def _account_changed(self, previous, current):
        self.pause()

    def pause(self, *args):
        self.generation += 1
        self.active = False
        self.downloader.abort()
        if self.job is not None:
            self.job.cancel()
            self.job.deleteLater()
            self.job = None

    def close(self):
        self.closed = True
        self.pause()
        self.downloader.close()
        try:
            self.project.cleared.disconnect(self.pause)
            self.project.layersAdded.disconnect(self._watch_layers)
            self.api.session.account_changed.disconnect(self._account_changed)
        except TypeError:
            pass
