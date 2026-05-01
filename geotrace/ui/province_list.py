"""左侧省份列表面板 — 浮动覆盖在地图上."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from geotrace.ui.theme import Colors, Fonts, frosted_rgba, Metrics


class ProvinceListPanel(QFrame):
    """浮动省份列表: 按照片数降序, 点击切换到对应省份."""

    provinceClicked = Signal(str)
    closeRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._stats: list[dict] = []
        self.setObjectName("floatingPanel")
        self.setMinimumWidth(170)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(6)

        # 标题栏
        header = QHBoxLayout()
        title = QLabel("省份")
        title.setFont(Fonts.title(11))
        header.addWidget(title)
        header.addStretch()
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(24, 24)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setProperty("cssClass", "ghost")
        close_btn.clicked.connect(self.closeRequested.emit)
        header.addWidget(close_btn)
        layout.addLayout(header)

        # 列表
        self._list = QListWidget()
        self._list.setAlternatingRowColors(True)
        self._list.setStyleSheet(f"""
            QListWidget {{
                border: 1px solid {Colors.BORDER_LIGHT};
                border-radius: 4px;
                background: {Colors.INPUT_BG};
                font-size: 13px;
            }}
            QListWidget::item {{
                padding: 5px 10px;
                border-bottom: 1px solid {Colors.BORDER_LIGHT};
            }}
            QListWidget::item:hover {{
                background: {Colors.ACCENT_HOVER_LIGHT};
            }}
            QListWidget::item:selected {{
                background: {Colors.ACCENT_SELECTED};
                color: {Colors.TEXT_PRIMARY};
            }}
            QListWidget::item:alternate {{
                background: {Colors.WINDOW_BG};
            }}
        """)
        self._list.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self._list)

        self.hide()

    def refresh(self, stats: list[dict]) -> None:
        self._stats = sorted(stats, key=lambda s: s.get("value", 0), reverse=True)
        self._list.clear()
        for s in self._stats:
            name = s.get("name", "")
            value = s.get("value", 0)
            item = QListWidgetItem(f"{name}  ({value})")
            item.setData(Qt.UserRole, name)
            self._list.addItem(item)

    def set_frosted_alpha(self, alpha: float) -> None:
        bg = frosted_rgba(alpha)
        self.setStyleSheet(f"""
            QFrame#floatingPanel {{
                background: {bg};
                border: 1px solid {Colors.BORDER_LIGHT};
                border-radius: {Metrics.BORDER_RADIUS_MD}px;
            }}
        """)

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        name = item.data(Qt.UserRole)
        if name:
            self.provinceClicked.emit(name)
