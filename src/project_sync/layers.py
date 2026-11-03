import hashlib
import json
import math
import os
import tempfile
import time
from pathlib import Path

from qgis.PyQt.QtCore import QObject, QDate, QDateTime, QTime, Qt, QTimer, pyqtSignal
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsJsonExporter,
    QgsVariantUtils,
    QgsVectorLayer,
    QgsVectorLayerFeatureSource,
)

from .area import require_gui_thread
from .managed import MANAGED_KEY, ManagedCapture
from .styles import RendererCapture, canonical_bytes


FIELD_TYPES = {
    1: "boolean",
    2: "integer",
    3: "integer",
    4: "integer",
    5: "integer",
    6: "number",
    8: "json",
    9: "json",
    10: "string",
    11: "json",
    14: "date",
    15: "time",
    16: "datetime",
}


def _field_schema(fields):
    schema = []
    for field in fields:
        type_id = int(field.type())
        kind = (
            "json" if field.typeName().lower() == "json" else FIELD_TYPES.get(type_id)
        )
        if kind is None:
            raise ValueError(
                f"Unsupported field type for {field.name()}: {field.typeName()}"
            )
        schema.append(
            {
                "name": field.name(),
                "type": kind,
                "original_type": canonical_bytes(
                    {
                        "qgis_type": type_id,
                        "qgis_subtype": int(field.subType()),
                        "type_name": field.typeName(),
                        "length": field.length(),
                        "precision": field.precision(),
                    }
                ).decode(),
                "serialization": "iso8601-ms"
                if kind in {"date", "datetime", "time"}
                else "json-native",
                "classification": False,
            }
        )
    return schema


def _json_value(value):
    if QgsVariantUtils.isNull(value):
        return None
    if isinstance(value, (QDateTime, QTime)):
        return value.toString(Qt.DateFormat.ISODateWithMs)
    if isinstance(value, QDate):
        return value.toString(Qt.DateFormat.ISODate)
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON map keys must be strings")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError(f"Unsupported field value: {type(value).__name__}")


