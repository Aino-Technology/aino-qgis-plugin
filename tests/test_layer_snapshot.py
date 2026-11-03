import hashlib
import json

import pytest
from qgis.PyQt.QtCore import QDate, QMetaType, QTimer
from qgis.core import QgsFeature, QgsField, QgsGeometry, QgsProject, QgsVectorLayer

from src.project_sync.layers import SnapshotCapture, capture_layers
from tests.test_style_conversion import reference


def layer_with_values(name="duplicate"):
    layer = QgsVectorLayer("MultiPoint?crs=EPSG:3857", name, "memory")
    layer.dataProvider().addAttributes(
        [
            QgsField("Name", QMetaType.Type.QString),
            QgsField("name", QMetaType.Type.QString),
            QgsField("day", QMetaType.Type.QDate),
            QgsField("big", QMetaType.Type.LongLong),
            QgsField("nested", QMetaType.Type.QVariantMap),
            QgsField("empty", QMetaType.Type.Double),
        ]
    )
    layer.updateFields()
    feature = QgsFeature(layer.fields())
    feature.setAttributes(
        [
            "Москва",
            "low",
            QDate(2026, 11, 2),
            9007199254740993,
            {"a": [1, "1", None]},
            None,
        ]
    )
    feature.setGeometry(QgsGeometry.fromWkt("MultiPoint ((0 0),(1000 1000))"))
    layer.dataProvider().addFeatures([feature])
    return layer


def test_snapshot_preserves_typed_fields_edits_ids_and_exact_hash(qgis_app, tmp_path):
    project = QgsProject()
    one, two = layer_with_values(), layer_with_values()
    project.addMapLayers([one, two])
    one.startEditing()
    one.changeAttributeValue(next(one.getFeatures()).id(), 0, "правка")
    one.removeSelection()
    outputs = capture_layers(
        project, [one.id(), two.id()], str(tmp_path), reference=reference()
    )
    assert len(outputs) == 2
    for output in outputs:
        raw = open(output["local_path"], "rb").read()
        assert output["content_hash"] == hashlib.sha256(raw).hexdigest()
        document = json.loads(raw)
        assert document["field_schema"] == output["field_schema"]
        assert len(document["features"]) == 1
        props = document["features"][0]["properties"]
        assert props["Name"] == (
            "правка" if output["client_layer_id"] == one.id() else "Москва"
        )
        assert props["name"] == "low"
        assert props["day"] == "2026-11-02"
        assert props["big"] == 9007199254740993
        assert props["nested"] == {"a": [1, "1", None]}
        assert props["empty"] is None
        assert document["features"][0]["geometry"]["type"] == "MultiPoint"
        assert document["features"][0]["geometry"]["coordinates"][1][0] < 0.01
    assert outputs[0]["local_path"] != outputs[1]["local_path"]
    project.clear()


def test_empty_schema_changes_content_hash(qgis_app, tmp_path):
    project = QgsProject()
    layer = QgsVectorLayer("Point?crs=EPSG:4326&field=empty:double", "empty", "memory")
    project.addMapLayer(layer)
    before = capture_layers(
        project, [layer.id()], str(tmp_path), reference=reference()
    )[0]
    assert before["field_schema"][0]["type"] == "number"
    layer.startEditing()
    layer.addAttribute(QgsField("new", QMetaType.Type.QString))
    after = capture_layers(project, [layer.id()], str(tmp_path), reference=reference())[
        0
    ]
    assert before["content_hash"] != after["content_hash"]
    project.clear()


def test_invalid_source_fails_without_partial_files(qgis_app, tmp_path):
    project = QgsProject()
    layer = QgsVectorLayer("/nonexistent/aino-layer.gpkg", "missing", "ogr")
    project.addMapLayer(layer)
    with pytest.raises(ValueError):
        capture_layers(project, [layer.id()], str(tmp_path), reference=reference())
    assert not list(tmp_path.iterdir())
    project.clear()


def test_incremental_snapshot_freezes_edits_and_yields(qgis_app, tmp_path, wait_for):
    project = QgsProject()
    layer = layer_with_values()
    project.addMapLayer(layer)
    layer.startEditing()
    job = SnapshotCapture(
        project, [layer.id()], str(tmp_path), reference=reference(), batch_size=1
    )
    layer.changeAttributeValue(next(layer.getFeatures()).id(), 0, "later")
    completed, ticks = [], []
    job.finished.connect(completed.append)
    QTimer.singleShot(0, lambda: ticks.append(True))
    job.start()
    wait_for(lambda: bool(completed))
    assert ticks
    data = json.load(open(completed[0][0]["local_path"]))
    assert data["features"][0]["properties"]["Name"] == "Москва"
    project.clear()


