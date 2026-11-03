import json
from pathlib import Path
from uuid import uuid4

import pytest
from qgis.core import QgsProject

from src.project_sync.layers import capture_layers
from src.project_sync.recovery import PROVENANCE_KEY
from src.results.importer import import_result
from tests.test_style_conversion import reference


def logical_index(layer, name):
    from src.project_sync.managed import MANAGED_KEY

    metadata = json.loads(layer.customProperty(MANAGED_KEY))
    physical = next(
        entry["physical"]
        for entry in metadata["fields"]
        if entry["schema"]["name"] == name
    )
    return layer.fields().indexFromName(physical)


def field(name, kind):
    return dict(
        name=name,
        type=kind,
        original_type=kind,
        serialization="json-native",
        classification=False,
    )


def result(**extra):
    return dict(
        task_id=str(uuid4()),
        account_id="account",
        client_project_id=str(uuid4()),
        project_id=17,
        dataset_id=71,
        version=1,
        content_hash="server-identity",
        name="Output",
        role="output",
        field_schema=[
            field("Name", "string"),
            field("name", "string"),
            field("big", "integer"),
            field("data", "json"),
            field("time", "datetime"),
        ],
        geometry_types=["Point"],
        style=dict(
            dataset_type="default",
            style=dict(
                color="#ff0000",
                opacity=0.5,
                stroke="#000000",
                strokeOpacity=1,
                lineWidth=2,
                pointSize=1,
            ),
            qgis={},
        ),
        **extra,
    )


def document(geometry):
    return dict(
        type="FeatureCollection",
        features=[
            dict(
                type="Feature",
                id=None,
                geometry=geometry,
                properties=dict(
                    Name="Москва",
                    name="low",
                    big=-9223372036854775807,
                    data={"a": [1, "1", None]},
                    time="2026-11-03T12:30:45.123456789+03:00",
                ),
            )
        ],
    )


def write(tmp_path, data):
    path = tmp_path / "raw.geojson"
    path.write_text(json.dumps(data))
    return str(path)


GEOMETRIES = [
    dict(type="Point", coordinates=[1, 2, 3]),
    dict(type="MultiPoint", coordinates=[[1, 2], [3, 4]]),
    dict(type="LineString", coordinates=[[1, 2], [3, 4]]),
    dict(type="MultiLineString", coordinates=[[[1, 2], [3, 4]]]),
    dict(
        type="Polygon",
        coordinates=[
            [[0, 0], [4, 0], [4, 4], [0, 0]],
            [[1, 1], [2, 1], [2, 2], [1, 1]],
        ],
    ),
    dict(type="MultiPolygon", coordinates=[[[[0, 0], [4, 0], [4, 4], [0, 0]]]]),
    None,
    dict(type="Point", coordinates=[]),
]


@pytest.mark.parametrize("geometry", GEOMETRIES)
def test_persistent_geometry_and_logical_schema(qgis_app, tmp_path, geometry):
    project = QgsProject()
    data, manifest = document(geometry), result()
    ids = import_result(project, manifest, write(tmp_path, data), reference=reference())
    assert ids
    layer = project.mapLayer(ids[0])
    assert (
        layer.providerType() == "ogr" and Path(layer.source().split("|")[0]).is_file()
    )
    assert (
        json.loads(layer.customProperty(PROVENANCE_KEY))["content_hash"]
        == "server-identity"
    )
    captured = capture_layers(
        project, ids, str(tmp_path / "capture"), reference=reference()
    )[0]
    actual = json.loads(Path(captured["local_path"]).read_text())
    assert actual["field_schema"] == manifest["field_schema"]
    assert actual["features"] == data["features"]
    assert (
        import_result(
            project, manifest, str(tmp_path / "raw.geojson"), reference=reference()
        )
        == ids
    )
    assert project.write(str(tmp_path / "saved.qgz"))
    project.clear()
    assert project.read(str(tmp_path / "saved.qgz"))
    restored = capture_layers(
        project,
        list(project.mapLayers()),
        str(tmp_path / "restored"),
        reference=reference(),
    )[0]
    assert restored["content_hash"] == captured["content_hash"]
    project.clear()


@pytest.mark.parametrize("dimension", [2, 3, 4])
@pytest.mark.parametrize("geometry", GEOMETRIES[:6])
def test_all_dimensions_and_actual_geometry_edit(
    qgis_app, tmp_path, dimension, geometry
):
    import copy
    from qgis.core import QgsGeometry
    from src.project_sync.managed import geometry_from_json

    geometry = copy.deepcopy(geometry)

    def resize(value):
        if isinstance(value[0], (float, int)):
            value[:] = (value[:2] + [3, 4])[:dimension]
        else:
            for item in value:
                resize(item)

    resize(geometry["coordinates"])
    project = QgsProject()
    manifest = result()
    ids = import_result(
        project, manifest, write(tmp_path, document(geometry)), reference=reference()
    )
    layer = project.mapLayer(ids[0])
    captured = capture_layers(
        project, ids, str(tmp_path / "before"), reference=reference()
    )[0]
    assert (
        json.loads(Path(captured["local_path"]).read_text())["features"][0]["geometry"]
        == geometry
    )
    changed = copy.deepcopy(geometry)

    def move(value):
        if isinstance(value[0], (int, float)):
            value[0] += 0.5
        else:
            for item in value:
                move(item)

    move(changed["coordinates"])
    native = QgsGeometry()
    native.fromWkb(bytes(geometry_from_json(changed).ExportToWkb()))
    assert layer.startEditing()
    assert layer.changeGeometry(next(layer.getFeatures()).id(), native)
    assert layer.commitChanges()
    after = capture_layers(
        project, ids, str(tmp_path / "after"), reference=reference()
    )[0]
    assert (
        json.loads(Path(after["local_path"]).read_text())["features"][0]["geometry"]
        == changed
    )
    project.clear()


