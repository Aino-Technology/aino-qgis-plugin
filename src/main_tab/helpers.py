import json
from datetime import datetime, timezone
from math import ceil

from qgis.core import QgsProject, QgsVectorLayer, QgsWkbTypes
from qgis.PyQt.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget


def update_style(button):
    current_style = button.styleSheet().rstrip(";")

    other_styles = "; ".join(
        style
        for style in current_style.split(";")
        if not style.strip().startswith("background-color")
    )

    background_color = (
        "\nbackground-color: black;"
        if button.isEnabled()
        else "\nbackground-color: gray;"
    )

    new_style = "; ".join([other_styles.strip(), background_color]).strip("; ")

    button.setStyleSheet(new_style)


def toggle_button_state(button):
    set_button_enabled(button, not button.isEnabled())


def set_button_enabled(button, enabled):
    button.setEnabled(enabled)

    update_style(button)


def add_data(data, name):
    data_json = json.loads(data)
    features_by_geometry = {}

    for feature in data_json["features"]:
        geometry = feature.get("geometry") or {}
        geometry_type = geometry.get("type")
        if geometry_type:
            features_by_geometry.setdefault(geometry_type, []).append(feature)

    for geometry_type, features in features_by_geometry.items():
        geometry_suffix = geometry_type.lower()
        geojson = json.dumps({"type": "FeatureCollection", "features": features})
        vector_layer = QgsVectorLayer(geojson, f"{name}_{geometry_suffix}", "ogr")
        if vector_layer.isValid():
            QgsProject.instance().addMapLayer(vector_layer)


def update_labels(api_response, main_ui):
    for label in main_ui.labels.values():
        label.setVisible(False)
        label.setText("")

    for option, result in api_response.items():
        if option in main_ui.labels:
            label = main_ui.labels[option]
            label.setVisible(True)

            if result == "error":
                label.setText("Error")
                label.setStyleSheet("color: red;")
            elif result == "success":
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
                geom_type = "_points"
            elif layer.geometryType() == QgsWkbTypes.LineGeometry:
                geom_type = "_lines"
            elif layer.geometryType() == QgsWkbTypes.PolygonGeometry:
                geom_type = "_polys"
            else:
                geom_type = ""

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


def update_days_label(main_ui, api_response):
    expiration_datetime = api_response["current_subscription"]["expiration_date"]
    if expiration_datetime:
        expiration_datetime = datetime.fromisoformat(
            expiration_datetime.replace("Z", "+00:00")
        )
        start_datetime = api_response["current_subscription"]["start_date"]
        start_datetime = datetime.fromisoformat(start_datetime.replace("Z", "+00:00"))

        current_datetime = datetime.now(timezone.utc)

        delta_remaining = expiration_datetime - current_datetime
        delta_start_datetime = expiration_datetime - start_datetime

        days_remaining = delta_remaining.total_seconds() / (3600 * 24)
        days_since_start = delta_start_datetime.total_seconds() / (3600 * 24)

        if expiration_datetime == current_datetime:
            days_remaining_rounded = 0
        else:
            days_remaining_rounded = ceil(days_remaining)

        if current_datetime < expiration_datetime:
            main_ui.trial_label.setText(
                f"{days_remaining_rounded}/{round(days_since_start)} days left"
            )


def update_prompts_left_label(main_ui, api_response):
    total_amount = None
    left_amount = None
    limits = api_response["limits"]
    for limit in limits:
        if limit["name"] == "ai_query":
            if limit["type"] == "unlimited":
                main_ui.prompts_label.setText("Unlimited prompts")
                main_ui.prompts_label.setStyleSheet("")
                return None
            total_amount = limit["total_amount"]
            left_amount = limit["left_amount"]
    if total_amount is not None and left_amount is not None:
        if left_amount > 0:
            main_ui.prompts_label.setText(f"{left_amount}/{total_amount} prompts")
            main_ui.prompts_label.setStyleSheet("")

        else:
            main_ui.prompts_label.setText(f"{left_amount}/{total_amount} prompts")
            main_ui.prompts_label.setStyleSheet("color: red;")
    return left_amount


def block_input_area(main_ui, reason):
    link = "<a href='https://app.aino.world'>app.aino.world</a>"
    message = None
    if reason == "subscription":
        message = f"To get more prompts upgrade your subscription at {link}"
    elif reason == "prompts":
        message = "To get more prompts please contact us via hello@aino.world"
    if message:
        main_ui.prompt_field.setReadOnly(True)
        main_ui.prompt_field.clear()
        main_ui.prompt_field.setHtml(message)
        main_ui.prompt_field.setStyleSheet("background-color: #F0F0F0; color: #A0A0A0;")
        toggle_button_state(main_ui.go_button)
