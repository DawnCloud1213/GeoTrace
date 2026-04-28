"""左侧省份列表面板 — 浮动覆盖在地图上."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)


class ProvinceListPanel(QFrame):
    """浮动省份列表: 按照片数降序, 点击切换到对应省份."""

    provinceClicked = Signal(str)
    closeRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._stats: list[dict] = []
        self.setObjectName("floatingPanel")
        self.setStyleSheet("""
            #floatingPanel {
                background: rgba(255,255,255,0.96);
                border: 1px solid #ddd;
                border-radius: 8px;
            }
        """)
        self.setMinimumWidth(170)
        self.setMaximumWidth(220)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(6)

        # 标题栏
        header = QHBoxLayout()
        title = QLabel("省份")
        title.setFont(QFont("", 11, QFont.Bold))
        header.addWidget(title)
        header.addStretch()
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(24, 24)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet("""
            QPushButton {
                background: transparent; border: none;
                font-size: 14px; color: #999;
            }
            QPushButton:hover { color: #333; }
        """)
        close_btn.clicked.connect(self.closeRequested.emit)
        header.addWidget(close_btn)
        layout.addLayout(header)

        # 列表
        self._list = QListWidget()
        self._list.setAlternatingRowColors(True)
        self._list.setStyleSheet("""
            QListWidget {
                border: 1px solid #e8e8e8;
                border-radius: 4px;
                background: #fafaf5;
                font-size: 13px;
            }
            QListWidget::item {
                padding: 5px 10px;
                border-bottom: 1px solid #f0f0f0;
            }
            QListWidget::item:hover {
                background: #fff3e0;
            }
            QListWidget::item:selected {
                background: #ffcc80; color: #333;
            }
            QListWidget::item:alternate {
                background: #f8f5f0;
            }
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

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        name = item.data(Qt.UserRole)
        if name:
            self.provinceClicked.emit(name)