def test_subset_edit_buffer_and_selection_export_all_visible_features(
    qgis_app, tmp_path
):
    from qgis.core import QgsCoordinateTransformContext, QgsVectorFileWriter

    source = QgsVectorLayer("Point?crs=EPSG:4326&field=n:integer", "subset", "memory")
    features = []
    for value in [1, 2, 3]:
        feature = QgsFeature(source.fields())
        feature.setAttributes([value])
        feature.setGeometry(QgsGeometry.fromWkt(f"Point ({value} 0)"))
        features.append(feature)
    source.dataProvider().addFeatures(features)
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.layerName = "capture"
    path = str(tmp_path / "source.gpkg")
    result = QgsVectorFileWriter.writeAsVectorFormatV3(
        source, path, QgsCoordinateTransformContext(), options
    )
    assert result[0] == QgsVectorFileWriter.WriterError.NoError
    layer = QgsVectorLayer(path + "|layername=capture", "subset", "ogr")
    layer.setSubsetString('"n" >= 2')
    rows = {feature["n"]: feature.id() for feature in layer.getFeatures()}
    layer.startEditing()
    layer.changeAttributeValue(rows[2], layer.fields().indexFromName("n"), 0)
    layer.deleteFeature(rows[3])
    layer.addAttribute(QgsField("buffer_only", QMetaType.Type.QString))
    feature = QgsFeature(layer.fields())
    feature.setAttributes([None, 4, "added"])
    feature.setGeometry(QgsGeometry.fromWkt("Point (4 0)"))
    layer.addFeature(feature)
    layer.selectByIds([rows[2]])
    project = QgsProject()
    project.addMapLayer(layer)
    output = capture_layers(
        project, [layer.id()], str(tmp_path / "out"), reference=reference()
    )[0]
    data = json.load(open(output["local_path"]))
    assert sorted(f["properties"]["n"] for f in data["features"]) == [0, 4]
    assert any(field["name"] == "buffer_only" for field in output["field_schema"])
    project.clear()


def test_classification_color_edit_changes_only_style_hash(qgis_app, tmp_path):
    from qgis.core import QgsCategorizedSymbolRenderer, QgsRendererCategory
    from tests.test_style_conversion import marker

    project = QgsProject()
    layer = layer_with_values()
    layer.setRenderer(
        QgsCategorizedSymbolRenderer(
            "Name", [QgsRendererCategory("Москва", marker("red"), "Moscow")]
        )
    )
    project.addMapLayer(layer)
    before = capture_layers(
        project, [layer.id()], str(tmp_path), reference=reference()
    )[0]
    layer.renderer().updateCategorySymbol(0, marker("blue"))
    after = capture_layers(project, [layer.id()], str(tmp_path), reference=reference())[
        0
    ]
    assert before["content_hash"] == after["content_hash"]
    assert before["style_hash"] != after["style_hash"]
    project.clear()


def test_large_capture_yields_and_cancel_removes_partial_files(
    qgis_app, tmp_path, wait_for
):
    project = QgsProject()
    layer = layer_with_values()
    prototype = next(layer.getFeatures())
    layer.dataProvider().addFeatures([QgsFeature(prototype) for _ in range(2000)])
    project.addMapLayer(layer)
    job = SnapshotCapture(
        project, [layer.id()], str(tmp_path), reference=reference(), batch_size=16
    )
    counts, completed = [], []
    job.progress.connect(counts.append)
    job.finished.connect(completed.append)
    job.start()
    wait_for(lambda: bool(counts))
    assert 0 < counts[0] <= 16
    job.cancel()
    qgis_app.processEvents()
    assert not completed
    assert not list(tmp_path.iterdir())
    project.clear()


def test_unavailable_local_postgis(qgis_app, tmp_path):
    layer = QgsVectorLayer(
        "host=127.0.0.1 port=59999 dbname=aino_unavailable connect_timeout=1 table=public.absent (geom)",
        "unavailable",
        "postgres",
    )
    assert not layer.isValid()
    project = QgsProject()
    project.addMapLayer(layer)
    with pytest.raises(ValueError):
        capture_layers(project, [layer.id()], str(tmp_path), reference=reference())
    assert not list(tmp_path.iterdir())
    project.clear()


