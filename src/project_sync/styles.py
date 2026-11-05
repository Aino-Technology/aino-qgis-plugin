import hashlib
import json
import math

from qgis.PyQt import sip
from qgis.PyQt.QtCore import Qt
from qgis.core import (
    QgsCategorizedSymbolRenderer,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGraduatedSymbolRenderer,
    QgsMapSettings,
    QgsPointXY,
    QgsRenderContext,
    QgsSimpleFillSymbolLayer,
    QgsSimpleLineSymbolLayer,
    QgsSimpleMarkerSymbolLayer,
    QgsSingleSymbolRenderer,
    QgsVariantUtils,
)

from .area import require_gui_thread


def canonical_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def typed_value(value):
    if QgsVariantUtils.isNull(value):
        return {"type": "null", "value": None}
    kind = {
        bool: "boolean",
        int: "integer",
        float: "number",
        str: "string",
        list: "json",
    }.get(type(value))
    if kind is None:
        return {
            "type": type(value).__name__,
            "value": value.toString(Qt.DateFormat.ISODateWithMs)
            if hasattr(value, "toString")
            else str(value),
        }
    return {"type": kind, "value": value}


class RenderReference:
    """A copied canvas render context and Aino reference zoom in CSS pixels."""

    def __init__(self, map_settings, zoom=None):
        require_gui_thread()
        self.settings = QgsMapSettings(map_settings)
        self.scale = self.settings.scale()
        if not self.settings.hasValidSettings():
            raise ValueError("A valid map view is required to capture style sizes")
        if zoom is None:
            center = self.settings.extent().center()
            resolution = self.settings.mapUnitsPerPixel()
            transform = QgsCoordinateTransform(
                self.settings.destinationCrs(),
                QgsCoordinateReferenceSystem("EPSG:3857"),
                self.settings.transformContext(),
            )
            left = transform.transform(
                QgsPointXY(center.x() - resolution / 2, center.y())
            )
            right = transform.transform(
                QgsPointXY(center.x() + resolution / 2, center.y())
            )
            metres_per_pixel = math.hypot(right.x() - left.x(), right.y() - left.y())
            if not math.isfinite(metres_per_pixel) or metres_per_pixel <= 0:
                raise ValueError("Cannot calibrate this map view to Aino zoom")
            zoom = math.log2(40075016.68557849 / (512 * metres_per_pixel))
        if not math.isfinite(zoom):
            raise ValueError("Reference zoom must be finite")
        self.zoom = zoom

    def context(self, renderer):
        context = QgsRenderContext.fromMapSettings(self.settings)
        context.setSymbologyReferenceScale(renderer.referenceScale())
        return context

    def metadata(self):
        return {
            "scale": self.scale,
            "dpi": self.settings.outputDpi(),
            "map_units_per_pixel": self.settings.mapUnitsPerPixel(),
            "zoom": self.zoom,
            "crs": self.settings.destinationCrs().authid(),
        }


def _class_id(definition):
    return "c_" + hashlib.sha256(canonical_bytes(definition)).hexdigest()[:24]


def _free_field(fields, reserved=()):
    names = {
        name.lower().upper()
        for name in [*(field.name() for field in fields), *reserved]
    }
    name, suffix = "aino_class", 0
    while name.lower().upper() in names:
        suffix += 1
        name = f"aino_class_{suffix}"
    return name


