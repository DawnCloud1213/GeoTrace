"""右侧设置面板 — 浮动覆盖在地图上."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)


class SettingsPanel(QFrame):
    """浮动设置面板."""

    addDirectory = Signal(str)
    removeDirectory = Signal(str)
    rescanRequested = Signal()
    closeRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("floatingPanel")
        self.setStyleSheet("""
            #floatingPanel {
                background: rgba(255,255,255,0.96);
                border: 1px solid #ddd;
                border-radius: 8px;
            }
        """)
        self.setMinimumWidth(180)
        self.setMaximumWidth(250)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(8)

        # 标题栏
        header = QHBoxLayout()
        title = QLabel("设置")
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

        # ── 照片目录 ──
        dir_label = QLabel("照片目录")
        dir_label.setFont(QFont("", 10, QFont.Bold))
        layout.addWidget(dir_label)

        self._dir_list = QListWidget()
        self._dir_list.setStyleSheet("""
            QListWidget {
                border: 1px solid #e8e8e8; border-radius: 4px;
                background: #fafaf5; font-size: 12px;
            }
            QListWidget::item {
                padding: 4px 8px; border-bottom: 1px solid #f0f0f0;
            }
            QListWidget::item:hover { background: #fff3e0; }
        """)
        layout.addWidget(self._dir_list)

        btn_style = """
            QPushButton {
                border: none; border-radius: 4px;
                padding: 5px 10px; font-size: 12px; color: white;
            }
        """
        add_btn = QPushButton("+ 添加目录")
        add_btn.setStyleSheet(btn_style + """
            QPushButton { background: #43a047; }
            QPushButton:hover { background: #388e3c; }
        """)
        add_btn.clicked.connect(self._on_add_directory)
        layout.addWidget(add_btn)

        remove_btn = QPushButton("- 移除选中")
        remove_btn.setStyleSheet(btn_style + """
            QPushButton { background: #e57373; }
            QPushButton:hover { background: #d32f2f; }
        """)
        remove_btn.clicked.connect(self._on_remove_directory)
        layout.addWidget(remove_btn)

        # ── 分隔 ──
        sep = QLabel()
        sep.setFixedHeight(1)
        sep.setStyleSheet("background: #e0e0e0;")
        layout.addWidget(sep)

        # ── 扫描 ──
        scan_label = QLabel("扫描")
        scan_label.setFont(QFont("", 10, QFont.Bold))
        layout.addWidget(scan_label)

        rescan_btn = QPushButton("重新扫描所有目录")
        rescan_btn.setStyleSheet(btn_style + """
            QPushButton { background: #ff9800; padding: 7px 12px; font-size: 13px; }
            QPushButton:hover { background: #f57c00; }
        """)
        rescan_btn.clicked.connect(self._on_rescan)
        layout.addWidget(rescan_btn)

        self._progress = QProgressBar()
        self._progress.setVisible(False)
        self._progress.setStyleSheet("""
            QProgressBar {
                border: 1px solid #ddd; border-radius: 3px;
                background: #f5f5f5; height: 14px;
            }
            QProgressBar::chunk { background: #ff9800; border-radius: 2px; }
        """)
        layout.addWidget(self._progress)

        layout.addStretch()
        self.hide()

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def set_directories(self, dirs: list[str]) -> None:
        self._dir_list.clear()
        for d in dirs:
            item = QListWidgetItem(d)
            item.setData(Qt.UserRole, d)
            self._dir_list.addItem(item)

    def set_progress(self, current: int, total: int) -> None:
        self._progress.setVisible(True)
        self._progress.setMaximum(total)
        self._progress.setValue(current)

    def hide_progress(self) -> None:
        self._progress.setVisible(False)

    # ------------------------------------------------------------------
    # 内部回调
    # ------------------------------------------------------------------

    def _on_add_directory(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择照片目录")
        if directory:
            self.addDirectory.emit(directory)

    def _on_remove_directory(self) -> None:
        item = self._dir_list.currentItem()
        if item is None:
            QMessageBox.information(self, "提示", "请先选中要移除的目录")
            return
        path = item.data(Qt.UserRole)
        if path:
            self.removeDirectory.emit(path)

    def _on_rescan(self) -> None:
        self.rescanRequested.emit()
