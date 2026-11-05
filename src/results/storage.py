import hashlib
import os
from pathlib import Path
from uuid import uuid4

from osgeo import gdal, ogr, osr

from src.project_sync.managed import (
    GEOMETRY_TYPES,
    encode,
    field_marker,
    geometry_from_json,
    geometry_json,
    loads,
    validate_value,
)
from src.project_sync.styles import canonical_bytes


def read_document(path, result):
    with open(path, "rb") as stream:
        document = loads(stream.read())
    if (
        not isinstance(document, dict)
        or document.get("type") != "FeatureCollection"
        or not isinstance(document.get("features"), list)
    ):
        raise ValueError("Export is not a complete GeoJSON FeatureCollection")
    schema = result["field_schema"]
    names = [item["name"] for item in schema]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate schema field names")
    for item in document["features"]:
        if (
            not isinstance(item, dict)
            or item.get("type") != "Feature"
            or "geometry" not in item
        ):
            raise ValueError("Export contains an invalid feature")
        props = item.get("properties")
        if props is not None and (
            not isinstance(props, dict) or set(props) - set(names)
        ):
            raise ValueError(
                "Export properties do not match authoritative field schema"
            )
        geometry = geometry_from_json(item["geometry"])
        if geometry is not None and geometry_json(geometry) != item["geometry"]:
            raise ValueError(
                "Output geometry cannot be represented without changing coordinates"
            )
    return document


def _entries(schema, features, style):
    classified = (
        {group.get("column") for group in style.get("style", {}).get("groups", [])}
        if style.get("dataset_type") == "groups"
        else set()
    )
    entries = []
    for field_index, item in enumerate(schema):
        values = [
            (feature.get("properties") or {}).get(item["name"]) for feature in features
        ]
        valid = True
        for value in values:
            try:
                validate_value(value, item)
            except (TypeError, ValueError, OverflowError):
                valid = False
        kind = item["type"]
        codec = "native"
        if (
            item["name"] in classified
            or not valid
            or kind == "json"
            or (kind == "number" and any(type(value) is int for value in values))
            or (
                kind == "integer"
                and any(
                    type(value) is int and not -(2**63) <= value < 2**63
                    for value in values
                )
            )
        ):
            codec = "json"
        elif kind == "boolean":
            codec = "boolean"
        entries.append(
            dict(
                physical=f"f_{field_index:03d}",
                schema=item,
                codec=codec,
                validate=valid,
            )
        )
    return entries


def build_storage(path, result, cancelled=lambda: False):
    with gdal.ExceptionMgr(), ogr.ExceptionMgr(), osr.ExceptionMgr():
        return _build_storage(path, result, cancelled)


def _build_storage(path, result, cancelled):
    document = read_document(path, result)
    features = document["features"]
    entries = _entries(result["field_schema"], features, result["style"])
    names = {entry["schema"]["name"] for entry in entries}
    meta = "_aino_feature"
    while meta in names:
        meta += "_"
    groups, nulls = {}, []
    for feature in features:
        geometry = geometry_from_json(feature["geometry"])
        if geometry is None:
            nulls.append(feature)
        else:
            key = str(geometry.GetGeometryType())
            groups.setdefault(key, []).append(feature)
    if not groups:
        kind = next(
            (v for v in result.get("geometry_types", []) if v in GEOMETRY_TYPES),
            "Point",
        )
        groups[
            str(geometry_from_json(dict(type=kind, coordinates=[])).GetGeometryType())
        ] = []
    groups[next(iter(groups))].extend(nulls)
    outputs = []
    for part_id, items in groups.items():
        if cancelled():
            raise InterruptedError("Import stopped")
        root = Path(path).parent / ("storage-" + uuid4().hex)
        root.mkdir(mode=0o700)
        target = root / "output.gpkg"
        driver = ogr.GetDriverByName("GPKG")
        dataset = driver.CreateDataSource(str(target))
        crs = osr.SpatialReference()
        crs.ImportFromEPSG(4326)
        layer = dataset.CreateLayer(
            "output",
            crs,
            int(part_id),
            options=["FID=_aino_rowid", "GEOMETRY_NAME=_aino_geometry"],
        )
        dataset.ExecuteSQL(
            "UPDATE gpkg_geometry_columns SET z=2, m=2 WHERE table_name='output'"
        )
        for entry in entries:
            kind = entry["schema"]["type"]
            type_id = (
                ogr.OFTString
                if entry["codec"] == "json"
                else {
                    "integer": ogr.OFTInteger64,
                    "number": ogr.OFTReal,
                    "boolean": ogr.OFTInteger,
                }.get(kind, ogr.OFTString)
            )
            definition = ogr.FieldDefn(entry["physical"], type_id)
            definition.SetComment(field_marker(entry))
            if layer.CreateField(definition) != 0:
                raise ValueError("Cannot create lossless output field")
        layer.CreateField(ogr.FieldDefn(meta, ogr.OFTString))
        if dataset.StartTransaction() != 0:
            raise ValueError("Cannot start persistent output transaction")
        for item in items:
            if cancelled():
                dataset = None
                raise InterruptedError("Import stopped")
            feature = ogr.Feature(layer.GetLayerDefn())
            properties = item.get("properties") or {}
            for index, entry in enumerate(entries):
                value = encode(properties.get(entry["schema"]["name"]), entry)
                if value is not None:
                    feature.SetField(index, value)
            geometry = geometry_from_json(item["geometry"])
            if geometry is not None:
                feature.SetGeometry(geometry)
            metadata = dict(
                has_id="id" in item,
                id=item.get("id"),
                missing=[name for name in names if name not in properties],
                null_properties=item.get("properties") is None,
                geometry=item["geometry"],
                stored_geometry=geometry_json(geometry)
                if geometry is not None
                else None,
            )
            feature.SetField(len(entries), canonical_bytes(metadata).decode())
            if layer.CreateFeature(feature) != 0:
                raise ValueError("Cannot persist output feature")
        if dataset.CommitTransaction() != 0:
            raise ValueError("Cannot commit persistent output transaction")
        layer = None
        dataset = None
        with target.open("rb") as stream:
            os.fsync(stream.fileno())
        reopened = ogr.Open(str(target))
        stored = reopened.GetLayer(0)
        if stored.GetFeatureCount() != len(items):
            raise ValueError("Persistent output lost features")
        for saved, original in zip(stored, items):
            properties = original.get("properties") or {}
            for index, entry in enumerate(entries):
                expected = encode(properties.get(entry["schema"]["name"]), entry)
                if saved.GetField(index) != expected:
                    raise ValueError("Persistent output changed a field value")
            expected_geometry = geometry_from_json(original["geometry"])
            expected_json = (
                geometry_json(expected_geometry)
                if expected_geometry is not None
                else None
            )
            if geometry_json(saved.GetGeometryRef()) != expected_json:
                raise ValueError("Persistent output changed a geometry")
        stored = None
        reopened = None
        mapping = dict(
            version=1,
            fields=entries,
            feature_metadata=meta,
            internal_fields=["_aino_rowid"],
            field_lineage=1,
        )
        sidecar = dict(
            mapping=mapping,
            result=result,
            part_id=part_id,
            raw_path=str(path),
            wire_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        )
        with (root / "metadata.json").open("xb") as stream:
            stream.write(canonical_bytes(sidecar))
            stream.flush()
            os.fsync(stream.fileno())
        for directory in (root, root.parent):
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        outputs.append(
            dict(
                path=str(target),
                mapping=mapping,
                part_id=part_id,
                feature_count=len(items),
            )
        )
    return outputs