def test_codec_edits_nulls_empty_schema_and_new_version(qgis_app, tmp_path):
    import copy

    manifest = result()
    manifest["field_schema"] += [
        field("mixed", "json"),
        field("null", "number"),
        field("huge", "integer"),
    ]
    data = document(None)
    values = ["1", 1, True, None, "null", {"n": 9007199254740993}, [None, "1", 1]]
    data["features"] = [
        dict(
            type="Feature",
            geometry=None,
            properties={
                **data["features"][0]["properties"],
                "mixed": value,
                "null": None,
                "huge": 2**80,
            },
        )
        for value in values
    ]
    project = QgsProject()
    ids = import_result(project, manifest, write(tmp_path, data), reference=reference())
    layer = project.mapLayer(ids[0])
    baseline = capture_layers(
        project, ids, str(tmp_path / "base"), reference=reference()
    )[0]
    assert (
        json.loads(Path(baseline["local_path"]).read_text())["features"]
        == data["features"]
    )
    feature = next(layer.getFeatures())
    assert feature.geometry().isNull()
    assert layer.startEditing()
    assert layer.changeAttributeValue(
        feature.id(),
        logical_index(layer, "data"),
        '{"edited":9007199254740995}',
    )
    assert layer.changeAttributeValue(
        feature.id(), logical_index(layer, "Name"), "edited"
    )
    assert layer.commitChanges()
    changed = capture_layers(
        project, ids, str(tmp_path / "changed"), reference=reference()
    )[0]
    assert changed["content_hash"] != baseline["content_hash"]
    assert json.loads(Path(changed["local_path"]).read_text())["features"][0][
        "properties"
    ]["data"] == {"edited": 9007199254740995}
    assert (
        import_result(
            project, manifest, str(tmp_path / "raw.geojson"), reference=reference()
        )
        == ids
    )
    assert next(layer.getFeatures())[logical_index(layer, "Name")] == "edited"
    newer = copy.deepcopy(manifest)
    newer.update(version=2, content_hash="other")
    new_ids = import_result(
        project, newer, str(tmp_path / "raw.geojson"), reference=reference()
    )
    assert new_ids != ids and project.mapLayer(new_ids[0]).name() == "Output (v2)"
    assert layer.startEditing()
    assert layer.changeAttributeValue(
        feature.id(), logical_index(layer, "data"), "not json"
    )
    with pytest.raises(ValueError):
        capture_layers(project, ids, str(tmp_path / "invalid"), reference=reference())
    layer.rollBack()
    project.clear()
    manifest["field_schema"] = [field("empty", "number")]
    empty = dict(type="FeatureCollection", features=[])
    ids = import_result(
        project, manifest, write(tmp_path, empty), reference=reference()
    )
    captured = capture_layers(
        project, ids, str(tmp_path / "empty"), reference=reference()
    )[0]
    assert (
        captured["feature_count"] == 0
        and captured["field_schema"] == manifest["field_schema"]
    )
    project.clear()


def test_split_output_reuse_and_subset_never_expands(qgis_app, tmp_path):
    from src.project_sync.controller import _output_references

    project = QgsProject()
    manifest = result()
    data = document(None)
    data["features"] = [
        document(geometry)["features"][0] for geometry in GEOMETRIES[:6]
    ]
    ids = import_result(project, manifest, write(tmp_path, data), reference=reference())
    assert len(ids) == 6 and len({project.mapLayer(i).name() for i in ids}) == 1
    captured = capture_layers(
        project, ids, str(tmp_path / "capture"), reference=reference()
    )
    for item in captured:
        item["output_provenance"] = json.loads(
            project.mapLayer(item["client_layer_id"]).customProperty(PROVENANCE_KEY)
        )
    references = _output_references(
        captured, "account", manifest["client_project_id"], 17
    )
    assert len(references) == 6
    assert [item for item in references.values() if item][0][
        "client_layer_id"
    ] == "output:71"
    assert not _output_references(
        captured[:1], "account", manifest["client_project_id"], 17
    )
    project.clear()


def test_categorized_recolor_has_proven_mapping(qgis_app, tmp_path):
    from qgis.PyQt.QtGui import QColor
    from src.project_sync.controller import _output_references

    project = QgsProject()
    manifest = result()
    base = manifest["style"]["style"]
    manifest["style"] = dict(
        dataset_type="groups",
        style={
            **base,
            "groups": [
                dict(base, column="name", values=[], name="Other", isDefault=True),
                dict(
                    base, column="name", values=["low"], name="Lower", isDefault=False
                ),
            ],
        },
        qgis={},
    )
    ids = import_result(
        project,
        manifest,
        write(tmp_path, document(GEOMETRIES[0])),
        reference=reference(),
    )
    layer = project.mapLayer(ids[0])
    provenance = json.loads(layer.customProperty(PROVENANCE_KEY))
    assert provenance["class_mapping"]["server_field"] == "name"
    categories = layer.renderer().categories()
    symbol = categories[1].symbol().clone()
    symbol.setColor(QColor("blue"))
    layer.renderer().updateCategorySymbol(1, symbol)
    captured = capture_layers(
        project, ids, str(tmp_path / "capture"), reference=reference()
    )
    captured[0]["output_provenance"] = provenance
    assert captured[0]["content_hash"] == provenance["local_content_hash"]
    references = _output_references(
        captured, "account", manifest["client_project_id"], 17
    )
    assert references[ids[0]]["dataset_id"] == 71
    assert references[ids[0]]["style"]["style"]["groups"][2]["values"] == ["low"]
    project.clear()


@pytest.mark.parametrize(
    "geometry",
    [
        dict(type=kind, coordinates=[])
        for kind in [
            "Point",
            "MultiPoint",
            "LineString",
            "MultiLineString",
            "Polygon",
            "MultiPolygon",
        ]
    ],
)
def test_empty_and_null_geometry_are_distinct(qgis_app, tmp_path, geometry):
    project = QgsProject()
    data = document(geometry)
    data["features"].append(dict(type="Feature", geometry=None, properties=None))
    manifest = result()
    ids = import_result(project, manifest, write(tmp_path, data), reference=reference())
    captured = capture_layers(
        project, ids, str(tmp_path / "capture"), reference=reference()
    )[0]
    assert (
        json.loads(Path(captured["local_path"]).read_text())["features"]
        == data["features"]
    )
    project.clear()


@pytest.mark.parametrize(
    "failure", ["truncated", "invalid_json", "network", "redirect"]
)
def test_download_rejects_incomplete_or_invalid_and_keeps_prior_file(
    client, local_api, wait_for, tmp_path, failure
):
    from src.results.download import ResultDownloader
    from tests.test_api_client import token

    session, api = client
    session.set_access_token(token())
    downloader = ResultDownloader(api)
    replies = []
    manifest = dict(task_id=str(uuid4()), layers=[result()])
    downloader.download_results(
        manifest["task_id"], manifest, str(tmp_path), lambda *args: replies.append(args)
    )
    wait_for(lambda: len(local_api.requests) == 1)
    assert "expected_version=1" in local_api.requests[0]["path"]
    local_api.respond(0, data={"url": local_api.origin + "/signed"})
    wait_for(lambda: len(local_api.requests) == 2)
    assert "Authorization" not in local_api.requests[1]["headers"]
    body = json.dumps(document(GEOMETRIES[0])).encode()
    if failure == "truncated":
        local_api.respond(1, raw=body, headers={"Content-Length": str(len(body) + 10)})
    elif failure == "invalid_json":
        local_api.respond(1, raw=b'{"type":"FeatureCollection","features":[')
    elif failure == "network":
        local_api.respond(1, raw=b"{", headers={"Content-Length": "100"})
    else:
        local_api.respond(
            1, status=302, headers={"Location": local_api.origin + "/elsewhere"}
        )
    wait_for(lambda: replies)
    assert replies[0][0] is None and replies[0][1]
    assert not list(tmp_path.rglob("*.geojson"))
    downloader.close()
    downloader.deleteLater()