def test_prepare_ui_ids_and_context_cancellation(qgis_app, tmp_path, wait_for):
    from types import SimpleNamespace
    from qgis.PyQt.QtCore import Qt
    from qgis.core import QgsCoordinateReferenceSystem, QgsRectangle
    from qgis.gui import QgsMapCanvas
    from src.main_tab import OsmParserDialogMain
    from src.session import Session

    project = QgsProject.instance()
    one, two = layer_with_values(), layer_with_values()
    project.addMapLayers([one, two])
    canvas = QgsMapCanvas()
    canvas.resize(800, 600)
    canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
    canvas.setExtent(QgsRectangle(0, 0, 2000, 2000))
    session = Session()
    dialog = OsmParserDialogMain(
        osm_parser=SimpleNamespace(
            session=session,
            api_client=SimpleNamespace(session=session),
            iface=SimpleNamespace(mapCanvas=lambda: canvas),
        )
    )
    assert dialog.input_layers.count() == 2
    for index in range(2):
        dialog.input_layers.item(index).setCheckState(Qt.CheckState.Checked)
    assert set(dialog.selected_input_ids()) == {one.id(), two.id()}
    job = dialog.prepare_inputs(str(tmp_path))
    assert job is not None, dialog.capture_status.text()
    wait_for(lambda: dialog.prepared_inputs is not None or not job.active)
    assert dialog.prepared_inputs is not None, dialog.capture_status.text()
    assert len(dialog.prepared_inputs[1]) == 2
    one.setName("renamed")
    assert dialog.prepared_inputs is None
    assert not list(tmp_path.iterdir())
    job = dialog.prepare_inputs(str(tmp_path))
    dialog._account_changed("a", "b")
    qgis_app.processEvents()
    assert not job.active
    assert dialog.prepared_inputs is None
    assert not list(tmp_path.iterdir())
    dialog.close()
    dialog.deleteLater()
    canvas.deleteLater()
    project.clear()


@pytest.mark.parametrize("change", ["renderer", "center"])
@pytest.mark.parametrize("stage", ["completed", "in_flight"])
def test_prepare_invalidates_renderer_and_center_changes(
    qgis_app, tmp_path, wait_for, change, stage
):
    from types import SimpleNamespace

    from qgis.PyQt.QtCore import QCoreApplication, QEvent, Qt
    from qgis.core import (
        QgsCoordinateReferenceSystem,
        QgsMarkerSymbol,
        QgsPointXY,
        QgsRectangle,
        QgsSingleSymbolRenderer,
    )
    from qgis.gui import QgsMapCanvas

    from src.main_tab import OsmParserDialogMain
    from src.session import Session

    project = QgsProject.instance()
    layer = layer_with_values()
    project.addMapLayer(layer)
    canvas = QgsMapCanvas()
    canvas.resize(800, 600)
    canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
    canvas.setExtent(QgsRectangle(0, 0, 2000, 2000))
    session = Session()
    dialog = OsmParserDialogMain(
        osm_parser=SimpleNamespace(
            session=session,
            api_client=SimpleNamespace(session=session),
            iface=SimpleNamespace(mapCanvas=lambda: canvas),
        )
    )
    completed = []
    dialog.inputs_prepared.connect(
        lambda area, outputs: completed.append((area, outputs))
    )
    try:
        dialog.input_layers.item(0).setCheckState(Qt.CheckState.Checked)
        if change == "center":
            dialog.area_mode.setCurrentIndex(2)
            dialog._pick_center()
            dialog._point_tool.canvasClicked.emit(
                QgsPointXY(500, 500), Qt.MouseButton.LeftButton
            )
            dialog._pick_center()
            assert dialog._point_tool is not None
        job = dialog.prepare_inputs(str(tmp_path))
        assert job is not None, dialog.capture_status.text()
        if stage == "completed":
            wait_for(lambda: bool(completed))
            assert dialog.prepared_inputs is not None
            assert len(list(tmp_path.iterdir())) == 2
        else:
            job.timer.stop()
            job.batch_size = 1
            job._tick()
            job.timer.stop()
            assert job.active
            assert list(tmp_path.glob("*.partial"))
        expected_emissions = len(completed)
        if change == "renderer":
            layer.setRenderer(
                QgsSingleSymbolRenderer(
                    QgsMarkerSymbol.createSimple({"name": "circle", "color": "blue"})
                )
            )
        else:
            dialog._point_tool.canvasClicked.emit(
                QgsPointXY(1500, 1500), Qt.MouseButton.LeftButton
            )
            assert dialog._center[0] == (1500, 1500)
            assert dialog._point_tool is None
        assert dialog.prepared_inputs is None
        assert not job.active
        assert not list(tmp_path.iterdir())
        job.finished.emit([])
        qgis_app.processEvents()
        assert dialog.prepared_inputs is None
        assert len(completed) == expected_emissions
        assert not list(tmp_path.iterdir())
    finally:
        dialog.close()
        dialog.deleteLater()
        canvas.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        project.clear()
