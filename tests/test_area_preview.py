import pytest
from qgis.core import QgsCoordinateReferenceSystem, QgsProject, QgsRectangle
from qgis.gui import QgsMapCanvas
from qgis.PyQt import sip


def test_overlay_reprojects_without_changing_project_layers(qgis_app):
    from src.project_sync.area import capture_area
    from src.project_sync.area_preview import AreaPreview

    canvas = QgsMapCanvas()
    canvas.resize(800, 600)
    canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
    canvas.setExtent(QgsRectangle(9, 69, 11, 71))
    before = set(QgsProject.instance().mapLayers())
    preview = AreaPreview(canvas)
    area = capture_area("radius", canvas, [], center=(10, 70), radius_m=1000)
    try:
        preview.set_area(area)
        assert preview.marker.isVisible() and preview.band.isVisible()
        canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
        assert preview.marker.center().x() == pytest.approx(1113194.9, abs=1)
        assert set(QgsProject.instance().mapLayers()) == before
        preview.set_visible(False)
        assert not preview.band.isVisible()
        preview.set_visible(True)
        assert preview.band.isVisible()
        preview.close()
        preview.close()
    finally:
        sip.delete(canvas)


def test_basemap_idempotent_bottom_and_persistent_attribution(qgis_app, tmp_path):
    from qgis.core import QgsRasterLayer, QgsVectorLayer
    from qgis.PyQt.QtGui import QColor, QImage
    from src.project_sync.area import add_osm_basemap

    project = QgsProject.instance()
    project.clear()
    canvas = QgsMapCanvas()
    canvas.resize(800, 600)
    canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
    fixture = tmp_path / "tile.png"
    image = QImage(256, 256, QImage.Format.Format_RGB32)
    image.fill(QColor("#dfe9df"))
    assert image.save(str(fixture))
    uri = "type=xyz&url=" + fixture.as_uri() + "&zmin=0&zmax=19"
    vector = QgsVectorLayer("Point?crs=EPSG:4326", "Inputs", "memory")
    project.addMapLayer(vector)
    project.writeEntry("DecorationCopyright", "/Label", "User copyright")
    try:
        layer = add_osm_basemap(project, canvas, uri=uri)
        assert isinstance(layer, QgsRasterLayer)
        assert add_osm_basemap(project, canvas, uri=uri).id() == layer.id()
        assert project.layerTreeRoot().layerOrder() == [vector, layer]
        assert project.readEntry("DecorationCopyright", "/Label")[0] == "User copyright"
        annotations = project.mainAnnotationLayer().items()
        assert len(annotations) == 1
        assert "OpenStreetMap" in next(iter(annotations.values())).text()
        assert project.write(str(tmp_path / "map.qgs"))
        project.clear()
        assert project.read(str(tmp_path / "map.qgs"))
        assert len(project.mainAnnotationLayer().items()) == 1
        add_osm_basemap(project, canvas, uri=uri)
        assert len(project.mainAnnotationLayer().items()) == 1
    finally:
        project.clear()
        sip.delete(canvas)


def test_native_xyz_identification_and_http_cache(
    qgis_app, local_api, wait_for, tmp_path
):
    from qgis.core import QgsNetworkAccessManager
    from qgis.PyQt.QtCore import QBuffer, QByteArray, QIODevice, QTimer
    from qgis.PyQt.QtGui import QColor, QImage
    from src.project_sync.area import OSM_URI, add_osm_basemap

    project = QgsProject.instance()
    project.clear()
    canvas = QgsMapCanvas()
    canvas.resize(256, 256)
    canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
    canvas.setExtent(QgsRectangle(-20037508, -20037508, 20037508, 20037508))
    image = QImage(256, 256, QImage.Format.Format_RGB32)
    image.fill(QColor("#e1e8df"))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    responded = []
    timer = QTimer()

    def respond():
        for index in range(len(responded), len(local_api.requests)):
            local_api.respond(
                index,
                raw=bytes(data),
                headers={
                    "Content-Type": "image/png",
                    "Cache-Control": "public, max-age=604800",
                },
            )
            responded.append(index)

    timer.timeout.connect(respond)
    timer.start(5)
    try:
        uri = OSM_URI.replace("https://tile.openstreetmap.org", local_api.origin)
        layer = add_osm_basemap(project, canvas, uri=uri)
        canvas.setLayers([layer])
        canvas.show()
        canvas.refresh()
        wait_for(lambda: bool(responded) and not canvas.isDrawing(), timeout=10000)
        headers = {
            key.lower(): value
            for key, value in local_api.requests[0]["headers"].items()
        }
        assert "QGIS/" in headers["user-agent"]
        assert headers.get("cache-control") != "no-cache"
        assert QgsNetworkAccessManager.instance().cache() is not None
        count = len(local_api.requests)
        layer.triggerRepaint()
        canvas.refresh()
        wait_for(lambda: not canvas.isDrawing())
        assert len(local_api.requests) == count
    finally:
        timer.stop()
        canvas.setLayers([])
        project.clear()
        sip.delete(canvas)