def test_download_refresh_pins_version_and_never_sends_bearer(
    client, local_api, wait_for, tmp_path
):
    from src.results.download import ResultDownloader
    from tests.test_api_client import token

    session, api = client
    session.set_access_token(token())
    downloader = ResultDownloader(api)
    replies = []
    manifest = dict(task_id=str(uuid4()), layers=[result()])
    downloader.download_results(
        manifest["task_id"], manifest, str(tmp_path), lambda *args: replies.append(args)
    )
    wait_for(lambda: len(local_api.requests) == 1)
    local_api.respond(0, data={"url": local_api.origin + "/expired"})
    wait_for(lambda: len(local_api.requests) == 2)
    local_api.respond(1, status=403, data={"expired": True})
    wait_for(lambda: len(local_api.requests) == 3)
    assert local_api.requests[2]["path"] == local_api.requests[0]["path"]
    local_api.respond(2, data={"url": local_api.origin + "/fresh"})
    wait_for(lambda: len(local_api.requests) == 4)
    assert all("Authorization" not in local_api.requests[i]["headers"] for i in [1, 3])
    local_api.respond(3, data=document(GEOMETRIES[0]))
    wait_for(lambda: replies, timeout=10000)
    assert replies[0][1] == ""
    output = replies[0][0][0]
    assert (
        Path(output["local_path"]).is_file()
        and Path(output["storage"][0]["path"]).is_file()
    )
    assert output["result"]["content_hash"] == "server-identity"
    downloader.close()
    downloader.deleteLater()


def test_controller_layer_add_before_journal_recovery_and_scope(
    client, local_api, wait_for, tmp_path, monkeypatch
):
    from qgis.PyQt import sip
    from src.project_sync.recovery import RecoveryJournal, project_identity
    from src.results.controller import ResultController
    from tests.test_api_client import token

    session, api = client
    session.set_access_token(token())
    project = QgsProject()
    identity = project_identity(project)
    journal = RecoveryJournal(tmp_path)
    analysis = dict(
        start_dispatched=True,
        action_dispatched=True,
        last_rejection={"error": "retained"},
    )
    journal.update_sections(
        identity,
        session.account_id,
        analysis=analysis,
        sync={"intact": True},
        datasets={"data": "retained"},
    )
    controller = ResultController(api, journal, project)
    completed, failures = [], []
    controller.completed.connect(completed.append)
    controller.failed.connect(failures.append)
    manifest = dict(task_id=str(uuid4()), report="Full report", layers=[result()])
    original = journal.update_sections

    def fail_completion(*args, **kwargs):
        if (
            kwargs.get("imports", {})
            .get("tasks", {})
            .get(manifest["task_id"], {})
            .get("status")
            == "complete"
        ):
            raise OSError("simulated layer-add-before-journal-save crash")
        return original(*args, **kwargs)

    monkeypatch.setattr(journal, "update_sections", fail_completion)
    controller.start(manifest, 17, reference())
    wait_for(lambda: len(local_api.requests) == 1)
    local_api.respond(0, data={"url": local_api.origin + "/result"})
    wait_for(lambda: len(local_api.requests) == 2)
    local_api.respond(1, data=document(GEOMETRIES[0]))
    wait_for(lambda: failures, timeout=10000)
    ids = list(project.mapLayers())
    assert len(ids) == 1
    provenance = json.loads(project.mapLayer(ids[0]).customProperty(PROVENANCE_KEY))
    assert provenance["account_id"] == session.account_id
    controller.close()
    sip.delete(controller)
    monkeypatch.setattr(journal, "update_sections", original)
    controller = ResultController(api, journal, project)
    controller.completed.connect(completed.append)
    controller.start(manifest, 17, reference())
    wait_for(lambda: completed)
    assert completed == [ids] and list(project.mapLayers()) == ids
    assert len(local_api.requests) == 2
    saved = journal.load(identity, session.account_id)
    assert (
        saved["analysis"] == analysis
        and saved["sync"] == {"intact": True}
        and saved["datasets"] == {"data": "retained"}
    )
    controller.close()
    sip.delete(controller)
    project.clear()
    sip.delete(project)


def test_text_only_and_stale_account_completion(client, local_api, wait_for, tmp_path):
    from qgis.PyQt import sip
    from src.project_sync.recovery import RecoveryJournal, project_identity
    from src.results.controller import ResultController
    from tests.test_api_client import token

    session, api = client
    session.set_access_token(token())
    project = QgsProject()
    project_identity(project)
    controller = ResultController(api, RecoveryJournal(tmp_path), project)
    complete = []
    controller.completed.connect(complete.append)
    controller.start(
        dict(task_id=str(uuid4()), layers=[], report={"text": "done"}), 17, reference()
    )
    assert complete == [[]] and not local_api.requests
    controller.start(
        dict(task_id=str(uuid4()), layers=[result()], report=""), 17, reference()
    )
    wait_for(lambda: len(local_api.requests) == 1)
    session.clear()
    local_api.respond(0, data={"url": local_api.origin + "/never"})
    assert complete == [[]] and not project.mapLayers()
    controller.close()
    sip.delete(controller)
    sip.delete(project)


def test_failed_replacement_keeps_valid_download(client, local_api, wait_for, tmp_path):
    from src.results.download import ResultDownloader
    from tests.test_api_client import token

    session, api = client
    session.set_access_token(token())
    downloader = ResultDownloader(api)
    replies = []
    manifest = dict(task_id=str(uuid4()), layers=[result()])
    downloader.download_results(
        manifest["task_id"], manifest, str(tmp_path), lambda *args: replies.append(args)
    )
    wait_for(lambda: len(local_api.requests) == 1)
    local_api.respond(0, data={"url": local_api.origin + "/signed"})
    wait_for(lambda: len(local_api.requests) == 2)
    local_api.respond(1, data=document(GEOMETRIES[0]))
    wait_for(lambda: replies, timeout=10000)
    path = Path(replies[0][0][0]["local_path"])
    good = path.read_bytes()
    downloader.download_results(
        manifest["task_id"], manifest, str(tmp_path), lambda *args: replies.append(args)
    )
    wait_for(lambda: len(local_api.requests) == 3)
    local_api.respond(2, data={"url": local_api.origin + "/signed"})
    wait_for(lambda: len(local_api.requests) == 4)
    body = json.dumps(document(None)).encode()
    local_api.respond(3, raw=body, headers={"Content-Length": str(len(body) + 1)})
    wait_for(lambda: len(replies) == 2)
    assert replies[1][0] is None and path.read_bytes() == good
    assert not list(tmp_path.rglob(".download-*"))
    downloader.close()
    downloader.deleteLater()


