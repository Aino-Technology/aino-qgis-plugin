from types import SimpleNamespace

import pytest
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransformContext,
    QgsDistanceArea,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsRectangle,
    QgsVectorLayer,
)

from src.project_sync.area import capture_area


def canvas(crs="EPSG:4326", extent=None):
    settings = SimpleNamespace(transformContext=lambda: QgsCoordinateTransformContext())
    return SimpleNamespace(
        mapSettings=lambda: settings,
        mapRenderer=lambda: None,
        extent=lambda: extent or QgsRectangle(9, 69, 11, 71),
        mapSettingsCrs=crs,
        destinationCrs=lambda: QgsCoordinateReferenceSystem(crs),
    )


def test_extent_densifies_curved_projection(qgis_app):
    view = canvas("EPSG:3413", QgsRectangle(-2000000, -2000000, 2000000, -500000))
    result = capture_area("extent", view, [])
    assert result["bbox"][3] == pytest.approx(85.38675, abs=0.0001)
    assert len(result["geometry"]["coordinates"][0]) > 100


def test_polygon_preserves_parts_holes_and_multiple_selection(qgis_app):
    layer = QgsVectorLayer("MultiPolygon?crs=EPSG:3857", "areas", "memory")
    features = []
    for wkt in [
        "MultiPolygon (((0 0,1000 0,1000 1000,0 1000,0 0),(200 200,200 400,400 400,400 200,200 200)),((2000 0,3000 0,3000 1000,2000 1000,2000 0)))",
        "MultiPolygon (((4000 0,5000 0,5000 1000,4000 1000,4000 0)))",
    ]:
        feature = QgsFeature()
        feature.setGeometry(QgsGeometry.fromWkt(wkt))
        features.append(feature)
    layer.dataProvider().addFeatures(features)
    layer.selectAll()
    result = capture_area("polygon", canvas(), [layer])
    assert result["geometry"]["type"] == "MultiPolygon"
    assert len(result["geometry"]["coordinates"]) == 3
    assert sum(len(part) for part in result["geometry"]["coordinates"]) == 4
    assert result["bbox"][2] < 0.05


@pytest.mark.parametrize("radius", [0, -1, float("nan"), float("inf"), None])
def test_invalid_radius(qgis_app, radius):
    with pytest.raises(ValueError):
        capture_area("radius", canvas(), [], center=(10, 70), radius_m=radius)


def test_empty_selection(qgis_app):
    with pytest.raises(ValueError, match="select|Select"):
        capture_area("polygon", canvas(), [])


def test_geodesic_radius_and_dateline(qgis_app):
    distance = QgsDistanceArea()
    distance.setSourceCrs(
        QgsCoordinateReferenceSystem("EPSG:4326"), QgsCoordinateTransformContext()
    )
    distance.setEllipsoid("WGS84")
    area = capture_area("radius", canvas(), [], center=(10, 70), radius_m=1000)
    for lon, lat in area["geometry"]["coordinates"][0][:-1]:
        assert distance.measureLine(
            QgsPointXY(10, 70), QgsPointXY(lon, lat)
        ) == pytest.approx(1000, abs=0.001)
    crossing = capture_area("radius", canvas(), [], center=(179.999, 70), radius_m=1000)
    assert crossing["geometry"]["type"] == "MultiPolygon"
    assert crossing["bbox"][0] >= -180 and crossing["bbox"][2] <= 180
    for part in crossing["geometry"]["coordinates"]:
        assert max(p[0] for p in part[0]) - min(p[0] for p in part[0]) < 1


def test_invalid_polygon_rejected(qgis_app):
    layer = QgsVectorLayer("Polygon?crs=EPSG:4326", "invalid", "memory")
    feature = QgsFeature()
    feature.setGeometry(QgsGeometry.fromWkt("Polygon ((0 0,2 2,2 0,0 2,0 0))"))
    layer.dataProvider().addFeatures([feature])
    layer.selectAll()
    with pytest.raises(ValueError, match="valid"):
        capture_area("polygon", canvas(), [layer])


