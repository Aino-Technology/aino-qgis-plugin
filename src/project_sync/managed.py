import json
import math
from datetime import date, datetime, time

from osgeo import ogr
from qgis.PyQt.QtCore import QObject
from qgis.core import QgsVariantUtils

from .styles import canonical_bytes


MANAGED_KEY = "aino/managed_output_v1"
GEOMETRY_TYPES = {
    "Point",
    "MultiPoint",
    "LineString",
    "MultiLineString",
    "Polygon",
    "MultiPolygon",
}


def loads(value):
    return json.loads(
        value,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError("Nonfinite JSON value")
        ),
    )


def validate_value(value, schema):
    if value is None:
        return
    kind = schema["type"]
    if kind == "json":
        canonical_bytes(value)
        return
    valid = {
        "string": isinstance(value, str),
        "integer": type(value) is int,
        "number": type(value) in (int, float) and math.isfinite(value),
        "boolean": type(value) is bool,
    }.get(kind)
    if kind in {"date", "datetime", "time"}:
        if not isinstance(value, str):
            raise ValueError(f"Invalid temporal value in {schema['name']}")
        {"date": date, "datetime": datetime, "time": time}[kind].fromisoformat(
            value.replace("Z", "+00:00")
        )
        return
    if valid is not True:
        raise ValueError(f"Invalid {kind} value in {schema['name']}")


def decode(value, entry):
    if QgsVariantUtils.isNull(value):
        return None
    if entry["codec"] == "json":
        value = loads(value)
    elif entry["codec"] == "boolean":
        if value not in (0, 1):
            raise ValueError("Invalid managed boolean")
        value = bool(value)
    if entry["codec"] != "json" or entry.get("validate", True):
        validate_value(value, entry["schema"])
    return value


def encode(value, entry):
    if entry["codec"] == "json":
        return canonical_bytes(value).decode()
    if value is None:
        return None
    return int(value) if entry["codec"] == "boolean" else value


def geometry_json(geometry):
    if geometry is None:
        return None
    kind = ogr.GeometryTypeToName(ogr.GT_Flatten(geometry.GetGeometryType())).replace(
        " ", ""
    )
    kind = {"3DPoint": "Point", "LinearRing": "LineString"}.get(kind, kind)
    names = {name.lower(): name for name in GEOMETRY_TYPES}
    name = names.get(kind.lower())
    if name is None:
        raise ValueError(f"Unsupported edited geometry: {kind}")

    def coordinate(index):
        point = geometry.GetPointZM(index)
        return list(point[:3] if geometry.Is3D() else point[:2]) + (
            [point[3]] if geometry.IsMeasured() else []
        )

    if name == "Point":
        coords = [] if geometry.IsEmpty() else coordinate(0)
    elif name == "LineString":
        coords = [coordinate(i) for i in range(geometry.GetPointCount())]
    elif name == "Polygon":
        coords = [
            geometry_json(geometry.GetGeometryRef(i))["coordinates"]
            for i in range(geometry.GetGeometryCount())
        ]
    else:
        coords = [
            geometry_json(geometry.GetGeometryRef(i))["coordinates"]
            for i in range(geometry.GetGeometryCount())
        ]
    return dict(type=name, coordinates=coords)


def geometry_from_json(document):
    if document is None:
        return None
    kind, coords = document["type"], document["coordinates"]
    if kind not in GEOMETRY_TYPES or not isinstance(coords, list):
        raise ValueError("Unsupported output geometry")
    points = []

    def inspect(value):
        if not value:
            return
        if isinstance(value[0], (int, float)):
            if len(value) not in (2, 3, 4) or any(
                type(v) not in (int, float) or not math.isfinite(v) for v in value
            ):
                raise ValueError("Invalid output coordinate")
            points.append(value)
        else:
            for child in value:
                if not isinstance(child, list):
                    raise ValueError("Invalid coordinate nesting")
                inspect(child)

    inspect(coords)
    dimensions = {len(point) for point in points}
    if len(dimensions) > 1:
        raise ValueError("A geometry has inconsistent coordinate dimensions")
    dim = next(iter(dimensions), 2)

    def wkt(value):
        if not value:
            return "EMPTY"
        if isinstance(value[0], (int, float)):
            return " ".join(str(v) for v in value)
        return "(" + ",".join(wkt(v) for v in value) + ")"

    body = wkt(coords)
    if kind == "Point" and coords:
        body = "(" + body + ")"
    suffix = {2: "", 3: " Z", 4: " ZM"}[dim]
    geometry = ogr.CreateGeometryFromWkt(kind.upper() + suffix + " " + body)
    if geometry is None:
        raise ValueError("Invalid output geometry")
    return geometry


