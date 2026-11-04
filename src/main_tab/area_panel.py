import copy

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsMapToolEmitPoint
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from src.project_sync.area import capture_area
from src.project_sync.area_preview import AreaPreview


class PointCapture(QgsMapToolEmitPoint):
    cancelled = pyqtSignal()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
            event.accept()
        else:
            super().keyPressEvent(event)


class AreaPanel(QWidget):
    """Keep explicit manual choices separate from optional vector input layers."""

    selection_changed = pyqtSignal()

    def __init__(self, canvas, project=None, parent=None):
        super().__init__(parent)
        self.canvas = canvas
        self.project = project if project is not None else QgsProject.instance()
        self.confirmed = {}
        self.resolved = None
        self.point_tool = None
        self.previous_tool = None
        self.preview = AreaPreview(canvas, self) if canvas is not None else None
        self._layer_signals = []
        self._area_handles = []
        self._area_cache = {}
        self._area_key = None
        self._area_generation = 0
        self.initially_empty = not bool(self.project.mapLayers())
        self.zoomed_result = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("Analysis area"))
        self.mode = QComboBox()
        self.mode.addItems(
            [
                "From your request",
                "Current map view",
                "Selected polygons",
                "Point and radius",
            ]
        )
        layout.addWidget(self.mode)
        self.description = QLabel()
        self.description.setWordWrap(True)
        layout.addWidget(self.description)
        self.use_view = QPushButton("Use current view")
        self.use_view.clicked.connect(self.capture_view)
        layout.addWidget(self.use_view)
        self.polygons = QComboBox()
        layout.addWidget(self.polygons)
        self.polygon_count = QLabel()
        layout.addWidget(self.polygon_count)
        row = QHBoxLayout()
        self.pick = QPushButton("Pick on map")
        self.pick.clicked.connect(self.pick_point)
        row.addWidget(self.pick)
        self.radius = QDoubleSpinBox()
        self.radius.setRange(0.01, 10000000)
        self.radius.setValue(1000)
        self.radius.setSuffix(" m")
        row.addWidget(self.radius)
        layout.addLayout(row)
        self.message = QLabel()
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.show_area = QPushButton("Show area")
        self.show_area.clicked.connect(self._show_area)
        layout.addWidget(self.show_area)
        self.empty_hint = QLabel(
            "No map layers yet. Add a basemap, or use From your request and name a place."
        )
        self.empty_hint.setWordWrap(True)
        layout.addWidget(self.empty_hint)
        self.basemap = QPushButton("Add OpenStreetMap basemap")
        self.basemap.clicked.connect(self._add_basemap)
        layout.addWidget(self.basemap)
        layout.addWidget(QLabel("Input vector layers (optional; all features)"))
        self.inputs = QListWidget()
        layout.addWidget(self.inputs)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self.polygons.currentIndexChanged.connect(self._polygon_changed)
        self.radius.valueChanged.connect(self._radius_changed)
        self.inputs.itemChanged.connect(lambda *_: self.selection_changed.emit())
        if self.preview:
            self.destroyed.connect(self.preview.close)
        owned_handles = self._area_handles
        self.destroyed.connect(lambda: [handle.abort() for handle in owned_handles])
        self.project.layersAdded.connect(self.refresh_layers)
        self.project.layerTreeRoot().addedChildren.connect(self.refresh_layers)
        self.project.layersRemoved.connect(self.refresh_layers)
        self.project.cleared.connect(self.reset)
        if canvas is not None:
            canvas.destinationCrsChanged.connect(self._crs_changed)
            self.preview.failed.connect(self.message.setText)
        self.refresh_layers()
        self._mode_changed()

    def selected_input_ids(self):
        return [
            self.inputs.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.inputs.count())
            if self.inputs.item(i).checkState() == Qt.CheckState.Checked
        ]

    def selection(self):
        index = self.mode.currentIndex()
        if index == 0:
            return None
        area = self.confirmed.get(index)
        if not area:
            raise ValueError(
                {
                    1: "Press Use current view to confirm the analysis area",
                    2: "Select polygon features in the boundary layer",
                    3: "Pick a point on the map and set its radius",
                }[index]
            )
        return copy.deepcopy(area)

    def set_resolved_area(self, area):
        self.resolved = copy.deepcopy(area)
        if self.mode.currentIndex() == 0:
            self._display()

    def _display(self):
        index = self.mode.currentIndex()
        area = self.resolved if index == 0 else self.confirmed.get(index)
        if self.preview:
            self.preview.set_area(area)
        self.show_area.setVisible(bool(area))
        if index and area:
            self.message.setText(
                "Using the selected area"
                + (f" · {area['radius_m']:g} m around your point" if index == 3 else "")
            )
        elif index == 0:
            self.message.setText(
                "Area from your request"
                + (" · " + area["label"] if area and area.get("label") else "")
                + (
                    " · Place coverage (provider extent)"
                    if area and area.get("boundary_source") == "viewport"
                    else ""
                )
                + (
                    f" · {area['radius_m']:g} m radius"
                    if area and area.get("radius_m")
                    else ""
                )
            )

    def _mode_changed(self, *args):
        self.cancel_capture()
        index = self.mode.currentIndex()
        self.description.setText(
            [
                "Name a place in your request. You do not need to position the map first.",
                "Position the map, then press Use current view. Moving the map afterwards keeps your selection.",
                "Only selected polygons define the boundary. Input layers are chosen separately.",
                "Pick a point on the map, then set the geodesic radius in metres.",
            ][index]
        )
        self.use_view.setVisible(index == 1)
        self.polygons.setVisible(index == 2)
        self.polygon_count.setVisible(index == 2)
        self.pick.setVisible(index == 3)
        self.radius.setVisible(index == 3)
        self.message.clear()
        if index == 2:
            self._polygon_changed()
        self._display()
        self.selection_changed.emit()

    def _accept(self, index, area):
        self.confirmed[index] = copy.deepcopy(area)
        self._display()
        self.selection_changed.emit()

    def capture_view(self):
        try:
            self._accept(1, capture_area("extent", self.canvas, []))
        except (ValueError, RuntimeError) as error:
            self.message.setText(str(error))

    def refresh_layers(self, *args):
        checked = set(self.selected_input_ids())
        selected = self.polygons.currentData()
        for signal in self._layer_signals:
            try:
                signal.disconnect(self._polygon_changed)
            except (TypeError, RuntimeError):
                pass
        self._layer_signals.clear()
        self.inputs.blockSignals(True)
        self.polygons.blockSignals(True)
        self.inputs.clear()
        self.polygons.clear()
        for layer in self.project.layerTreeRoot().layerOrder():
            if not isinstance(layer, QgsVectorLayer) or not layer.isSpatial():
                continue
            item = QListWidgetItem(layer.name())
            item.setData(Qt.ItemDataRole.UserRole, layer.id())
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if layer.id() in checked
                else Qt.CheckState.Unchecked
            )
            self.inputs.addItem(item)
            if layer.geometryType() == QgsWkbTypes.GeometryType.PolygonGeometry:
                self.polygons.addItem(layer.name(), layer.id())
                for signal in (
                    layer.selectionChanged,
                    layer.geometryChanged,
                    layer.crsChanged,
                ):
                    signal.connect(self._polygon_changed)
                    self._layer_signals.append(signal)
        index = self.polygons.findData(selected)
        if index >= 0:
            self.polygons.setCurrentIndex(index)
        self.inputs.blockSignals(False)
        self.polygons.blockSignals(False)
        self.empty_hint.setVisible(not bool(self.project.mapLayers()))
        self._polygon_changed()

    def _polygon_changed(self, *args):
        layer = self.project.mapLayer(self.polygons.currentData() or "")
        self.polygon_count.setText(
            f"{layer.selectedFeatureCount()} polygons selected"
            if layer
            else "No polygon layer available"
        )
        if self.mode.currentIndex() != 2:
            return
        try:
            self._accept(
                2, capture_area("polygon", self.canvas, [layer] if layer else [])
            )
        except (ValueError, RuntimeError) as error:
            self.confirmed.pop(2, None)
            self._display()
            self.message.setText(str(error))
            self.selection_changed.emit()

    def pick_point(self):
        if self.canvas is None:
            self.message.setText("Open a QGIS map canvas to pick a point")
            return
        self.cancel_capture()
        self.previous_tool = self.canvas.mapTool()
        self.point_tool = PointCapture(self.canvas)
        self.point_tool.canvasClicked.connect(self._point_picked)
        self.point_tool.cancelled.connect(self.cancel_capture)
        self.canvas.setMapTool(self.point_tool)
        self.canvas.setFocus()
        self.message.setText(
            "Click the map to choose a point. Escape keeps your previous area."
        )

    def _point_picked(self, point, button):
        if button != Qt.MouseButton.LeftButton:
            return
        try:
            area = capture_area(
                "radius", self.canvas, [], (point.x(), point.y()), self.radius.value()
            )
            self._accept(3, area)
        except (ValueError, RuntimeError) as error:
            self.message.setText(str(error))
        finally:
            self.cancel_capture()

    def _radius_changed(self, *args):
        area = self.confirmed.get(3)
        if not area or self.canvas is None:
            return
        try:
            self._accept(
                3,
                capture_area(
                    "radius",
                    self.canvas,
                    [],
                    area["center"],
                    self.radius.value(),
                    center_crs=QgsCoordinateReferenceSystem("EPSG:4326"),
                ),
            )
        except (ValueError, RuntimeError) as error:
            self.message.setText(str(error))

    def cancel_capture(self):
        if self.point_tool is None:
            return
        tool = self.point_tool
        self.point_tool = None
        if self.canvas.mapTool() == tool:
            self.canvas.unsetMapTool(tool)
            if self.previous_tool is not None:
                self.canvas.setMapTool(self.previous_tool)
        self.previous_tool = None
        tool.deleteLater()

    def _crs_changed(self):
        if self.point_tool is not None:
            self.cancel_capture()
            self.message.setText(
                "Point capture cancelled because the map CRS changed. The previous area is retained."
            )

    def set_preview_visible(self, visible):
        if not visible:
            self.cancel_capture()
        if self.preview:
            self.preview.set_visible(visible)

    def _show_area(self):
        if self.preview:
            self.preview.show_area()

    def reset(self):
        self.cancel_capture()
        self._area_generation += 1
        for handle in self._area_handles:
            handle.abort()
        self._area_handles.clear()
        self._area_key = None
        self._area_cache.clear()
        self.initially_empty = not bool(self.project.mapLayers())
        self.zoomed_result = False
        self.confirmed.clear()
        self.resolved = None
        self.mode.setCurrentIndex(0)
        self._display()

    def update_analysis(self, api, state, identity):
        latest = state.get("latest") or {}
        task = state.get("task_id")
        revision = latest.get("area_revision", 0)
        key = (identity, api.session.account_id, task, revision)
        if key == self._area_key:
            return
        self._area_key = key
        self._area_generation += 1
        generation = self._area_generation
        for handle in self._area_handles:
            handle.abort()
        self._area_handles.clear()
        self.set_resolved_area(None)
        if not task or not revision:
            return
        if key in self._area_cache:
            self.set_resolved_area(self._area_cache[key])
            return

        def finished(response):
            if generation != self._area_generation or api.session.account_id != key[1]:
                return
            from src.project_sync.recovery import project_identity

            if project_identity(self.project, create=False) != identity:
                return
            data = response.data
            if not response.ok or not isinstance(data, dict):
                self.message.setText(
                    "Could not load the analysis area. " + response.message
                )
                return
            if data.get("task_id") != task or data.get("revision") != revision:
                return
            self._area_cache[key] = copy.deepcopy(data.get("area"))
            self.set_resolved_area(data.get("area"))

        self._area_handles.append(
            api.send("GET", f"/integrations/qgis/analyses/{task}/area", None, finished)
        )

    def results_imported(self, layer_ids):
        if not self.initially_empty or self.zoomed_result or self.canvas is None:
            return
        from qgis.core import QgsCoordinateTransform, QgsRectangle

        bounds = QgsRectangle()
        for layer_id in layer_ids:
            layer = self.project.mapLayer(layer_id)
            if layer is None or layer.extent().isEmpty():
                continue
            transform = QgsCoordinateTransform(
                layer.crs(),
                self.canvas.mapSettings().destinationCrs(),
                self.canvas.mapSettings().transformContext(),
            )
            bounds.combineExtentWith(transform.transformBoundingBox(layer.extent()))
        if not bounds.isEmpty():
            bounds.scale(1.1)
            self.canvas.setExtent(bounds)
            self.canvas.refresh()
            self.zoomed_result = True

    def _add_basemap(self):
        from src.project_sync.area import add_osm_basemap

        try:
            add_osm_basemap(self.project, self.canvas)
        except (ValueError, RuntimeError) as error:
            self.message.setText(str(error))

    def close(self):
        self.cancel_capture()
        for handle in self._area_handles:
            handle.abort()
        self._area_handles.clear()
        if self.preview:
            self.preview.close()
        return super().close()
