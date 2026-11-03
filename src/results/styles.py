from qgis.PyQt.QtGui import QColor
from qgis.core import (
    Qgis,
    QgsCategorizedSymbolRenderer,
    QgsFillSymbol,
    QgsGraduatedSymbolRenderer,
    QgsLineSymbol,
    QgsMarkerSymbol,
    QgsRendererCategory,
    QgsRendererRange,
    QgsSingleSymbolRenderer,
)

from src.project_sync.managed import encode


def _color(value, opacity):
    color = QColor(value)
    if not color.isValid() or not 0 <= opacity <= 1:
        raise ValueError("Invalid output style color or opacity")
    color.setAlphaF(opacity)
    return color


def _symbol(layer, style, reference, grouped):
    fill = _color(style.get("color", "#808080"), style.get("opacity", 1))
    stroke = _color(style.get("stroke", "#808080"), style.get("strokeOpacity", 1))
    width = max(0, float(style.get("lineWidth", 1)))
    if layer.geometryType() == Qgis.GeometryType.Point:
        symbol = QgsMarkerSymbol.createSimple({"name": "circle"})
        part = symbol.symbolLayer(0)
        t = min(1, max(0, (reference.zoom - 4) / 2))
        radius = ((1 - t) * 24 + t * 10.125) if grouped else ((1 - t) * 5 + t * 7)
        part.setSize(max(0, float(style.get("pointSize", 1))) * 2 * radius + width)
        part.setSizeUnit(Qgis.RenderUnit.Pixels)
    elif layer.geometryType() == Qgis.GeometryType.Line:
        symbol = QgsLineSymbol.createSimple({})
        part = symbol.symbolLayer(0)
        part.setColor(stroke)
        part.setWidth(width)
        part.setWidthUnit(Qgis.RenderUnit.Pixels)
        return symbol
    else:
        symbol = QgsFillSymbol.createSimple({})
        part = symbol.symbolLayer(0)
    part.setColor(fill)
    part.setStrokeColor(stroke)
    part.setStrokeWidth(width)
    part.setStrokeWidthUnit(Qgis.RenderUnit.Pixels)
    return symbol


def apply_style(layer, envelope, mapping, reference, *, prefer_graduated=True):
    kind = envelope.get("dataset_type")
    if kind not in {"default", "groups"} or not isinstance(envelope.get("style"), dict):
        raise ValueError(
            "Result style requires the authoritative default/groups envelope"
        )
    style = envelope["style"]
    if kind == "default":
        layer.setRenderer(
            QgsSingleSymbolRenderer(_symbol(layer, style, reference, False))
        )
        return None
    groups = style.get("groups", [])
    columns = {group.get("column") for group in groups}
    entries = {entry["schema"]["name"]: entry for entry in mapping["fields"]}
    if len(columns) != 1 or next(iter(columns)) not in entries:
        raise ValueError(
            "Output classification does not identify one exact schema field"
        )
    field = next(iter(columns))
    metadata = envelope.get("qgis", {}).get("classification", {})
    definitions = metadata.get("definitions", [])
    source = metadata.get("source_field")
    if (
        prefer_graduated
        and metadata.get("kind") == "graduated"
        and source in entries
        and entries[source]["codec"] == "native"
        and entries[source]["schema"]["type"] in {"number", "integer"}
    ):
        by_value = {
            value: group
            for group in groups
            for value in group.get("values", [])
            if isinstance(value, str)
        }
        if definitions and all(
            definition["id"] in by_value for definition in definitions
        ):
            ranges = []
            for definition in definitions:
                group = by_value[definition["id"]]
                ranges.append(
                    QgsRendererRange(
                        definition["lower"],
                        definition["upper"],
                        _symbol(layer, group, reference, True),
                        group.get("name", ""),
                        definition.get("enabled", True),
                    )
                )
            layer.setRenderer(
                QgsGraduatedSymbolRenderer(entries[source]["physical"], ranges)
            )
            return field
    categories = []
    for group in groups:
        values = group.get("values", [])
        if group.get("isDefault"):
            values = [""]
        for value in values:
            categories.append(
                QgsRendererCategory(
                    "" if group.get("isDefault") else encode(value, entries[field]),
                    _symbol(layer, group, reference, True),
                    group.get("name", ""),
                )
            )
    layer.setRenderer(
        QgsCategorizedSymbolRenderer(entries[field]["physical"], categories)
    )
    return field
