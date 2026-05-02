"""右侧设置面板 — 浮动覆盖在地图上."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSlider,
    QVBoxLayout,
)

from geotrace.ui.theme import Colors, Fonts, Metrics, frosted_rgba


class SettingsPanel(QFrame):
    """浮动设置面板."""

    addDirectory = Signal(str)
    removeDirectory = Signal(str)
    rescanRequested = Signal()
    closeRequested = Signal()
    thumbnailToggleChanged = Signal(bool)
    frostedAlphaChanged = Signal(int)  # 0-100

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("floatingPanel")
        self.setMinimumWidth(180)
        self.setMaximumWidth(250)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(8)

        # 标题栏
        header = QHBoxLayout()
        title = QLabel("设置")
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

        # ── 照片目录 ──
        dir_label = QLabel("照片目录")
        dir_label.setFont(Fonts.title(10))
        layout.addWidget(dir_label)

        self._dir_list = QListWidget()
        self._dir_list.setStyleSheet(f"""
            QListWidget {{
                border: 1px solid {Colors.BORDER_LIGHT};
                border-radius: 4px;
                background: {Colors.INPUT_BG};
                font-size: 12px;
            }}
            QListWidget::item {{
                padding: 4px 8px;
                border-bottom: 1px solid {Colors.BORDER_LIGHT};
            }}
            QListWidget::item:hover {{
                background: {Colors.ACCENT_HOVER_LIGHT};
            }}
        """)
        layout.addWidget(self._dir_list)

        add_btn = QPushButton("+ 添加目录")
        add_btn.setProperty("cssClass", "success")
        add_btn.clicked.connect(self._on_add_directory)
        layout.addWidget(add_btn)

        remove_btn = QPushButton("- 移除选中")
        remove_btn.setProperty("cssClass", "danger")
        remove_btn.clicked.connect(self._on_remove_directory)
        layout.addWidget(remove_btn)

        # ── 分隔 ──
        sep = QLabel()
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background: {Colors.BORDER_LIGHT};")
        layout.addWidget(sep)

        # ── 扫描 ──
        scan_label = QLabel("扫描")
        scan_label.setFont(Fonts.title(10))
        layout.addWidget(scan_label)

        rescan_btn = QPushButton("重新扫描所有目录")
        rescan_btn.setProperty("cssClass", "primary")
        rescan_btn.setStyleSheet("font-size: 13px; padding: 7px 12px;")
        rescan_btn.clicked.connect(self._on_rescan)
        layout.addWidget(rescan_btn)

        self._progress = QProgressBar()
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        # ── 分隔 ──
        sep2 = QLabel()
        sep2.setFixedHeight(1)
        sep2.setStyleSheet(f"background: {Colors.BORDER_LIGHT};")
        layout.addWidget(sep2)

        # ── 显示效果 ──
        fx_label = QLabel("显示效果")
        fx_label.setFont(Fonts.title(10))
        layout.addWidget(fx_label)

        self._thumb_check = QCheckBox("全国视图显示缩略图")
        self._thumb_check.stateChanged.connect(
            lambda state: self.thumbnailToggleChanged.emit(state == Qt.CheckState.Checked.value)
        )
        layout.addWidget(self._thumb_check)

        # 透明度滑块
        slider_row = QHBoxLayout()
        slider_row.addWidget(QLabel("面板透明度"))

        self._alpha_slider = QSlider(Qt.Horizontal)
        self._alpha_slider.setRange(20, 100)
        self._alpha_slider.setValue(85)
        self._alpha_slider.setTickPosition(QSlider.TicksBelow)
        self._alpha_slider.setTickInterval(10)
        self._alpha_slider.valueChanged.connect(self.frostedAlphaChanged.emit)
        slider_row.addWidget(self._alpha_slider)

        self._alpha_value_label = QLabel("85%")
        self._alpha_value_label.setFixedWidth(36)
        self._alpha_value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._alpha_slider.valueChanged.connect(
            lambda v: self._alpha_value_label.setText(f"{v}%")
        )
        slider_row.addWidget(self._alpha_value_label)
        layout.addLayout(slider_row)

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

    def set_frosted_alpha(self, alpha: float) -> None:
        """动态刷新面板自身背景（用于滑块调节时实时预览）."""
        bg = frosted_rgba(alpha)
        self.setStyleSheet(f"""
            QFrame#floatingPanel {{
                background: {bg};
                border: 1px solid {Colors.BORDER_LIGHT};
                border-radius: {Metrics.BORDER_RADIUS_MD}px;
            }}
        """)

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
