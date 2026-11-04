from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtWidgets import (
    QDockWidget,
    QScrollArea,
    QSizePolicy,
    QWidget,
    QVBoxLayout,
)


class AinoDockWidget(QDockWidget):
    def __init__(self, iface, content, parent=None):
        super().__init__("Aino", parent or iface.mainWindow())
        self.setObjectName("AinoDockWidget")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.content = content
        self.setMinimumWidth(320)
        content.setMinimumWidth(0)
        content.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        content.setMaximumWidth(16777215)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setWidget(content)
        container = QWidget(self)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        account = getattr(content, "account_widget", None)
        if account is not None:
            content.setCornerWidget(None)
            account.setParent(container)
            content.account_email_label.setWordWrap(True)
            content.account_email_label.setSizePolicy(
                QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
            )
            layout.addWidget(account)
            account.show()
        layout.addWidget(self.scroll)
        self.setWidget(container)
        self.visibilityChanged.connect(self._visibility_changed)

    def register(self, iface):
        window = iface.mainWindow()
        restored = window.restoreDockWidget(self)
        area = (
            window.dockWidgetArea(self)
            if restored
            else Qt.DockWidgetArea.RightDockWidgetArea
        )
        iface.addDockWidget(area, self)
        if not restored:
            window.resizeDocks([self], [380], Qt.Orientation.Horizontal)

    def sizeHint(self):
        return QSize(380, 600)

    def _visibility_changed(self, visible):
        if not visible:
            self.content.suspend_map_interaction()
