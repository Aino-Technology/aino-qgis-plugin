from types import SimpleNamespace

import pytest

from qgis.PyQt import sip
from qgis.PyQt.QtCore import QCoreApplication, QEvent, Qt
from qgis.PyQt.QtWidgets import QMainWindow, QWidget

from src.dock import AinoDockWidget


class Content(QWidget):
    def __init__(self):
        super().__init__()
        self.suspended = 0
        self.stopped = 0

    def suspend_map_interaction(self):
        self.suspended += 1

    def shutdown(self):
        self.stopped += 1


def test_native_dock_hide_float_restore_and_deletion(qgis_app):
    window = QMainWindow()
    window.setCentralWidget(QWidget())
    content = Content()
    content.setMinimumHeight(900)
    dock = AinoDockWidget(SimpleNamespace(mainWindow=lambda: window), content)
    dock.register(
        SimpleNamespace(mainWindow=lambda: window, addDockWidget=window.addDockWidget)
    )
    window.resize(1000, 600)
    window.show()
    qgis_app.processEvents()
    assert dock.objectName() == "AinoDockWidget"
    assert dock.parent() is window
    assert dock.minimumWidth() == 320
    assert content.width() == dock.scroll.viewport().width()
    assert dock.scroll.widget() is content
    assert dock.scroll.verticalScrollBar().maximum() > 0
    dock.hide()
    qgis_app.processEvents()
    assert content.suspended > 0 and content.stopped == 0
    dock.show()
    dock.setFloating(True)
    qgis_app.processEvents()
    assert dock.isFloating()
    dock.resize(460, 550)
    saved = window.saveState()
    dock.setFloating(False)
    assert window.restoreState(saved)
    assert dock.isFloating()
    dock.close()
    assert not sip.isdeleted(dock) and content.stopped == 0
    dock.show()
    assert dock.isVisible()
    dock.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert sip.isdeleted(content) and sip.isdeleted(dock)
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.mark.parametrize("floating", [False, True])
def test_native_new_window_restores_saved_placement(qgis_app, floating):
    first = QMainWindow()
    first.setCentralWidget(QWidget())
    dock = AinoDockWidget(SimpleNamespace(mainWindow=lambda: first), Content())
    first.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
    first.resize(1100, 650)
    first.show()
    qgis_app.processEvents()
    first.resizeDocks([dock], [470], Qt.Orientation.Horizontal)
    qgis_app.processEvents()
    dock.setFloating(floating)
    qgis_app.processEvents()
    width = dock.width()
    saved = first.saveState()
    second = QMainWindow()
    second.setCentralWidget(QWidget())
    second.resize(1100, 650)
    second.show()
    assert second.restoreState(saved)
    replacement = AinoDockWidget(SimpleNamespace(mainWindow=lambda: second), Content())
    replacement.register(
        SimpleNamespace(mainWindow=lambda: second, addDockWidget=second.addDockWidget)
    )
    replacement.show()
    qgis_app.processEvents()
    assert second.dockWidgetArea(replacement) == Qt.DockWidgetArea.LeftDockWidgetArea
    assert replacement.isFloating() is floating
    assert abs(replacement.width() - width) <= 2
    for window in (first, second):
        window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_native_dock_hide_returns_main_canvas_tool(client, qgis_app):
    from qgis.core import QgsProject, QgsPointXY, QgsCoordinateReferenceSystem
    from qgis.gui import QgsMapCanvas, QgsMapToolPan
    from src.main_tab import OsmParserDialogMain

    session, api = client
    window = QMainWindow()
    canvas = QgsMapCanvas(window)
    canvas.setProject(QgsProject.instance())
    canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
    window.setCentralWidget(canvas)
    iface = SimpleNamespace(
        mainWindow=lambda: window,
        mapCanvas=lambda: canvas,
        addDockWidget=window.addDockWidget,
    )
    content = OsmParserDialogMain(
        osm_parser=SimpleNamespace(session=session, api_client=api, iface=iface)
    )
    dock = AinoDockWidget(iface, content)
    window.resize(1100, 650)
    window.show()
    dock.register(iface)
    qgis_app.processEvents()
    assert dock.width() == 380
    window.resize(1000, 460)
    window.resizeDocks([dock], [320], Qt.Orientation.Horizontal)
    qgis_app.processEvents()
    assert dock.width() == 320
    assert content.width() == dock.scroll.viewport().width()
    for widget in (content.prompt_field, content.go_button, content.prompts_label):
        assert widget.geometry().left() >= 0
        assert widget.geometry().right() < content.main_tab.width()
    dock.scroll.ensureWidgetVisible(content.go_button)
    qgis_app.processEvents()
    position = content.go_button.mapTo(
        dock.scroll.viewport(), content.go_button.rect().center()
    )
    assert dock.scroll.viewport().rect().contains(position)
    assert content.sign_out_button.isVisible()
    previous = QgsMapToolPan(canvas)
    canvas.setMapTool(previous)
    content.area_panel.mode.setCurrentIndex(3)
    content.area_panel.pick_point()
    assert canvas.mapTool() is content.area_panel.point_tool
    dock.hide()
    qgis_app.processEvents()
    assert canvas.mapTool() is previous
    assert not content.request_controller._closed
    dock.show()
    content.area_panel.pick_point()
    content.area_panel.point_tool.canvasClicked.emit(
        QgsPointXY(0, 0), Qt.MouseButton.LeftButton
    )
    assert content.area_panel.selection()["radius_m"] == 1000
    dock.close()
    assert canvas.mapTool() is previous
    content.shutdown()
    content.shutdown()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert sip.isdeleted(content) and sip.isdeleted(dock)


