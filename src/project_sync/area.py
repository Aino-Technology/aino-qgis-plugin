import json
import math

from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsDistanceArea,
    QgsGeometry,
    QgsPointXY,
    QgsRectangle,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QThread

WGS84 = "EPSG:4326"
TRANSFORM_TOLERANCE_DEGREES = 0.000001
MAX_RADIUS_METRES = 10000000


def require_gui_thread():
    if QThread.currentThread() != QgsApplication.instance().thread():
        raise RuntimeError("Capture must run on the QGIS main thread")


def _transformed_ring(ring, transform):
    def point(value):
        result = transform.transform(QgsPointXY(value))
        if not math.isfinite(result.x()) or not math.isfinite(result.y()):
            raise ValueError("Area cannot be transformed to WGS84")
        return result

    def segment(a, b, pa, pb, depth=0):
        middle = QgsPointXY((a.x() + b.x()) / 2, (a.y() + b.y()) / 2)
        pm = point(middle)
        longitude = pb.x() + 360 * round((pa.x() - pb.x()) / 360)
        expected = (pa.x() + longitude) / 2
        actual = pm.x() + 360 * round((expected - pm.x()) / 360)
        error = max(abs(actual - expected), abs(pm.y() - (pa.y() + pb.y()) / 2))
        if error <= TRANSFORM_TOLERANCE_DEGREES:
            return [pa]
        if depth >= 20:
            raise ValueError("Area boundary cannot be transformed within tolerance")
        return segment(a, middle, pa, pm, depth + 1) + segment(
            middle, b, pm, pb, depth + 1
        )

    result = []
    for a, b in zip(ring, ring[1:]):
        result.extend(segment(a, b, point(a), point(b)))
    result.append(point(ring[-1]))
    return result


def _transform_polygon(geometry, source_crs, context):
    if (
        geometry.isEmpty()
        or geometry.type() != QgsWkbTypes.GeometryType.PolygonGeometry
        or not geometry.isGeosValid()
    ):
        raise ValueError("Select valid Polygon or MultiPolygon geometry")
    if not source_crs.isValid():
        raise ValueError("Area layer CRS is unknown; assign a valid CRS first")
    transform = QgsCoordinateTransform(
        source_crs, QgsCoordinateReferenceSystem(WGS84), context
    )
    geometry = geometry.densifyByCount(31)
    polygons = (
        geometry.asMultiPolygon() if geometry.isMultipart() else [geometry.asPolygon()]
    )
    return QgsGeometry.fromMultiPolygonXY(
        [
            [_transformed_ring(ring, transform) for ring in polygon]
            for polygon in polygons
        ]
    )


def _split_dateline(geometry, pole=None):
    polygons = (
        geometry.asMultiPolygon() if geometry.isMultipart() else [geometry.asPolygon()]
    )
    parts = []
    for polygon in polygons:
        rings = []
        anchor = polygon[0][0].x()
        for ring in polygon:
            previous = anchor
            unwrapped = []
            for point in ring:
                lon = point.x() + 360 * round((previous - point.x()) / 360)
                unwrapped.append(QgsPointXY(lon, point.y()))
                previous = lon
            if abs(unwrapped[-1].x() - unwrapped[0].x()) > 0.001:
                if pole is None:
                    raise ValueError("Area crosses a pole without a defined interior")
                unwrapped.extend(
                    [
                        QgsPointXY(unwrapped[-1].x(), pole),
                        QgsPointXY(unwrapped[0].x(), pole),
                        unwrapped[0],
                    ]
                )
            rings.append(unwrapped)
            if len(rings) == 1:
                anchor = (
                    min(point.x() for point in unwrapped)
                    + max(point.x() for point in unwrapped)
                ) / 2
        unwrapped = QgsGeometry.fromPolygonXY(rings)
        bounds = unwrapped.boundingBox()
        first = math.floor((bounds.xMinimum() + 180) / 360)
        last = math.floor((bounds.xMaximum() + 180) / 360)
        for strip in range(first, last + 1):
            clipped = unwrapped.intersection(
                QgsGeometry.fromRect(
                    QgsRectangle(-180 + 360 * strip, -90, 180 + 360 * strip, 90)
                )
            )
            if clipped.isEmpty():
                continue
            clipped.translate(-360 * strip, 0)
            candidates = (
                clipped.asMultiPolygon()
                if clipped.isMultipart()
                else [clipped.asPolygon()]
            )
            parts.extend(part for part in candidates if part)
    if not parts:
        raise ValueError("Area has no polygon surface")
    result = (
        QgsGeometry.fromPolygonXY(parts[0])
        if len(parts) == 1
        else QgsGeometry.fromMultiPolygonXY(parts)
    )
    if pole is not None:
        result = QgsGeometry.unaryUnion(
            [QgsGeometry.fromPolygonXY(part) for part in parts]
        )
    if not result.isGeosValid():
        raise ValueError("Area is invalid after transformation")
    return result