def test_saved_project_fresh_process_reopens_exact_data(qgis_app, tmp_path):
    import os
    import subprocess
    import sys

    project = QgsProject()
    manifest = result()
    data = document(dict(type="Point", coordinates=[1, 2, 3, 4]))
    data["features"].append(document(None)["features"][0])
    ids = import_result(project, manifest, write(tmp_path, data), reference=reference())
    layer = project.mapLayer(ids[0])
    assert layer.startEditing()
    assert layer.changeAttributeValue(
        next(layer.getFeatures()).id(),
        logical_index(layer, "data"),
        '{"edited":9007199254740995}',
    )
    assert layer.commitChanges()
    captured = capture_layers(
        project, ids, str(tmp_path / "before"), reference=reference()
    )[0]
    filename = str(tmp_path / "persisted.qgz")
    assert project.write(filename)
    project.clear()
    script = """
import json, os, sys
from pathlib import Path
from qgis.core import QgsApplication, QgsProject
sys.path.insert(0, os.getcwd())
from src.project_sync.layers import capture_layers
from tests.test_style_conversion import reference
root=Path(sys.argv[2]); root.mkdir()
os.environ['XDG_CONFIG_HOME']=str(root)
app=QgsApplication([],False,str(root)); app.initQgis()
project=QgsProject(); assert project.read(sys.argv[1])
output=capture_layers(project,list(project.mapLayers()),str(root/'capture'),reference=reference())
(root/'output.json').write_text(json.dumps(output))
project.clear(); app.exitQgis()
"""
    child = subprocess.run(
        [sys.executable, "-c", script, filename, str(tmp_path / "child")],
        capture_output=True,
        text=True,
        env={**os.environ, "QGIS_CUSTOM_CONFIG_PATH": str(tmp_path / "child")},
        timeout=40,
    )
    assert child.returncode == 0, child.stderr
    reopened = json.loads((tmp_path / "child" / "output.json").read_text())[0]
    assert reopened["content_hash"] == captured["content_hash"]


def test_graduated_metadata_restores_ranges_and_authority_wins(qgis_app, tmp_path):
    from qgis.core import (
        QgsFeature,
        QgsGeometry,
        QgsGraduatedSymbolRenderer,
        QgsRendererRange,
        QgsVectorLayer,
    )
    from tests.test_style_conversion import marker

    project = QgsProject()
    source = QgsVectorLayer("Point?crs=EPSG:4326&field=n:double", "source", "memory")
    source.setRenderer(
        QgsGraduatedSymbolRenderer(
            "n",
            [
                QgsRendererRange(0, 10, marker("red"), "Low"),
                QgsRendererRange(10, 20, marker("blue"), "High"),
            ],
        )
    )
    for number in [0, 10, 20]:
        feature = QgsFeature(source.fields())
        feature.setAttributes([number])
        feature.setGeometry(QgsGeometry.fromWkt("Point (1 2)"))
        source.dataProvider().addFeatures([feature])
    project.addMapLayer(source)
    original = capture_layers(
        project, [source.id()], str(tmp_path / "source"), reference=reference()
    )[0]
    manifest = result()
    manifest.update(field_schema=original["field_schema"], style=original["style"])
    ids = import_result(
        project, manifest, original["local_path"], reference=reference()
    )
    assert isinstance(project.mapLayer(ids[0]).renderer(), QgsGraduatedSymbolRenderer)
    assert len(project.mapLayer(ids[0]).renderer().ranges()) == 2
    altered = json.loads(Path(original["local_path"]).read_text())
    class_field = original["style"]["qgis"]["classification"]["field"]
    altered["features"][0]["properties"][class_field] = original["style"]["qgis"][
        "classification"
    ]["definitions"][1]["id"]
    stale_path = tmp_path / "stale.geojson"
    stale_path.write_text(json.dumps(altered))
    stale = dict(manifest, dataset_id=73)
    stale_ids = import_result(project, stale, str(stale_path), reference=reference())
    assert project.mapLayer(stale_ids[0]).renderer().type() == "categorizedSymbol"
    default = dict(
        manifest, dataset_id=72, style={**manifest["style"], "dataset_type": "default"}
    )
    other = import_result(
        project, default, original["local_path"], reference=reference()
    )
    assert project.mapLayer(other[0]).renderer().type() == "singleSymbol"
    project.clear()


def test_duplicate_names_multiple_outputs_are_distinct(
    client, local_api, wait_for, tmp_path
):
    from qgis.PyQt import sip
    from src.project_sync.recovery import RecoveryJournal, project_identity
    from src.results.controller import ResultController
    from tests.test_api_client import token

    session, api = client
    session.set_access_token(token())
    project = QgsProject()
    project_identity(project)
    controller = ResultController(api, RecoveryJournal(tmp_path), project)
    completed = []
    controller.completed.connect(completed.append)
    one, two = result(), result()
    two["dataset_id"] = 72
    manifest = dict(task_id=str(uuid4()), layers=[one, two], report="full report")
    controller.start(manifest, 17, reference())
    for index in range(2):
        wait_for(lambda: len(local_api.requests) == index * 2 + 1)
        local_api.respond(index * 2, data={"url": local_api.origin + f"/output{index}"})
        wait_for(lambda: len(local_api.requests) == index * 2 + 2)
        local_api.respond(index * 2 + 1, data=document(GEOMETRIES[index]))
    wait_for(lambda: completed, timeout=10000)
    assert len(completed[0]) == 2 and len(project.mapLayers()) == 2
    assert {layer.name() for layer in project.mapLayers().values()} == {"Output"}
    assert {
        layer.customProperty("aino/dataset_id")
        for layer in project.mapLayers().values()
    } == {71, 72}
    controller.close()
    sip.delete(controller)
    project.clear()
    sip.delete(project)


