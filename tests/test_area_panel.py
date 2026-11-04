import copy

import pytest
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
)
from qgis.gui import QgsMapCanvas, QgsMapToolPan
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QEvent, Qt
from qgis.PyQt.QtGui import QKeyEvent


def panel(qgis_app):
    from src.main_tab.area_panel import AreaPanel

    canvas = QgsMapCanvas()
    canvas.resize(800, 600)
    canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
    canvas.setExtent(QgsRectangle(0, 0, 2000, 2000))
    return AreaPanel(canvas, QgsProject.instance()), canvas


def test_extent_is_explicit_and_frozen_across_pan_and_crs(qgis_app):
    widget, canvas = panel(qgis_app)
    try:
        assert widget.selection() is None
        widget.mode.setCurrentIndex(1)
        with pytest.raises(ValueError, match="Use current view"):
            widget.selection()
        widget.use_view.click()
        accepted = widget.selection()
        canvas.setExtent(QgsRectangle(10000, 10000, 20000, 20000))
        canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
        assert widget.selection() == accepted
        returned = widget.selection()
        returned["bbox"][0] = 999
        assert widget.selection() == accepted
    finally:
        widget.close()
        sip.delete(widget)
        sip.delete(canvas)


def test_polygon_layer_is_independent_of_input_selection(qgis_app):
    widget, canvas = panel(qgis_app)
    project = QgsProject.instance()
    layer = QgsVectorLayer("MultiPolygon?crs=EPSG:4326", "Boundary", "memory")
    feature = QgsFeature()
    feature.setGeometry(
        QgsGeometry.fromWkt(
            "MultiPolygon (((0 0,1 0,1 1,0 1,0 0),(0.2 0.2,0.2 0.4,0.4 0.4,0.4 0.2,0.2 0.2)),((2 0,3 0,3 1,2 1,2 0)))"
        )
    )
    layer.dataProvider().addFeatures([feature])
    project.addMapLayer(layer)
    layer.selectAll()
    try:
        widget.mode.setCurrentIndex(2)
        assert widget.selected_input_ids() == []
        area = widget.selection()
        assert area["geometry"]["type"] == "MultiPolygon"
        assert sum(map(len, area["geometry"]["coordinates"])) == 3
        assert "1" in widget.polygon_count.text()
        widget.inputs.item(0).setCheckState(Qt.CheckState.Checked)
        assert widget.selection() == area
    finally:
        widget.close()
        sip.delete(widget)
        project.clear()
        sip.delete(canvas)


def test_escape_crs_change_and_hide_preserve_confirmed_radius(qgis_app):
    widget, canvas = panel(qgis_app)
    pan = QgsMapToolPan(canvas)
    canvas.setMapTool(pan)
    try:
        widget.mode.setCurrentIndex(3)
        widget.pick_point()
        widget.point_tool.canvasClicked.emit(
            QgsPointXY(1000, 1000), Qt.MouseButton.LeftButton
        )
        accepted = copy.deepcopy(widget.selection())
        assert accepted["radius_m"] == 1000
        assert canvas.mapTool() == pan
        widget.pick_point()
        widget.point_tool.keyPressEvent(
            QKeyEvent(
                QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier
            )
        )
        assert widget.point_tool is None and canvas.mapTool() == pan
        assert widget.selection() == accepted
        widget.pick_point()
        canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:32633"))
        assert widget.point_tool is None
        assert widget.selection() == accepted
        assert "cancelled" in widget.message.text().lower()
        widget.set_preview_visible(False)
        assert widget.selection() == accepted
        widget.set_preview_visible(True)
        assert widget.preview.band.isVisible()
    finally:
        widget.close()
        sip.delete(widget)
        sip.delete(canvas)


def test_area_revision_fetch_once_and_context_fenced(qgis_app):
    from types import SimpleNamespace

    from src.api_client import ApiResponse
    from src.project_sync.recovery import project_identity

    widget, canvas = panel(qgis_app)
    identity = project_identity(QgsProject.instance())
    calls = []
    api = SimpleNamespace(
        session=SimpleNamespace(account_id="a"),
        send=lambda method, path, payload, callback: calls.append((path, callback))
        or SimpleNamespace(abort=lambda: None),
    )
    state = {"task_id": "task-a", "latest": {"area_revision": 1}}
    area = {
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]],
        }
    }
    try:
        widget.update_analysis(api, state, identity)
        widget.update_analysis(api, state, identity)
        assert len(calls) == 1
        state["task_id"] = "task-b"
        widget.update_analysis(api, state, identity)
        calls[0][1](
            ApiResponse(200, {"task_id": "task-a", "revision": 1, "area": area})
        )
        assert widget.resolved is None
        calls[1][1](
            ApiResponse(200, {"task_id": "task-b", "revision": 1, "area": area})
        )
        assert widget.resolved == area
        widget.update_analysis(api, state, identity)
        assert len(calls) == 2
        state["latest"]["area_revision"] = 2
        widget.update_analysis(api, state, identity)
        api.session.account_id = "other"
        calls[2][1](
            ApiResponse(200, {"task_id": "task-b", "revision": 2, "area": area})
        )
        assert widget.resolved is None
    finally:
        widget.close()
        sip.delete(widget)
        sip.delete(canvas)


def test_empty_project_zooms_only_first_import(qgis_app):
    project = QgsProject.instance()
    project.clear()
    widget, canvas = panel(qgis_app)
    layer = QgsVectorLayer("Polygon?crs=EPSG:4326", "Result", "memory")
    feature = QgsFeature()
    feature.setGeometry(
        QgsGeometry.fromWkt("Polygon ((13 52,14 52,14 53,13 53,13 52))")
    )
    layer.dataProvider().addFeatures([feature])
    layer.updateExtents()
    project.addMapLayer(layer)
    try:
        widget.results_imported([layer.id()])
        assert widget.zoomed_result
        canvas.setExtent(QgsRectangle(0, 0, 100, 100))
        extent = QgsRectangle(canvas.extent())
        widget.results_imported([layer.id()])
        assert canvas.extent() == extent
    finally:
        widget.close()
        sip.delete(widget)
        project.clear()
        sip.delete(canvas)


def test_radius_edit_after_crs_change_retains_center(qgis_app):
    widget, canvas = panel(qgis_app)
    try:
        widget.mode.setCurrentIndex(3)
        widget.pick_point()
        widget.point_tool.canvasClicked.emit(
            QgsPointXY(1000, 1000), Qt.MouseButton.LeftButton
        )
        center = widget.selection()["center"]
        canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:32633"))
        widget.radius.setValue(2000)
        assert widget.selection()["center"] == pytest.approx(center)
        assert widget.selection()["radius_m"] == 2000
    finally:
        widget.close()
        sip.delete(widget)
        sip.delete(canvas)


def test_resolved_area_label_and_provider_coverage_remain_visible(qgis_app):
    widget, canvas = panel(qgis_app)
    try:
        widget.set_resolved_area(
            {
                "label": "Berlin",
                "boundary_source": "viewport",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[13, 52], [14, 52], [14, 53], [13, 52]]],
                },
            }
        )
        assert "Berlin" in widget.message.text()
        assert "coverage" in widget.message.text()
        assert widget.selection() is None
        widget.set_resolved_area(
            {
                "label": "Center",
                "radius_m": 500,
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[13, 52], [14, 52], [14, 53], [13, 52]]],
                },
            }
        )
        assert "500 m" in widget.message.text()
    finally:
        widget.close()
        sip.delete(widget)
        sip.delete(canvas)