class _LayerStream:
    def __init__(self, project, layer, order, directory, reference):
        if (
            not isinstance(layer, QgsVectorLayer)
            or not layer.isValid()
            or not layer.isSpatial()
            or not layer.crs().isValid()
        ):
            raise ValueError(
                "Source must be an available spatial vector layer with a valid CRS"
            )
        self.source = QgsVectorLayerFeatureSource(layer)
        self.fields = self.source.fields()
        self.managed = (
            ManagedCapture(layer) if layer.customProperty(MANAGED_KEY, "") else None
        )
        self.schema = (
            self.managed.schema if self.managed else _field_schema(self.fields)
        )
        self.renderer = RendererCapture(
            layer,
            reference,
            reserved_fields=[field["name"] for field in self.schema]
            if self.managed
            else (),
        )
        if self.managed:
            logical_names = {
                entry["physical"]: entry["schema"]["name"]
                for entry in self.managed.entries
            }
            source_field = self.renderer.metadata.get("source_field")
            if source_field in logical_names:
                self.renderer.metadata["source_field"] = logical_names[source_field]
        self.class_field = self.renderer.metadata.get("field")
        if self.class_field:
            self.schema.append(
                {
                    "name": self.class_field,
                    "type": "string",
                    "original_type": "aino-renderer-class-v1",
                    "serialization": "json-native",
                    "classification": True,
                }
            )
        if len(self.schema) > 1000:
            self.renderer.close()
            raise ValueError(
                "Snapshot supports at most 1000 fields including classification"
            )
        self.transform = QgsCoordinateTransform(
            layer.crs(),
            QgsCoordinateReferenceSystem("EPSG:4326"),
            project.transformContext(),
        )
        self.exporter = QgsJsonExporter(None, 15)
        self.exporter.setTransformGeometries(False)
        self.iterator = self.source.getFeatures()
        if not self.iterator.isValid():
            self.renderer.close()
            raise ValueError(f"Source features are unavailable: {layer.name()}")
        node = project.layerTreeRoot().findLayer(layer.id())
        self.output = {
            "client_layer_id": layer.id(),
            "name": layer.name(),
            "visible": node.isVisible() if node else True,
            "order": order,
            "style": self.renderer.style,
            "field_schema": self.schema,
            "style_hash": hashlib.sha256(
                canonical_bytes(self.renderer.style)
            ).hexdigest(),
            "warnings": self.renderer.warnings,
            "provenance": {
                "source_crs": layer.crs().authid() or layer.crs().toWkt(),
                "provider": layer.providerType(),
                "selection_limits_export": False,
                "snapshot_semantics": "QGIS feature-source view including edit buffer and provider subset; external provider isolation follows its iterator",
            },
        }
        self.directory = Path(directory)
        self.file = None
        self.path = None
        self.sidecar = None
        self.complete = False
        self.count = 0
        self.class_values = {}
        self.digest = hashlib.sha256()

    def _write(self, data):
        self.file.write(data)
        self.digest.update(data)

    def _open(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(
            prefix="aino-", suffix=".partial", dir=self.directory
        )
        self.path = Path(name)
        self.file = os.fdopen(fd, "wb")
        self._write(
            b'{"type":"FeatureCollection","field_schema":'
            + canonical_bytes(self.schema)
            + b',"features":['
        )

    def step(self):
        if self.file is None:
            self._open()
        feature = QgsFeature()
        if not self.iterator.nextFeature(feature):
            self._finish()
            return False
        classification = self.renderer.classify(feature)
        properties = {
            field.name(): _json_value(feature[index])
            for index, field in enumerate(self.fields)
        }
        if self.class_field:
            properties[self.class_field] = classification
        copied = QgsFeature(feature)
        if copied.hasGeometry():
            geometry = copied.geometry()
            geometry.transform(self.transform)
            copied.setGeometry(geometry)
        document = json.loads(self.exporter.exportFeature(copied))
        document.pop("id", None)
        document["properties"] = properties
        if self.managed:
            document = self.managed.feature(copied, document)
            if self.class_field:
                document["properties"] = document["properties"] or {}
                document["properties"][self.class_field] = classification
                values = self.class_values.setdefault(classification, {})
                name = self.managed.metadata.get("server_class_field")
                if name:
                    value = document["properties"].get(name)
                    if not isinstance(value, (dict, list)):
                        values.setdefault(name, {})[canonical_bytes(value)] = value
        self._write((b"," if self.count else b"") + canonical_bytes(document))
        self.count += 1
        return True

    def _finish(self):
        self._write(b"]}")
        self.file.flush()
        os.fsync(self.file.fileno())
        self.file.close()
        self.file = None
        finished = self.path.with_suffix(".geojson")
        self.path.rename(finished)
        self.path = finished
        self.sidecar = finished.with_suffix(".fields.json")
        with self.sidecar.open("xb") as file:
            file.write(canonical_bytes(self.schema))
        self.output.update(
            local_path=str(finished),
            schema_path=str(self.sidecar),
            content_hash=self.digest.hexdigest(),
            feature_count=self.count,
        )
        if self.managed:
            self.output["managed_class_values"] = {
                key: {name: list(values.values()) for name, values in fields.items()}
                for key, fields in self.class_values.items()
            }
        self.complete = True
        self.iterator.close()
        self.renderer.close()

    def close(self, remove=False):
        self.iterator.close()
        self.renderer.close()
        if self.file is not None:
            self.file.close()
            self.file = None
        if remove or not self.complete:
            for path in [self.path, self.sidecar]:
                if path is not None:
                    path.unlink(missing_ok=True)


class SnapshotCapture(QObject):
    """Cooperative Qt-thread streaming job; finished emits only completed files and JSON."""

    finished = pyqtSignal(list)
    failed = pyqtSignal(str)
    progress = pyqtSignal(int)
    cancelled = pyqtSignal()

    def __init__(
        self,
        project,
        selected_ids: list[str],
        directory: str,
        *,
        reference,
        batch_size=128,
        parent=None,
    ):
        super().__init__(parent)
        require_gui_thread()
        if len(selected_ids) != len(set(selected_ids)) or len(selected_ids) > 100:
            raise ValueError("Select at most 100 distinct input layers")
        if batch_size <= 0:
            raise ValueError("Batch size must be positive")
        self.streams = []
        self.batch_size = batch_size
        self.index = 0
        self.active = True
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._tick)
        try:
            selected = set(selected_ids)
            ordered = [
                layer.id()
                for layer in project.layerTreeRoot().layerOrder()
                if layer.id() in selected
            ]
            if set(ordered) != selected:
                raise ValueError("An input layer was removed from the project")
            for order, layer_id in enumerate(ordered):
                self.streams.append(
                    _LayerStream(
                        project, project.mapLayer(layer_id), order, directory, reference
                    )
                )
        except Exception:
            self.cancel()
            raise
        streams = self.streams
        self.destroyed.connect(
            lambda: [stream.close(remove=not stream.complete) for stream in streams]
        )

    def start(self):
        require_gui_thread()
        if self.active:
            self.timer.start(0)

    def _advance(self):
        if self.index == len(self.streams):
            self.active = False
            return False
        stream = self.streams[self.index]
        if not stream.step():
            self.index += 1
        return True

    def _tick(self):
        if not self.active:
            return
        try:
            started = time.monotonic()
            for _ in range(self.batch_size):
                if not self._advance():
                    self.finished.emit([stream.output for stream in self.streams])
                    return
                if time.monotonic() - started > 0.012:
                    break
            self.progress.emit(sum(stream.count for stream in self.streams))
            self.timer.start(0)
        except Exception as error:
            self.cancel()
            self.failed.emit(str(error))

    def cancel(self):
        require_gui_thread()
        self.timer.stop()
        if self.active:
            self.active = False
            for stream in self.streams:
                stream.close(remove=True)
            self.cancelled.emit()


def capture_layers(
    project, selected_ids: list[str], directory: str, *, reference=None
) -> list[dict]:
    """Synchronous non-UI convenience; interactive callers use SnapshotCapture.start."""
    if reference is None:
        raise ValueError("Layer capture requires the current canvas RenderReference")
    capture = SnapshotCapture(project, selected_ids, directory, reference=reference)
    try:
        while capture._advance():
            pass
        return [stream.output for stream in capture.streams]
    except Exception:
        capture.cancel()
        raise