class ManagedCapture:
    def __init__(self, layer):
        error = layer.customProperty("aino/managed_edit_error", "")
        if error:
            raise ValueError(error)
        buffer = layer.editBuffer()
        self.edited_fields = (
            buffer.changedAttributeValues() if buffer is not None else {}
        )
        self.metadata = loads(layer.customProperty(MANAGED_KEY))
        if self.metadata.get("version") != 1:
            raise ValueError("Unsupported managed output codec version")
        self.entries = self.metadata["fields"]
        self.schema = [entry["schema"] for entry in self.entries]
        self.indices = [
            layer.fields().indexFromName(entry["physical"]) for entry in self.entries
        ]
        self.meta_index = layer.fields().indexFromName(
            self.metadata["feature_metadata"]
        )
        if min(self.indices + [self.meta_index]) < 0 or len(layer.fields()) != len(
            self.entries
        ) + 1 + len(self.metadata.get("internal_fields", [])):
            raise ValueError(
                "Managed output schema was changed; restore its mapped fields before synchronization"
            )

    def feature(self, feature, document):
        props = {
            entry["schema"]["name"]: decode(feature[index], entry)
            for entry, index in zip(self.entries, self.indices)
        }
        raw = feature[self.meta_index]
        metadata = {} if QgsVariantUtils.isNull(raw) else loads(raw)
        geometry = feature.geometry()
        actual = (
            None
            if geometry.isNull()
            else ogr.CreateGeometryFromWkb(bytes(geometry.asWkb()))
        )
        current = geometry_json(actual)
        if current == metadata.get("stored_geometry"):
            current = metadata.get("geometry", current)
        document.pop("bbox", None)
        document["geometry"] = current
        edited = {
            entry["schema"]["name"]
            for entry, index in zip(self.entries, self.indices)
            if index in self.edited_fields.get(feature.id(), {})
        }
        for missing in metadata.get("missing", []):
            if props.get(missing) is None and missing not in edited:
                props.pop(missing, None)
        document["properties"] = (
            None
            if metadata.get("null_properties")
            and not edited
            and not any(value is not None for value in props.values())
            else props
        )
        if metadata.get("has_id"):
            document["id"] = metadata["id"]
        return document


class ManagedOutputPresence(QObject):
    def __init__(self, layer):
        super().__init__(layer)
        self.setObjectName("aino-managed-output-presence")
        self.layer = layer
        layer.beforeCommitChanges.connect(self._before_commit)
        layer.afterRollBack.connect(self._rolled_back)

    def _rolled_back(self):
        self.layer.removeCustomProperty("aino/managed_edit_error")

    def _before_commit(self, *args):
        try:
            buffer = self.layer.editBuffer()
            if buffer is None:
                return
            mapping = loads(self.layer.customProperty(MANAGED_KEY))
            names = {
                self.layer.fields().indexFromName(entry["physical"]): entry["schema"][
                    "name"
                ]
                for entry in mapping["fields"]
            }
            index = self.layer.fields().indexFromName(mapping["feature_metadata"])
            for fid, attributes in buffer.changedAttributeValues().items():
                edited = {names[key] for key in attributes if key in names}
                if not edited:
                    continue
                feature = self.layer.getFeature(fid)
                raw = feature[index]
                if QgsVariantUtils.isNull(raw):
                    continue
                metadata = loads(raw)
                metadata["missing"] = [
                    name for name in metadata.get("missing", []) if name not in edited
                ]
                metadata["null_properties"] = False
                if not self.layer.changeAttributeValue(
                    fid, index, canonical_bytes(metadata).decode()
                ):
                    raise ValueError("Cannot persist explicit NULL property edit")
        except Exception as error:
            self.layer.setCustomProperty("aino/managed_edit_error", str(error))


def watch_managed_output(layer):
    if (
        layer.customProperty(MANAGED_KEY, "")
        and layer.findChild(ManagedOutputPresence, "aino-managed-output-presence")
        is None
    ):
        ManagedOutputPresence(layer)