def test_empty_geometry_edit_undo_redo_rollback_and_reopen(qgis_app, tmp_path):
    from qgis.core import QgsGeometry

    project = QgsProject()
    data = document(dict(type="Point", coordinates=[]))
    ids = import_result(project, result(), write(tmp_path, data), reference=reference())
    layer = project.mapLayer(ids[0])
    fid = next(layer.getFeatures()).id()

    def captured_geometry(label):
        output = capture_layers(
            project, [layer.id()], str(tmp_path / label), reference=reference()
        )[0]
        return json.loads(Path(output["local_path"]).read_text())["features"][0][
            "geometry"
        ]

    assert layer.startEditing()
    layer.beginEditCommand("Clear empty geometry")
    assert layer.changeGeometry(fid, QgsGeometry())
    layer.endEditCommand()
    assert captured_geometry("cleared") is None
    layer.undoStack().undo()
    assert captured_geometry("undone") == data["features"][0]["geometry"]
    layer.undoStack().redo()
    assert captured_geometry("redone") is None
    assert layer.rollBack()
    assert captured_geometry("rollback") == data["features"][0]["geometry"]
    assert layer.startEditing()
    layer.beginEditCommand("Clear empty geometry")
    assert layer.changeGeometry(fid, QgsGeometry())
    layer.endEditCommand()
    assert layer.commitChanges()
    assert captured_geometry("committed") is None
    assert project.write(str(tmp_path / "empty-edited.qgz"))
    project.clear()
    assert project.read(str(tmp_path / "empty-edited.qgz"))
    layer = project.mapLayer(ids[0])
    assert captured_geometry("reopened") is None
    project.clear()


def test_edited_cached_file_reimport_retains_original_baseline(
    qgis_app, tmp_path, wait_for
):
    from qgis.core import QgsFeature, QgsGeometry
    from src.results.importer import ImportJob
    from src.results.storage import build_storage

    project = QgsProject()
    manifest = result()
    path = write(tmp_path, document(GEOMETRIES[0]))
    storage = build_storage(path, manifest)
    finished = []
    job = ImportJob(project, manifest, storage, reference())
    job.finished.connect(finished.append)
    job.start()
    wait_for(lambda: finished)
    layer = project.mapLayer(finished[0][0])
    original = json.loads(layer.customProperty(PROVENANCE_KEY))
    assert layer.startEditing()
    assert layer.changeAttributeValue(
        next(layer.getFeatures()).id(), logical_index(layer, "Name"), "edited"
    )
    added = QgsFeature(layer.fields())
    added.setGeometry(QgsGeometry.fromWkt("POINT Z(2 3 4)"))
    assert layer.addFeature(added)
    assert layer.commitChanges()
    project.removeMapLayer(layer.id())
    second = ImportJob(project, manifest, storage, reference())
    second.finished.connect(finished.append)
    second.start()
    wait_for(lambda: len(finished) == 2)
    restored = project.mapLayer(finished[1][0])
    assert restored.featureCount() == 2
    provenance = json.loads(restored.customProperty(PROVENANCE_KEY))
    assert provenance["local_content_hash"] == original["local_content_hash"]
    captured = capture_layers(
        project, [restored.id()], str(tmp_path / "edited"), reference=reference()
    )[0]
    assert captured["content_hash"] != original["local_content_hash"]
    project.clear()


def test_xy_add_to_xyz_managed_layer_must_not_drop_feature(qgis_app, tmp_path):
    from qgis.core import QgsFeature, QgsGeometry

    project = QgsProject()
    ids = import_result(
        project,
        result(),
        write(tmp_path, document(GEOMETRIES[0])),
        reference=reference(),
    )
    layer = project.mapLayer(ids[0])
    assert layer.startEditing()
    feature = QgsFeature(layer.fields())
    feature.setGeometry(QgsGeometry.fromWkt("Point (2 3)"))
    assert layer.addFeature(feature)
    assert layer.commitChanges()
    assert layer.featureCount() == 2
    captured = capture_layers(
        project, ids, str(tmp_path / "capture"), reference=reference()
    )[0]
    features = json.loads(Path(captured["local_path"]).read_text())["features"]
    assert len(features) == 2 and features[-1]["geometry"] == dict(
        type="Point", coordinates=[2, 3]
    )
    project.clear()


def test_close_during_storage_worker_suppresses_delivery(
    client, local_api, wait_for, tmp_path, monkeypatch
):
    import threading
    from qgis.PyQt import sip
    from qgis.PyQt.QtCore import QTimer
    from src.results import download
    from tests.test_api_client import token

    session, api = client
    session.set_access_token(token())
    entered, release = threading.Event(), threading.Event()
    original = download.build_storage

    def delayed(*args):
        entered.set()
        release.wait(5)
        return original(*args)

    monkeypatch.setattr(download, "build_storage", delayed)
    downloader = download.ResultDownloader(api)
    replies = []
    manifest = dict(task_id=str(uuid4()), layers=[result()])
    downloader.download_results(
        manifest["task_id"], manifest, str(tmp_path), lambda *args: replies.append(args)
    )
    wait_for(lambda: len(local_api.requests) == 1)
    local_api.respond(0, data={"url": local_api.origin + "/signed"})
    wait_for(lambda: len(local_api.requests) == 2)
    local_api.respond(1, data=document(GEOMETRIES[0]))
    wait_for(entered.is_set)
    alive = []
    QTimer.singleShot(0, lambda: alive.append(True))
    wait_for(lambda: alive)
    workers = downloader._workers
    downloader.close()
    sip.delete(downloader)
    release.set()
    wait_for(lambda: not workers, timeout=10000)
    assert not replies


def test_export_version_conflict_stops_without_download(
    client, local_api, wait_for, tmp_path
):
    from src.results.download import ResultDownloader
    from tests.test_api_client import token

    session, api = client
    session.set_access_token(token())
    downloader = ResultDownloader(api)
    replies = []
    manifest = dict(task_id=str(uuid4()), layers=[result()])
    downloader.download_results(
        manifest["task_id"], manifest, str(tmp_path), lambda *args: replies.append(args)
    )
    wait_for(lambda: len(local_api.requests) == 1)
    local_api.respond(0, status=409, data={"error_code": "qgis_request_conflict"})
    wait_for(lambda: replies)
    assert replies[0][0] is None and len(local_api.requests) == 1
    assert not list(tmp_path.rglob("*.geojson"))
    downloader.close()
    downloader.deleteLater()


def test_imported_classification_field_never_overwritten_by_recapture(
    qgis_app, tmp_path
):
    project = QgsProject()
    manifest = result()
    manifest["field_schema"].append(field("aino_class", "string"))
    base = manifest["style"]["style"]
    manifest["style"] = dict(
        dataset_type="groups",
        style={
            **base,
            "groups": [
                dict(
                    base,
                    column="aino_class",
                    values=["server-class"],
                    name="Class",
                    isDefault=False,
                )
            ],
        },
        qgis={},
    )
    data = document(GEOMETRIES[0])
    data["features"][0]["properties"]["aino_class"] = "server-class"
    ids = import_result(project, manifest, write(tmp_path, data), reference=reference())
    captured = capture_layers(
        project, ids, str(tmp_path / "capture"), reference=reference()
    )[0]
    props = json.loads(Path(captured["local_path"]).read_text())["features"][0][
        "properties"
    ]
    assert props["aino_class"] == "server-class"
    assert len({item["name"] for item in captured["field_schema"]}) == len(
        captured["field_schema"]
    )
    provenance = json.loads(project.mapLayer(ids[0]).customProperty(PROVENANCE_KEY))
    assert ["server-class"] in provenance["class_mapping"]["classes"].values()
    project.clear()


