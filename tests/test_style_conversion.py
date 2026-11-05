import pytest
from qgis.PyQt.QtCore import QSize
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    Qgis,
    QgsCategorizedSymbolRenderer,
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsGraduatedSymbolRenderer,
    QgsMapSettings,
    QgsMarkerSymbol,
    QgsRectangle,
    QgsRendererCategory,
    QgsRendererRange,
    QgsVectorLayer,
)

from src.project_sync.styles import RenderReference, RendererCapture, convert_renderer


def reference():
    settings = QgsMapSettings()
    settings.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
    settings.setExtent(QgsRectangle(0, 0, 2000, 2000))
    settings.setOutputSize(QSize(1000, 1000))
    settings.setOutputDpi(120)
    return RenderReference(settings, zoom=5)


def marker(color):
    return QgsMarkerSymbol.createSimple(
        {"name": "circle", "color": color, "size": "2", "outline_width": "0"}
    )


def test_alpha_units_and_point_multiplier(qgis_app):
    layer = QgsVectorLayer("Point?crs=EPSG:3857", "points", "memory")
    symbol = marker("255,0,0,128")
    symbol.setOpacity(0.5)
    layer.setOpacity(0.4)
    layer.renderer().setSymbol(symbol)
    style, metadata, warnings = convert_renderer(
        layer, reference().scale, reference=reference()
    )
    assert not warnings
    assert style["dataset_type"] == "default"
    assert style["style"]["color"] == "#ff0000"
    assert style["style"]["opacity"] == pytest.approx(128 / 255 * 0.5 * 0.4)
    assert style["style"]["pointSize"] == pytest.approx((2 * 120 / 25.4) / 12)


def test_typed_categories_use_actual_symbol_and_stable_ids(qgis_app):
    layer = QgsVectorLayer(
        "Point?crs=EPSG:4326&field=cat:string&field=AINO_CLASS:string",
        "points",
        "memory",
    )
    categories = [
        QgsRendererCategory(v, marker(c), label)
        for v, c, label in [
            (None, "red", "Null"),
            (1, "blue", "Number"),
            ("1", "green", "String"),
            ("", "gray", "Other"),
        ]
    ]
    layer.setRenderer(QgsCategorizedSymbolRenderer("cat", categories))
    capture = RendererCapture(layer, reference())
    assert capture.metadata["field"] != "aino_class"
    ids = []
    for value in [None, 1, "1", 2]:
        feature = QgsFeature(layer.fields())
        feature.setAttributes([value, None])
        ids.append(capture.classify(feature))
    assert ids[1] == ids[2]
    assert ids[0] == ids[3]
    groups = capture.style["style"]["groups"]
    assert next(g for g in groups if ids[1] in g["values"])["color"] == "#008000"
    assert next(g for g in groups if ids[0] in g["values"])["color"] == "#808080"
    assert any(g["isDefault"] and not g["values"] for g in groups)
    assert [d["value"]["type"] for d in capture.metadata["definitions"]] == [
        "null",
        "integer",
        "string",
        "string",
    ]
    capture.close()
    layer.renderer().updateCategorySymbol(2, marker("yellow"))
    again = RendererCapture(layer, reference())
    assert [d["id"] for d in again.metadata["definitions"]] == [
        d["id"] for d in capture.metadata["definitions"]
    ]
    again.close()


def test_graduated_native_boundaries_and_no_symbol(qgis_app):
    layer = QgsVectorLayer("Point?crs=EPSG:4326&field=n:double", "points", "memory")
    layer.setRenderer(
        QgsGraduatedSymbolRenderer(
            "n",
            [
                QgsRendererRange(0, 10, marker("red"), "low"),
                QgsRendererRange(10, 20, marker("blue"), "high"),
            ],
        )
    )
    capture = RendererCapture(layer, reference())
    ids = []
    for value in [None, -1, 0, 10, 10.000001, 20, 21, "bad"]:
        feature = QgsFeature(layer.fields())
        feature.setAttributes([value])
        ids.append(capture.classify(feature))
    assert ids[0] == ids[1] == ids[6] == "unrendered"
    assert ids[2] == ids[3] == ids[7]
    assert ids[4] == ids[5] != ids[3]
    hidden = next(
        g for g in capture.style["style"]["groups"] if "unrendered" in g["values"]
    )
    assert hidden["opacity"] == hidden["strokeOpacity"] == 0
    assert not capture.warnings
    capture.close()