def _appearance(symbol, opacity, context, zoom, grouped, warnings):
    appearance = {
        "color": "#808080",
        "opacity": 0.0,
        "stroke": "#808080",
        "strokeOpacity": 0.0,
        "lineWidth": 0.0,
        "pointSize": 0.0,
        "pattern": None,
        "extrusion": False,
    }
    if symbol is None:
        return appearance
    if symbol.symbolLayerCount() != 1 or symbol.hasDataDefinedProperties():
        warnings.append(
            "Composite or data-defined symbols are simplified to their first symbol layer"
        )
    part = symbol.symbolLayer(0)
    alpha = opacity * symbol.opacity()
    fill, stroke, width, diameter = part.color(), part.color(), 0.0, None
    if isinstance(part, QgsSimpleMarkerSymbolLayer):
        if part.shape() != QgsSimpleMarkerSymbolLayer.Shape.Circle:
            warnings.append("Non-circular markers are simplified to circles")
        fill, stroke = part.color(), part.strokeColor()
        width = context.convertToPainterUnits(
            part.strokeWidth(), part.strokeWidthUnit(), part.strokeWidthMapUnitScale()
        )
        diameter = context.convertToPainterUnits(
            part.size(), part.sizeUnit(), part.sizeMapUnitScale()
        )
        if part.strokeStyle() == Qt.PenStyle.NoPen:
            width = 0.0
        elif part.strokeStyle() != Qt.PenStyle.SolidLine:
            warnings.append("Dashed marker outlines are simplified to solid outlines")
    elif isinstance(part, QgsSimpleLineSymbolLayer):
        stroke = part.color()
        fill = None
        width = context.convertToPainterUnits(
            part.width(), part.widthUnit(), part.widthMapUnitScale()
        )
        if part.penStyle() == Qt.PenStyle.NoPen:
            width = 0.0
        elif part.penStyle() != Qt.PenStyle.SolidLine:
            warnings.append("Dashed lines are simplified to solid lines")
    elif isinstance(part, QgsSimpleFillSymbolLayer):
        fill, stroke = part.color(), part.strokeColor()
        width = context.convertToPainterUnits(
            part.strokeWidth(), part.strokeWidthUnit(), part.strokeWidthMapUnitScale()
        )
        if part.brushStyle() == Qt.BrushStyle.NoBrush:
            fill = None
        elif part.brushStyle() != Qt.BrushStyle.SolidPattern:
            warnings.append("Pattern fills are simplified to solid fills")
        if part.strokeStyle() == Qt.PenStyle.NoPen:
            width = 0.0
        elif part.strokeStyle() != Qt.PenStyle.SolidLine:
            warnings.append("Dashed polygon outlines are simplified to solid outlines")
    else:
        warnings.append(
            "SVG or unsupported symbol layers are simplified to a solid symbol"
        )
        fill, stroke = symbol.color(), symbol.color()
        if hasattr(symbol, "size"):
            diameter = context.convertToPainterUnits(
                symbol.size(), symbol.sizeUnit(), symbol.sizeMapUnitScale()
            )
    if fill is not None:
        appearance.update(color=fill.name(), opacity=fill.alphaF() * alpha)
    appearance.update(
        stroke=stroke.name(),
        strokeOpacity=stroke.alphaF() * alpha if width else 0.0,
        lineWidth=width,
    )
    if diameter is not None:
        t = min(1.0, max(0.0, (zoom - 4) / 2))
        radius = ((1 - t) * 24 + t * 10.125) if grouped else ((1 - t) * 5 + t * 7)
        appearance["pointSize"] = max(0.0, diameter - width) / (2 * radius)
    return appearance