@pytest.mark.parametrize("name", ["Name", "data"])
def test_explicit_null_edit_overrides_original_missing_property(
    qgis_app, tmp_path, name
):
    project = QgsProject()
    data = document(GEOMETRIES[0])
    del data["features"][0]["properties"][name]
    ids = import_result(project, result(), write(tmp_path, data), reference=reference())
    layer = project.mapLayer(ids[0])
    fid = next(layer.getFeatures()).id()

    def props(label):
        captured = capture_layers(
            project, ids, str(tmp_path / label), reference=reference()
        )[0]
        return json.loads(Path(captured["local_path"]).read_text())["features"][0][
            "properties"
        ]

    assert name not in props("before")
    assert layer.startEditing()
    assert layer.changeAttributeValue(fid, logical_index(layer, name), None)
    assert name in props("edited") and props("edited2")[name] is None
    layer.undoStack().undo()
    assert name not in props("undone")
    layer.undoStack().redo()
    assert name in props("redone")
    assert layer.rollBack()
    assert name not in props("rollback")
    assert layer.startEditing()
    assert layer.changeAttributeValue(fid, logical_index(layer, name), None)
    assert layer.commitChanges()
    assert name in props("committed")
    assert project.write(str(tmp_path / "presence.qgz"))
    project.clear()
    assert project.read(str(tmp_path / "presence.qgz"))
    assert name in props("reopened")
    project.clear()


def test_native_ui_text_only_preserves_report_and_repeats_without_requests(
    qgis_app, tmp_path
):
    from types import SimpleNamespace
    from qgis.PyQt import sip
    from qgis.gui import QgsMapCanvas
    from src.main_tab import OsmParserDialogMain
    from src.project_sync.recovery import RecoveryJournal
    from src.session import Session
    from tests.test_analysis_controller import Api
    from tests.test_api_client import token

    project = QgsProject.instance()
    project.clear()
    session = Session()
    session.set_access_token(token())
    api = Api(session)
    canvas = QgsMapCanvas()
    dialog = OsmParserDialogMain(
        osm_parser=SimpleNamespace(
            session=session,
            api_client=api,
            recovery_journal=RecoveryJournal(tmp_path),
            iface=SimpleNamespace(mapCanvas=lambda: canvas),
        )
    )
    controller = dialog.analysis_controller
    controller._scope()
    manifest = dict(task_id=str(uuid4()), layers=[], report="Full narrative retained")
    controller.state = dict(
        results=manifest,
        task_id=manifest["task_id"],
        start_intent={"project_id": 17},
        latest={"status": "completed", "project_id": 17},
    )
    controller._save()
    dialog._analysis_results(manifest)
    dialog._analysis_results(manifest)
    assert dialog.analysis_report.toPlainText() == "Full narrative retained"
    assert dialog.analysis_stage.text() == "Analysis completed — text result"
    assert not dialog.results_retry.isEnabled() and not api.calls
    dialog.result_controller.close()
    controller.close()
    dialog.sync_controller.close()
    sip.delete(dialog)
    sip.delete(canvas)
    sip.delete(session)
    project.clear()


@pytest.mark.parametrize("geometry", [GEOMETRIES[0], GEOMETRIES[2], GEOMETRIES[4]])
def test_imported_appearance_round_trip_matches_supported_style(
    qgis_app, tmp_path, geometry
):
    project = QgsProject()
    manifest = result()
    ids = import_result(
        project, manifest, write(tmp_path, document(geometry)), reference=reference()
    )
    captured = capture_layers(
        project, ids, str(tmp_path / "capture"), reference=reference()
    )[0]
    appearance = captured["style"]["style"]
    expected = manifest["style"]["style"]
    for key in ["stroke", "lineWidth", "strokeOpacity"]:
        assert appearance[key] == expected[key]
    if geometry["type"] != "LineString":
        assert appearance["color"] == expected["color"]
        assert appearance["opacity"] == pytest.approx(expected["opacity"], abs=1 / 255)
    if geometry["type"] == "Point":
        assert appearance["pointSize"] == pytest.approx(expected["pointSize"])
    project.clear()


def test_managed_capture_applies_current_layer_crs(qgis_app, tmp_path):
    from qgis.core import QgsCoordinateReferenceSystem

    project = QgsProject()
    ids = import_result(
        project,
        result(),
        write(tmp_path, document(GEOMETRIES[0])),
        reference=reference(),
    )
    layer = project.mapLayer(ids[0])
    layer.setCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
    captured = capture_layers(
        project, ids, str(tmp_path / "capture"), reference=reference()
    )[0]
    geometry = json.loads(Path(captured["local_path"]).read_text())["features"][0][
        "geometry"
    ]
    assert geometry["coordinates"][0] == pytest.approx(0.00000898315284)
    project.clear()


def test_download_disk_failure_is_reported(
    client, local_api, wait_for, tmp_path, monkeypatch
):
    from src.results import download
    from tests.test_api_client import token

    session, api = client
    session.set_access_token(token())
    downloader = download.ResultDownloader(api)
    replies = []
    manifest = dict(task_id=str(uuid4()), layers=[result()])
    downloader.download_results(
        manifest["task_id"], manifest, str(tmp_path), lambda *args: replies.append(args)
    )
    wait_for(lambda: len(local_api.requests) == 1)

    def denied(*args, **kwargs):
        raise OSError("fixture disk full")

    monkeypatch.setattr(download.tempfile, "mkstemp", denied)
    from src.api_client import ApiResponse

    downloader._signed(
        ApiResponse(200, {"url": local_api.origin + "/signed"}), downloader.generation
    )
    assert replies[0][0] is None and "disk full" in replies[0][1]
    assert len(local_api.requests) == 1
    downloader.close()
    downloader.deleteLater()


def test_final_import_scope_check_prevents_layer_add(qgis_app, tmp_path, wait_for):
    from qgis.PyQt import sip
    from src.results.importer import ImportJob
    from src.results.storage import build_storage

    project = QgsProject()
    manifest = result()
    storage = build_storage(write(tmp_path, document(GEOMETRIES[0])), manifest)
    checked = []
    finished = []

    def invalid():
        checked.append(True)
        return False

    job = ImportJob(project, manifest, storage, reference(), valid=invalid)
    job.finished.connect(finished.append)
    job.start()
    wait_for(lambda: checked)
    assert not project.mapLayers() and not finished
    job.cancel()
    sip.delete(job)
    sip.delete(project)


