from qgis.core import QgsVectorLayer, QgsProject, QgsJsonExporter, QgsWkbTypes
from qgis.PyQt.QtWidgets import QDialog, QVBoxLayout, QLabel, QLineEdit, QPushButton, QMessageBox, QWidget, QCheckBox, \
    QHBoxLayout

import json


def update_style(button):
    current_style = button.styleSheet().rstrip(';')

    other_styles = '; '.join(
        style for style in current_style.split(';') if not style.strip().startswith('background-color'))

    background_color = "\nbackground-color: black;" if button.isEnabled() else "\nbackground-color: gray;"

    new_style = '; '.join([other_styles.strip(), background_color]).strip('; ')

    button.setStyleSheet(new_style)


def toggle_button_state(button):
    button.setEnabled(not button.isEnabled())

    update_style(button)


def add_data(data, name):
    data_json = json.loads(data)
    point_features = []
    line_features = []
    polygon_features = []

    for feature in data_json['features']:
        geom_type = feature['geometry']['type']
        if geom_type == 'Point':
            point_features.append(feature)
        elif geom_type == 'LineString':
            line_features.append(feature)
        elif geom_type == 'Polygon':
            polygon_features.append(feature)

    if point_features:
        point_geojson = json.dumps({'type': 'FeatureCollection', 'features': point_features})
        vector_layer = QgsVectorLayer(point_geojson, f'{name}_points', "ogr")
        QgsProject.instance().addMapLayer(vector_layer)

    if line_features:
        line_geojson = json.dumps({'type': 'FeatureCollection', 'features': line_features})
        vector_layer = QgsVectorLayer(line_geojson, f'{name}_lines', "ogr")
        QgsProject.instance().addMapLayer(vector_layer)

    if polygon_features:
        polygon_geojson = json.dumps({'type': 'FeatureCollection', 'features': polygon_features})
        vector_layer = QgsVectorLayer(polygon_geojson, f'{name}_polygons', "ogr")
        QgsProject.instance().addMapLayer(vector_layer)


def update_labels(api_response, main_ui):
    for label in main_ui.labels.values():
        label.setVisible(False)
        label.setText("")

    for option, result in api_response.items():
        if option in main_ui.labels:
            label = main_ui.labels[option]
            label.setVisible(True)

            if result == 'error':
                label.setText("Error")
                label.setStyleSheet("color: red;")
            elif result == 'success':
                label.setText("Success")
                label.setStyleSheet("color: green;")


def update_scroll_area(main_ui):
    main_ui.container = QWidget()
    main_ui.layout = QVBoxLayout()
    main_ui.checkboxes = {}
    main_ui.labels = {}

    layer_names = set()
    main_ui.options = []
    for layer in QgsProject.instance().mapLayers().values():
        if layer.type() == QgsVectorLayer.VectorLayer:
            name = layer.name()
            if layer.geometryType() == QgsWkbTypes.PointGeometry:
                geom_type = '_points'
            elif layer.geometryType() == QgsWkbTypes.LineGeometry:
                geom_type = '_lines'
            elif layer.geometryType() == QgsWkbTypes.PolygonGeometry:
                geom_type = '_polys'
            else:
                geom_type = ''

            new_name = name + geom_type if name in layer_names else name
            layer_names.add(new_name)
            main_ui.options.append(new_name)

    for option in main_ui.options:
        check_box = QCheckBox(option)
        label = QLabel("")
        h_layout = QHBoxLayout()
        h_layout.addWidget(check_box)
        h_layout.addWidget(label)

        main_ui.layout.addLayout(h_layout)
        main_ui.checkboxes[option] = check_box
        main_ui.labels[option] = label

    main_ui.container.setLayout(main_ui.layout)
    main_ui.scroll_area.setWidget(main_ui.container)
    main_ui.scroll_area.setWidgetResizable(True)


def get_selected_options(main_ui):
    selected_options = []
    for option, check_box in main_ui.checkboxes.items():
        if check_box.isChecked():
            selected_options.append(option)
    return selected_options