def test_native_polling_owns_and_deletes_timer_without_destroyed_callback(
    client, qgis_app
):
    from src.analysis.polling import PollingController
    from qgis.PyQt.QtCore import QObject

    session, api = client
    parent = QObject()
    polling = PollingController(api, parent)
    scheduler = polling.scheduler
    timer = scheduler.timer
    fired = []
    scheduler.schedule(0, lambda: fired.append(True))
    assert scheduler.parent() is polling and timer.parent() is scheduler
    parent.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qgis_app.processEvents()
    assert sip.isdeleted(polling) and sip.isdeleted(scheduler) and sip.isdeleted(timer)
    assert not fired


def test_native_parent_delete_aborts_inflight_poll(
    client, local_api, wait_for, qgis_app
):
    from qgis.PyQt.QtCore import QObject
    from src.analysis.polling import PollingController
    from test_api_client import token

    session, api = client
    session.set_access_token(token())
    parent = QObject()
    polling = PollingController(api, parent)
    updated = []
    polling.updated.connect(updated.append)
    polling.watch("owned-task")
    wait_for(lambda: len(local_api.requests) == 1)
    assert api.pending_count == 1
    parent.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qgis_app.processEvents()
    assert sip.isdeleted(polling) and api.pending_count == 0
    assert not updated
    assert len(local_api.requests) == 1


def test_native_completed_poll_cleanup_connections_are_bounded(
    client, local_api, wait_for
):
    import gc
    from src.analysis.polling import PollingController
    from test_api_client import token

    session, api = client
    session.set_access_token(token())
    polling = PollingController(api)
    counts = []
    polling.watch("bounded-task")
    for index in range(12):
        wait_for(lambda: len(local_api.requests) == index + 1)
        local_api.respond(
            index,
            data={
                "task_id": "bounded-task",
                "status": "running",
                "revision": index,
                "steps": [],
            },
        )
        wait_for(lambda: api.pending_count == 0)
        polling.scheduler.stop()
        gc.collect()
        counts.append(polling.receivers(polling.destroyed))
        if index < 11:
            polling._poll()
    polling.stop()
    polling.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert max(counts[3:]) <= max(counts[:3])