def test_unobserved_empty_string_category_has_proven_mapping(qgis_app, tmp_path):
    project = QgsProject()
    manifest = result()
    base = manifest["style"]["style"]
    manifest["style"] = dict(
        dataset_type="groups",
        style={
            **base,
            "groups": [
                dict(
                    base,
                    column="name",
                    values=[""],
                    name="Empty string",
                    isDefault=False,
                ),
                dict(base, column="name", values=[], name="Other", isDefault=True),
            ],
        },
        qgis={},
    )
    ids = import_result(
        project,
        manifest,
        write(tmp_path, document(GEOMETRIES[0])),
        reference=reference(),
    )
    provenance = json.loads(project.mapLayer(ids[0]).customProperty(PROVENANCE_KEY))
    assert [""] in provenance["class_mapping"]["classes"].values()
    project.clear()


def test_added_attribute_is_captured_as_new_snapshot(qgis_app, tmp_path):
    from qgis.PyQt.QtCore import QMetaType
    from qgis.core import QgsField
    from src.project_sync.controller import _output_references

    project = QgsProject()
    manifest = result()
    ids = import_result(
        project,
        manifest,
        write(tmp_path, document(GEOMETRIES[0])),
        reference=reference(),
    )
    layer = project.mapLayer(ids[0])
    provenance = json.loads(layer.customProperty(PROVENANCE_KEY))
    assert layer.startEditing()
    assert layer.addAttribute(QgsField("review_note", QMetaType.Type.QString))
    assert layer.changeAttributeValue(
        next(layer.getFeatures()).id(),
        layer.fields().indexFromName("review_note"),
        "local review",
    )
    assert layer.commitChanges()
    captured = capture_layers(
        project, ids, str(tmp_path / "added"), reference=reference()
    )[0]
    captured["output_provenance"] = provenance
    assert captured["content_hash"] != provenance["local_content_hash"]
    assert not _output_references(
        [captured], "account", manifest["client_project_id"], 17
    )
    assert (
        json.loads(Path(captured["local_path"]).read_text())["features"][0][
            "properties"
        ]["review_note"]
        == "local review"
    )
    assert project.write(str(tmp_path / "schema.qgz"))
    project.clear()
    assert project.read(str(tmp_path / "schema.qgz"))
    restored = capture_layers(
        project, ids, str(tmp_path / "restored"), reference=reference()
    )[0]
    assert restored["content_hash"] == captured["content_hash"]
    project.clear()


@pytest.mark.parametrize(
    "close_fails,unlink_fails", [(True, False), (True, True), (False, True)]
)
def test_flush_then_close_failure_notifies_once_and_clears_cleanup(
    client, tmp_path, monkeypatch, close_fails, unlink_fails
):
    from qgis.PyQt.QtNetwork import QNetworkReply
    from src.results.download import ResultDownloader

    _, api = client
    downloader = ResultDownloader(api)
    temporary = tmp_path / "incomplete.partial"
    temporary.write_bytes(b"partial")
    replies = []

    class FailedBuffer:
        closes = 0

        def write(self, data):
            return len(data)

        def flush(self):
            raise OSError("original flush ENOSPC")

        def close(self):
            self.closes += 1
            if close_fails:
                raise OSError("secondary close ENOSPC")

    class FinishedReply:
        def readAll(self):
            return b""

        def attribute(self, key):
            return 200

        def rawHeader(self, name):
            return b"7"

        def error(self):
            return QNetworkReply.NetworkError.NoError

        def deleteLater(self):
            pass

    buffer = FailedBuffer()
    unlink = Path.unlink
    calls = []

    def cleanup(path, *args, **kwargs):
        if path == temporary:
            calls.append(path)
            if unlink_fails:
                raise OSError("secondary unlink EACCES")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", cleanup)
    downloader.active = True
    downloader.callback = lambda *args: replies.append(args)
    downloader.file = buffer
    downloader.temporary = str(temporary)
    downloader.reply = FinishedReply()
    downloader.received = 7
    downloader._downloaded(downloader.generation)
    assert replies == [(None, "original flush ENOSPC")]
    assert downloader.file is None and downloader.temporary is None
    assert buffer.closes == 1 and len(calls) == 1
    assert not downloader.active and not list(tmp_path.glob("*.geojson"))
    downloader.abort()
    assert len(replies) == 1 and buffer.closes == 1 and len(calls) == 1
    downloader.close()
    downloader.deleteLater()


def test_codec_rename_delete_undo_rollback_commit_reopen(qgis_app, tmp_path):
    from src.project_sync.controller import _output_references

    project = QgsProject()
    manifest = result()
    original = document(GEOMETRIES[0])
    ids = import_result(
        project, manifest, write(tmp_path, original), reference=reference()
    )
    layer = project.mapLayer(ids[0])
    provenance = json.loads(layer.customProperty(PROVENANCE_KEY))
    payload_index = logical_index(layer, "data")
    time_index = logical_index(layer, "time")

    def snapshot(label):
        return capture_layers(
            project, ids, str(tmp_path / label), reference=reference()
        )[0]

    assert layer.startEditing()
    layer.beginEditCommand("Rename JSON and remove time")
    assert layer.renameAttribute(payload_index, "review_payload")
    assert layer.deleteAttribute(time_index)
    layer.endEditCommand()
    edited = snapshot("pending")
    expected_schema = [
        dict(item, name="review_payload") if item["name"] == "data" else item
        for item in manifest["field_schema"]
        if item["name"] != "time"
    ]
    assert edited["field_schema"] == expected_schema
    props = json.loads(Path(edited["local_path"]).read_text())["features"][0][
        "properties"
    ]
    assert props["review_payload"] == original["features"][0]["properties"]["data"]
    assert "data" not in props and "time" not in props
    layer.undoStack().undo()
    assert snapshot("undone")["content_hash"] == provenance["local_content_hash"]
    layer.undoStack().redo()
    assert snapshot("redone")["content_hash"] == edited["content_hash"]
    assert layer.rollBack()
    assert snapshot("rollback")["content_hash"] == provenance["local_content_hash"]
    assert layer.startEditing()
    assert layer.renameAttribute(logical_index(layer, "data"), "review_payload")
    assert layer.deleteAttribute(logical_index(layer, "time"))
    assert layer.commitChanges()
    committed = snapshot("committed")
    assert committed["field_schema"] == expected_schema
    assert committed["content_hash"] == edited["content_hash"]
    committed["output_provenance"] = provenance
    assert not _output_references(
        [committed], "account", manifest["client_project_id"], 17
    )
    assert project.write(str(tmp_path / "renamed.qgz"))
    project.clear()
    assert project.read(str(tmp_path / "renamed.qgz"))
    reopened = snapshot("reopened")
    assert reopened["content_hash"] == committed["content_hash"]
    assert reopened["field_schema"] == expected_schema
    project.clear()