@pytest.mark.parametrize("zoom", [4, 5, 6])
def test_actual_512_pixel_world_zoom_calibration(qgis_app, zoom):
    settings = reference().settings
    resolution = 40075016.68557849 / (512 * 2**zoom)
    settings.setExtent(
        QgsRectangle(
            -500 * resolution, -500 * resolution, 500 * resolution, 500 * resolution
        )
    )
    captured = RenderReference(settings)
    assert captured.zoom == pytest.approx(zoom)


@pytest.mark.parametrize(
    "unit,value,expected",
    [
        (Qgis.RenderUnit.Millimeters, 2, 120 / 25.4),
        (Qgis.RenderUnit.Points, 6, 5),
        (Qgis.RenderUnit.Pixels, 10, 5),
        (Qgis.RenderUnit.MapUnits, 10, 2.5),
    ],
)
def test_units_apply_reference_scale_exactly_once(qgis_app, unit, value, expected):
    from qgis.core import QgsLineSymbol

    layer = QgsVectorLayer("LineString?crs=EPSG:3857", "line", "memory")
    symbol = QgsLineSymbol.createSimple({"line_color": "red"})
    symbol.setWidth(value)
    symbol.setOutputUnit(unit)
    layer.renderer().setSymbol(symbol)
    ref = reference()
    layer.renderer().setReferenceScale(ref.scale / 2)
    style, _, warnings = convert_renderer(layer, ref.scale, reference=ref)
    assert not warnings
    assert style["style"]["lineWidth"] == pytest.approx(expected)


def test_group_point_size_outline_and_disabled_category(qgis_app):
    layer = QgsVectorLayer("Point?crs=EPSG:4326&field=cat:string", "points", "memory")
    symbol = marker("red")
    symbol.symbolLayer(0).setStrokeWidth(0.2)
    layer.setRenderer(
        QgsCategorizedSymbolRenderer(
            "cat", [QgsRendererCategory("a", symbol, "A", False)]
        )
    )
    capture = RendererCapture(layer, reference())
    group = capture.style["style"]["groups"][1]
    assert group["pointSize"] == pytest.approx(1.8 * 120 / 25.4 / (2 * 17.0625))
    assert group["opacity"] == group["strokeOpacity"] == 0
    feature = QgsFeature(layer.fields())
    feature.setAttributes(["a"])
    assert capture.classify(feature) == "unrendered"
    capture.close()


def test_unsupported_rules_warn_before_capture(qgis_app):
    from qgis.core import QgsRuleBasedRenderer

    layer = QgsVectorLayer("Point?crs=EPSG:4326", "points", "memory")
    layer.setRenderer(QgsRuleBasedRenderer(marker("red")))
    style, _, warnings = convert_renderer(
        layer, reference().scale, reference=reference()
    )
    assert style["dataset_type"] == "default"
    assert any("Rules" in warning for warning in warnings)


def test_native_render_alpha_and_marker_outer_diameter(qgis_app, tmp_path):
    from qgis.core import QgsGeometry, QgsMapRendererParallelJob

    layer = QgsVectorLayer("Point?crs=EPSG:3857", "native", "memory")
    symbol = marker("255,0,0,128")
    symbol.setOpacity(0.5)
    symbol.symbolLayer(0).setStrokeWidth(0.2)
    layer.setOpacity(0.4)
    layer.renderer().setSymbol(symbol)
    feature = QgsFeature()
    feature.setGeometry(QgsGeometry.fromWkt("Point (1000 1000)"))
    layer.dataProvider().addFeatures([feature])
    ref = reference()
    settings = ref.settings
    settings.setLayers([layer])
    settings.setBackgroundColor(QColor(0, 0, 0, 0))
    render = QgsMapRendererParallelJob(settings)
    render.start()
    render.waitForFinished()
    image = render.renderedImage()
    assert not render.errors()
    image.save(str(tmp_path / "qgis-native-marker.png"))
    assert image.pixelColor(500, 500).alphaF() == pytest.approx(
        128 / 255 * 0.5 * 0.4, abs=2 / 255
    )
    style, _, warnings = convert_renderer(layer, ref.scale, reference=ref)
    assert not warnings
    aino_outer = 12 * style["style"]["pointSize"] + 2 * style["style"]["lineWidth"]
    pixels = [x for x in range(480, 520) if image.pixelColor(x, 500).alpha() > 3]
    assert max(pixels) - min(pixels) + 1 == pytest.approx(aino_outer, abs=2)