class RendererCapture:
    """Cloned renderer with native feature membership and stable transport classes."""

    def __init__(self, layer, reference, reserved_fields=()):
        require_gui_thread()
        self.renderer = layer.renderer().clone()
        self.context = reference.context(self.renderer)
        self.fields = layer.fields()
        self.warnings = []
        self._symbols = {}
        self._started = False
        self.metadata = {
            "kind": "single",
            "reference": reference.metadata(),
            "definitions": [],
        }
        classified = isinstance(
            self.renderer, (QgsCategorizedSymbolRenderer, QgsGraduatedSymbolRenderer)
        )
        if classified and self.fields.indexFromName(self.renderer.classAttribute()) < 0:
            self.warnings.append(
                "Expression classification is simplified to a single symbol"
            )
            classified = False
        if not isinstance(
            self.renderer,
            (
                QgsSingleSymbolRenderer,
                QgsCategorizedSymbolRenderer,
                QgsGraduatedSymbolRenderer,
            ),
        ):
            self.warnings.append(
                "Rules or unsupported renderers are simplified to a single symbol"
            )
        if classified:
            kind = (
                "categorized"
                if isinstance(self.renderer, QgsCategorizedSymbolRenderer)
                else "graduated"
            )
            field = _free_field(self.fields, reserved_fields)
            self.metadata.update(
                kind=kind, field=field, source_field=self.renderer.classAttribute()
            )
            values = (
                self.renderer.categories()
                if kind == "categorized"
                else self.renderer.ranges()
            )
            groups = []
            for index, item in enumerate(values):
                definition = {"index": index}
                if kind == "categorized":
                    definition["value"] = typed_value(item.value())
                else:
                    definition.update(lower=item.lowerValue(), upper=item.upperValue())
                class_id = _class_id(
                    {
                        "kind": kind,
                        "source_field": self.renderer.classAttribute(),
                        **definition,
                    }
                )
                self.metadata["definitions"].append(
                    {
                        **definition,
                        "id": class_id,
                        "label": item.label(),
                        "enabled": item.renderState(),
                    }
                )
                owned_symbol = item.symbol().clone()
                if kind == "categorized":
                    self.renderer.updateCategorySymbol(index, owned_symbol)
                else:
                    self.renderer.updateRangeSymbol(index, owned_symbol)
                self._symbols[sip.unwrapinstance(owned_symbol)] = class_id
                appearance = _appearance(
                    item.symbol(),
                    layer.opacity(),
                    self.context,
                    reference.zoom,
                    True,
                    self.warnings,
                )
                if not item.renderState():
                    appearance.update(opacity=0.0, strokeOpacity=0.0)
                groups.append(
                    {
                        **appearance,
                        "isDefault": False,
                        "column": field,
                        "name": item.label(),
                        "values": [class_id],
                    }
                )
            hidden = _appearance(
                None, 1, self.context, reference.zoom, True, self.warnings
            )
            groups.append(
                {
                    **hidden,
                    "isDefault": False,
                    "column": field,
                    "name": "Not rendered",
                    "values": ["unrendered"],
                }
            )
            groups.insert(
                0,
                {
                    **hidden,
                    "isDefault": True,
                    "column": field,
                    "name": "Other",
                    "values": [],
                },
            )
            self.style = {
                "dataset_type": "groups",
                "style": {**hidden, "groups": groups},
            }
            self.renderer.startRender(self.context, self.fields)
            self._started = True
        else:
            symbols = self.renderer.symbols(self.context)
            symbol = symbols[0] if symbols else None
            self.style = {
                "dataset_type": "default",
                "style": _appearance(
                    symbol,
                    layer.opacity(),
                    self.context,
                    reference.zoom,
                    False,
                    self.warnings,
                ),
            }
        self.warnings = list(dict.fromkeys(self.warnings))
        self.style["qgis"] = {
            "classification": self.metadata,
            "warnings": self.warnings,
        }

    def classify(self, feature):
        if not self._started:
            return None
        self.context.expressionContext().setFeature(feature)
        symbol = self.renderer.symbolForFeature(feature, self.context)
        if symbol is None:
            return "unrendered"
        key = sip.unwrapinstance(symbol)
        if key not in self._symbols:
            raise ValueError("Renderer returned an unexpected dynamic symbol")
        return self._symbols[key]

    def close(self):
        if self._started:
            self.renderer.stopRender(self.context)
            self._started = False


def convert_renderer(
    layer, reference_scale: float, *, reference=None
) -> tuple[dict, dict, list[str]]:
    """Convert appearance using a real RenderReference, never an assumed DPI."""
    if reference is None:
        raise ValueError(
            "Style capture requires RenderReference from the current canvas"
        )
    if not math.isclose(reference_scale, reference.scale, rel_tol=1e-9):
        raise ValueError("Reference scale must match the captured map settings")
    capture = RendererCapture(layer, reference)
    try:
        return capture.style, capture.metadata, capture.warnings
    finally:
        capture.close()