def test_renamed_missing_codec_explicit_null_keeps_presence(qgis_app, tmp_path):
    project = QgsProject()
    original = document(GEOMETRIES[0])
    del original["features"][0]["properties"]["data"]
    ids = import_result(
        project, result(), write(tmp_path, original), reference=reference()
    )
    layer = project.mapLayer(ids[0])
    assert layer.startEditing()
    assert layer.renameAttribute(logical_index(layer, "data"), "payload")
    before = capture_layers(
        project, ids, str(tmp_path / "renamed"), reference=reference()
    )[0]
    assert (
        "payload"
        not in json.loads(Path(before["local_path"]).read_text())["features"][0][
            "properties"
        ]
    )
    assert layer.changeAttributeValue(
        next(layer.getFeatures()).id(), layer.fields().indexFromName("payload"), None
    )
    assert layer.commitChanges()
    assert project.write(str(tmp_path / "presence.qgz"))
    project.clear()
    assert project.read(str(tmp_path / "presence.qgz"))
    captured = capture_layers(
        project, ids, str(tmp_path / "after"), reference=reference()
    )[0]
    props = json.loads(Path(captured["local_path"]).read_text())["features"][0][
        "properties"
    ]
    assert props["payload"] is None and "data" not in props
    project.clear()


def test_empty_output_added_schema_changes_hash_and_reference(qgis_app, tmp_path):
    from qgis.PyQt.QtCore import QMetaType
    from qgis.core import QgsField
    from src.project_sync.controller import _output_references

    project = QgsProject()
    manifest = result()
    manifest["field_schema"] = []
    ids = import_result(
        project,
        manifest,
        write(tmp_path, dict(type="FeatureCollection", features=[])),
        reference=reference(),
    )
    layer = project.mapLayer(ids[0])
    provenance = json.loads(layer.customProperty(PROVENANCE_KEY))
    assert layer.startEditing()
    assert layer.addAttribute(QgsField("review_note", QMetaType.Type.QString))
    assert layer.commitChanges()
    assert project.write(str(tmp_path / "empty-schema.qgz"))
    project.clear()
    assert project.read(str(tmp_path / "empty-schema.qgz"))
    captured = capture_layers(
        project, ids, str(tmp_path / "added"), reference=reference()
    )[0]
    assert captured["feature_count"] == 0
    assert captured["field_schema"][0]["name"] == "review_note"
    assert captured["content_hash"] != provenance["local_content_hash"]
    captured["output_provenance"] = provenance
    assert not _output_references(
        [captured], "account", manifest["client_project_id"], 17
    )
    project.clear()


@pytest.mark.parametrize(
    "bad_field", ["logical_collision", "duplicate_marker", "mismatched_marker"]
)
def test_managed_schema_ambiguity_is_explicit(qgis_app, tmp_path, bad_field):
    from qgis.PyQt.QtCore import QMetaType
    from qgis.core import QgsField

    project = QgsProject()
    ids = import_result(
        project,
        result(),
        write(tmp_path, document(GEOMETRIES[0])),
        reference=reference(),
    )
    layer = project.mapLayer(ids[0])
    original = layer.fields()[logical_index(layer, "data")]
    added = QgsField(
        "Name" if bad_field == "logical_collision" else "duplicate",
        QMetaType.Type.QString,
    )
    if bad_field == "duplicate_marker":
        added.setComment(original.comment())
    if bad_field == "mismatched_marker":
        marker = json.loads(original.comment())
        marker["field"]["codec"] = "invalid-codec"
        added.setComment(json.dumps(marker))
    assert layer.startEditing()
    assert layer.addAttribute(added)
    assert layer.commitChanges()
    pattern = {
        "logical_collision": "Ambiguous managed logical field name",
        "duplicate_marker": "Duplicate managed field lineage",
        "mismatched_marker": "Invalid managed field lineage",
    }[bad_field]
    with pytest.raises(ValueError, match=pattern):
        capture_layers(project, ids, str(tmp_path / "invalid"), reference=reference())
    assert layer.fields().indexFromName(added.name()) >= 0
    project.clear()


def test_unmarked_legacy_mapping_safe_until_codec_rename(qgis_app, tmp_path, wait_for):
    from osgeo import ogr
    from qgis.PyQt import sip
    from qgis.PyQt.QtCore import QMetaType
    from qgis.core import QgsField
    from src.results.importer import ImportJob
    from src.results.storage import build_storage

    project = QgsProject()
    manifest = result()
    storage = build_storage(write(tmp_path, document(GEOMETRIES[0])), manifest)
    for part in storage:
        part["mapping"].pop("field_lineage")
        with ogr.ExceptionMgr():
            dataset = ogr.Open(part["path"], 1)
            native = dataset.GetLayer(0)
            for index in range(len(part["mapping"]["fields"])):
                original = native.GetLayerDefn().GetFieldDefn(index)
                definition = ogr.FieldDefn(original.GetName(), original.GetType())
                definition.SetComment("")
                assert (
                    native.AlterFieldDefn(index, definition, ogr.ALTER_COMMENT_FLAG)
                    == 0
                )
            native = None
            dataset = None
    finished = []
    job = ImportJob(project, manifest, storage, reference())
    job.finished.connect(finished.append)
    job.start()
    wait_for(lambda: finished)
    ids = finished[0]
    layer = project.mapLayer(ids[0])
    initial = capture_layers(
        project, ids, str(tmp_path / "initial"), reference=reference()
    )[0]
    assert initial["field_schema"] == manifest["field_schema"]
    assert layer.startEditing()
    assert layer.addAttribute(QgsField("review_note", QMetaType.Type.QString))
    assert layer.commitChanges()
    added = capture_layers(
        project, ids, str(tmp_path / "added"), reference=reference()
    )[0]
    assert added["field_schema"][-1]["name"] == "review_note"
    assert layer.startEditing()
    assert layer.renameAttribute(logical_index(layer, "data"), "legacy_payload")
    assert layer.commitChanges()
    with pytest.raises(ValueError, match="Unmarked legacy managed fields"):
        capture_layers(project, ids, str(tmp_path / "ambiguous"), reference=reference())
    sip.delete(job)
    project.clear()