def test_wide_geographic_polygon_does_not_become_dateline_sliver(qgis_app):
    layer = QgsVectorLayer("Polygon?crs=EPSG:4326", "wide", "memory")
    feature = QgsFeature()
    feature.setGeometry(
        QgsGeometry.fromWkt("Polygon ((-170 -10,170 -10,170 10,-170 10,-170 -10))")
    )
    layer.dataProvider().addFeatures([feature])
    layer.selectAll()
    result = capture_area("polygon", canvas(), [layer])
    assert result["bbox"] == pytest.approx([-170, -10, 170, 10])
    ring = result["geometry"]["coordinates"][0]
    assert max(abs(a[0] - b[0]) for a, b in zip(ring, ring[1:])) < 180


def test_wide_polygon_retains_hole_near_opposite_edge(qgis_app):
    layer = QgsVectorLayer("Polygon?crs=EPSG:4326", "wide-hole", "memory")
    feature = QgsFeature()
    feature.setGeometry(
        QgsGeometry.fromWkt(
            "Polygon ((-170 -20,170 -20,170 20,-170 20,-170 -20),(140 -5,140 5,150 5,150 -5,140 -5))"
        )
    )
    layer.dataProvider().addFeatures([feature])
    layer.selectAll()
    result = capture_area("polygon", canvas(), [layer])
    assert result["geometry"]["type"] == "Polygon"
    assert len(result["geometry"]["coordinates"]) == 2
    hole = result["geometry"]["coordinates"][1]
    assert min(p[0] for p in hole) == 140
    assert max(p[0] for p in hole) == 150


@pytest.mark.parametrize("latitude", [89, 90, -89, -90])
def test_geodesic_polar_caps(qgis_app, latitude):
    import json
    from qgis.core import QgsJsonUtils

    result = capture_area(
        "radius", canvas(), [], center=(10, latitude), radius_m=200000
    )
    feature_json = {"type": "Feature", "properties": {}, "geometry": result["geometry"]}
    geometry = QgsJsonUtils.stringToFeatureList(json.dumps(feature_json))[0].geometry()
    assert geometry.isGeosValid()
    assert result["bbox"][0] >= -180 and result["bbox"][2] <= 180
    assert result["bbox"][1] >= -90 and result["bbox"][3] <= 90
    parts = (
        result["geometry"]["coordinates"]
        if result["geometry"]["type"] == "MultiPolygon"
        else [result["geometry"]["coordinates"]]
    )
    assert all(len(part) == 1 for part in parts)
    distance = QgsDistanceArea()
    distance.setSourceCrs(
        QgsCoordinateReferenceSystem("EPSG:4326"), QgsCoordinateTransformContext()
    )
    distance.setEllipsoid("WGS84")
    sign = 1 if latitude > 0 else -1
    for lon in [-179, -120, -60, 0, 60, 120, 179]:
        for lat in [86, 87, 88, 89, 89.5, 89.9]:
            point = QgsPointXY(lon, lat * sign)
            expected = distance.measureLine(QgsPointXY(10, latitude), point) < 200000
            assert geometry.intersects(QgsGeometry.fromPointXY(point)) == expected, (
                latitude,
                lon,
                lat,
            )
    assert geometry.intersects(QgsGeometry.fromPointXY(QgsPointXY(10, 90 * sign)))


def test_globe_spanning_radius_rejected(qgis_app):
    with pytest.raises(ValueError, match="10,000,000"):
        capture_area("radius", canvas(), [], center=(0, 0), radius_m=10000001)


@pytest.mark.parametrize("crs", ["EPSG:4326", "EPSG:3857", "EPSG:32633"])
def test_radius_wgs84_center_survives_canvas_crs(qgis_app, crs):
    area = capture_area(
        "radius",
        canvas(crs),
        [],
        center=(13.4, 52.5),
        radius_m=1000,
        center_crs=QgsCoordinateReferenceSystem("EPSG:4326"),
    )
    assert area["center"] == pytest.approx([13.4, 52.5])


def test_invalid_crs_rejected_before_capture(qgis_app):
    with pytest.raises(ValueError, match="CRS"):
        capture_area("extent", canvas("invalid"), [])
