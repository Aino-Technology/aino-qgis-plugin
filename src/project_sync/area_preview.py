import copy
import json

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsJsonUtils,
    QgsPointXY,
    QgsWkbTypes,
)
from qgis.gui import QgsRubberBand, QgsVertexMarker
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.PyQt.QtGui import QColor


class AreaPreview(QObject):
    """Display the accepted WGS84 boundary without adding a project layer."""

    failed = pyqtSignal(str)

    def __init__(self, canvas, parent=None):
        super().__init__(parent)
        self.canvas = canvas
        self.area = None
        self.visible = True
        self.closed = False
        self.band = QgsRubberBand(canvas, QgsWkbTypes.GeometryType.PolygonGeometry)
        self.band.setStrokeColor(QColor("#6374df"))
        self.band.setFillColor(QColor(99, 116, 223, 35))
        self.band.setWidth(2)
        self.marker = QgsVertexMarker(canvas)
        self.marker.setColor(QColor("#5264df"))
        self.marker.setFillColor(QColor("#5264df"))
        self.marker.setIconType(QgsVertexMarker.ICON_CIRCLE)
        self.marker.setIconSize(10)
        self.marker.setPenWidth(2)
        canvas.destinationCrsChanged.connect(self._render)
        self._render()

    def set_area(self, area):
        self.area = copy.deepcopy(area)
        self._render()

    def set_visible(self, visible):
        self.visible = visible
        self._render()

    def _render(self):
        if self.closed:
            return
        self.band.hide()
        self.marker.hide()
        if not self.area or not self.visible:
            return
        try:
            crs = self.canvas.mapSettings().destinationCrs()
            if not crs.isValid():
                raise ValueError(
                    "Map CRS is unknown; choose a valid CRS to display the area"
                )
            features = QgsJsonUtils.stringToFeatureList(
                json.dumps(
                    {
                        "type": "Feature",
                        "properties": {},
                        "geometry": self.area["geometry"],
                    }
                )
            )
            if not features:
                raise ValueError("Analysis area has no displayable geometry")
            geometry = features[0].geometry()
            transform = QgsCoordinateTransform(
                QgsCoordinateReferenceSystem("EPSG:4326"),
                crs,
                self.canvas.mapSettings().transformContext(),
            )
            geometry.transform(transform)
            self.band.setToGeometry(geometry)
            self.band.show()
            center = self.area.get("center")
            if center:
                self.marker.setCenter(transform.transform(QgsPointXY(*center)))
                self.marker.show()
        except (ValueError, RuntimeError, KeyError) as error:
            self.failed.emit(str(error))

    def show_area(self):
        self.visible = True
        self._render()
        if self.area and self.band.isVisible():
            bounds = self.band.asGeometry().boundingBox()
            bounds.scale(1.15)
            self.canvas.setExtent(bounds)
            self.canvas.refresh()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.canvas.destinationCrsChanged.disconnect(self._render)
        for item in (self.band, self.marker):
            self.canvas.scene().removeItem(item)
            sip.delete(item)
        self.area = None