def capture_area(
    mode: str, canvas, selected_layers, center=None, radius_m=None, *, center_crs=None
) -> dict:
    """Capture WGS84 area; radius center is in the canvas destination CRS."""
    require_gui_thread()
    settings = canvas.mapSettings()
    crs = (
        settings.destinationCrs()
        if hasattr(settings, "destinationCrs")
        else canvas.destinationCrs()
    )
    if not crs.isValid():
        raise ValueError(
            "Map CRS is unknown; choose a valid CRS before selecting an area"
        )
    context = settings.transformContext()
    extra = {}
    pole = None
    source_crs = crs.authid() or crs.toWkt()
    if mode == "extent":
        extent = canvas.extent()
        if extent.isEmpty() or not extent.isFinite():
            raise ValueError("Map extent must have a finite area")
        geometry = _transform_polygon(QgsGeometry.fromRect(extent), crs, context)
    elif mode == "polygon":
        pieces, sources = [], []
        for layer in selected_layers:
            if not layer.isValid():
                raise ValueError("Selected area layer is unavailable")
            for feature in layer.getSelectedFeatures():
                pieces.append(
                    _split_dateline(
                        _transform_polygon(feature.geometry(), layer.crs(), context)
                    )
                )
                sources.append(layer.crs().authid() or layer.crs().toWkt())
        if not pieces:
            raise ValueError(
                "Select one or more polygon features for the analysis area"
            )
        geometry = QgsGeometry.unaryUnion(pieces)
        source_crs = ";".join(dict.fromkeys(sources))
    elif mode == "radius":
        if radius_m is None or not math.isfinite(radius_m) or radius_m <= 0:
            raise ValueError("Radius must be a finite positive number of metres")
        if radius_m > MAX_RADIUS_METRES:
            raise ValueError(
                "Radius must not exceed 10,000,000 metres (hemisphere-scale bound)"
            )
        if (
            center is None
            or len(center) != 2
            or not all(math.isfinite(v) for v in center)
        ):
            raise ValueError("Select a finite center point on the map")
        origin_crs = center_crs if center_crs is not None else crs
        if not origin_crs.isValid():
            raise ValueError("Point CRS is unknown")
        transform = QgsCoordinateTransform(
            origin_crs, QgsCoordinateReferenceSystem(WGS84), context
        )
        origin = transform.transform(QgsPointXY(*center))
        if not (-180 <= origin.x() <= 180 and -90 <= origin.y() <= 90):
            raise ValueError("Radius center must be a valid geographic point")
        distance = QgsDistanceArea()
        distance.setSourceCrs(QgsCoordinateReferenceSystem(WGS84), context)
        distance.setEllipsoid("WGS84")
        north_distance = distance.measureLine(origin, QgsPointXY(origin.x(), 90))
        south_distance = distance.measureLine(origin, QgsPointXY(origin.x(), -90))
        if radius_m >= north_distance:
            pole = 90
        elif radius_m >= south_distance:
            pole = -90
        ring = [
            distance.computeSpheroidProject(origin, radius_m, i * math.tau / 720)
            for i in range(720)
        ]
        ring.append(ring[0])
        geometry = QgsGeometry.fromPolygonXY([ring])
        extra = {"center": [origin.x(), origin.y()], "radius_m": radius_m}
    else:
        raise ValueError("Unknown analysis area mode")
    geometry = _split_dateline(geometry, pole=pole)
    bounds = geometry.boundingBox()
    return {
        "kind": mode,
        "geometry": json.loads(geometry.asJson(12)),
        "bbox": [
            bounds.xMinimum(),
            bounds.yMinimum(),
            bounds.xMaximum(),
            bounds.yMaximum(),
        ],
        "source_crs": source_crs,
        **extra,
    }


OSM_URI = "type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png&zmin=0&zmax=19"
OSM_CREDIT = (
    '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
)


def _xyz_identity(uri):
    from qgis.core import QgsProviderRegistry
    from qgis.PyQt.QtCore import QUrl

    values = QgsProviderRegistry.instance().decodeUri("wms", uri)
    url = QUrl(values.get("url", ""))
    values["url"] = url.toLocalFile() if url.isLocalFile() else url.toString()
    values.pop("path", None)
    if values.get("crs") == "EPSG:3857":
        values.pop("crs")
    if not values.get("format") or str(values.get("format")) == "NULL":
        values.pop("format", None)
    return values


def add_osm_basemap(project, canvas, *, uri=OSM_URI):
    """Add one native XYZ layer and project-owned visible contributor credit."""
    from qgis.core import (
        Qgis,
        QgsAnnotationRectangleTextItem,
        QgsRasterLayer,
        QgsTextFormat,
    )
    from qgis.PyQt.QtCore import QSizeF
    from qgis.PyQt.QtGui import QColor

    layer = next(
        (
            layer
            for layer in project.mapLayers().values()
            if isinstance(layer, QgsRasterLayer)
            and _xyz_identity(layer.source()) == _xyz_identity(uri)
        ),
        None,
    )
    if layer is None:
        layer = QgsRasterLayer(uri, "OpenStreetMap", "wms")
        if not layer.isValid():
            raise ValueError(
                "OpenStreetMap basemap could not be created. From your request remains available."
            )
        metadata = layer.metadata()
        metadata.setRights(
            ["© OpenStreetMap contributors · https://www.openstreetmap.org/copyright"]
        )
        layer.setMetadata(metadata)
        project.addMapLayer(layer, False)
        project.layerTreeRoot().addLayer(layer)
    annotations = project.mainAnnotationLayer()
    item_id = layer.customProperty("aino/osm_credit", "")
    if not item_id or annotations.item(item_id) is None:
        item = QgsAnnotationRectangleTextItem(
            OSM_CREDIT, QgsRectangle(0.65, 0.94, 0.99, 0.99)
        )
        item.setPlacementMode(Qgis.AnnotationPlacementMode.RelativeToMapFrame)
        item.setFixedSize(QSizeF(64, 8))
        item.setFixedSizeUnit(Qgis.RenderUnit.Millimeters)
        format = QgsTextFormat()
        format.setSize(9)
        format.setColor(QColor("#20232c"))
        format.setAllowHtmlFormatting(True)
        buffer = format.buffer()
        buffer.setEnabled(True)
        buffer.setSize(1)
        buffer.setColor(QColor("white"))
        format.setBuffer(buffer)
        item.setFormat(format)
        item_id = annotations.addItem(item)
        layer.setCustomProperty("aino/osm_credit", item_id)
    if canvas is not None:
        canvas.refresh()
    return layer
