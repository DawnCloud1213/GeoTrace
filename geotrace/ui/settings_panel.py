"""右侧设置面板 — 浮动覆盖在地图上, 真实毛玻璃背板."""

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
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

from geotrace.ui.blur_engine import (
    BackdropBlurCapture,
    FrostedSurfacePainter,
    generate_noise_pixmap,
)
from geotrace.ui.theme import Colors, Fonts, Metrics


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

        # ── 毛玻璃引擎 ──
        self._frosted_alpha: float = 0.63
        self._blur_capture: BackdropBlurCapture | None = None
        self._capture_pending = False
        self._noise_pixmap = generate_noise_pixmap(250, 500, opacity=0.04)
        self._frosted_painter = FrostedSurfacePainter(
            tint_color=QColor(255, 255, 255, int(self._frosted_alpha * 255)),
            border_color=QColor(Colors.FROSTED_TINT_R, Colors.FROSTED_TINT_G, Colors.FROSTED_TINT_B, 40),
            border_radius=float(Metrics.BORDER_RADIUS_MD),
        )

        # 覆盖 GLOBAL_QSS 的白色背景 — 毛玻璃由 paintEvent 渲染
        self.setStyleSheet(
            "QFrame#floatingPanel { background: transparent; border: none; }"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(8)

        # 标题栏
        header = QHBoxLayout()
        title = QLabel("设置")
        title.setFont(Fonts.title(11))
        title.setStyleSheet(f"color: {Colors.TEXT_PRIMARY};")
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
        dir_label.setStyleSheet(f"color: {Colors.TEXT_PRIMARY};")
        layout.addWidget(dir_label)

        self._dir_list = QListWidget()
        self._dir_list.setStyleSheet(f"""
            QListWidget {{
                border: 1px solid rgba(232,224,208,0.40);
                border-radius: 4px;
                background: rgba(250,250,245,0.50);
                font-size: 12px;
                color: {Colors.TEXT_PRIMARY};
            }}
            QListWidget::item {{
                padding: 4px 8px;
                border-bottom: 1px solid rgba(232,224,208,0.30);
                color: {Colors.TEXT_PRIMARY};
            }}
            QListWidget::item:hover {{
                background: rgba(255,243,224,0.60);
            }}
        """)
        layout.addWidget(self._dir_list)

        add_btn = QPushButton("+ 添加目录")
        add_btn.setProperty("cssClass", "success")
        add_btn.style().unpolish(add_btn)
        add_btn.style().polish(add_btn)
        add_btn.setStyleSheet("color: #503214; font-weight: bold;")
        add_btn.clicked.connect(self._on_add_directory)
        layout.addWidget(add_btn)

        remove_btn = QPushButton("- 移除选中")
        remove_btn.setProperty("cssClass", "danger")
        remove_btn.style().unpolish(remove_btn)
        remove_btn.style().polish(remove_btn)
        remove_btn.setStyleSheet("color: #FFFFFF; font-weight: bold; background-color: #B5432E; border: none;")
        remove_btn.clicked.connect(self._on_remove_directory)
        layout.addWidget(remove_btn)

        # ── 分隔 ──
        sep = QLabel()
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background: rgba(232,224,208,0.35);")
        layout.addWidget(sep)

        # ── 扫描 ──
        scan_label = QLabel("扫描")
        scan_label.setFont(Fonts.title(10))
        scan_label.setStyleSheet(f"color: {Colors.TEXT_PRIMARY};")
        layout.addWidget(scan_label)

        rescan_btn = QPushButton("重新扫描所有目录")
        rescan_btn.setProperty("cssClass", "primary")
        rescan_btn.style().unpolish(rescan_btn)
        rescan_btn.style().polish(rescan_btn)
        rescan_btn.setStyleSheet(f"font-size: 13px; padding: 7px 12px; color: #FFFFFF; background-color: {Colors.ACCENT_PRIMARY}; border: none;")
        rescan_btn.clicked.connect(self._on_rescan)
        layout.addWidget(rescan_btn)

        self._progress = QProgressBar()
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        # ── 分隔 ──
        sep2 = QLabel()
        sep2.setFixedHeight(1)
        sep2.setStyleSheet(f"background: rgba(232,224,208,0.35);")
        layout.addWidget(sep2)

        # ── 显示效果 ──
        fx_label = QLabel("显示效果")
        fx_label.setFont(Fonts.title(10))
        fx_label.setStyleSheet(f"color: {Colors.TEXT_PRIMARY};")
        layout.addWidget(fx_label)

        self._thumb_check = QCheckBox("全国视图显示缩略图")
        self._thumb_check.setStyleSheet(f"color: {Colors.TEXT_PRIMARY};")
        self._thumb_check.stateChanged.connect(
            lambda state: self.thumbnailToggleChanged.emit(
                int(state) == int(Qt.CheckState.Checked)
            )
        )
        layout.addWidget(self._thumb_check)

        # 透明度滑块
        slider_row = QHBoxLayout()
        slider_label = QLabel("面板透明度")
        slider_label.setStyleSheet(f"color: {Colors.TEXT_PRIMARY};")
        slider_row.addWidget(slider_label)

        self._alpha_slider = QSlider(Qt.Horizontal)
        self._alpha_slider.setRange(30, 100)
        self._alpha_slider.setValue(63)
        self._alpha_slider.setTickPosition(QSlider.TicksBelow)
        self._alpha_slider.setTickInterval(10)
        self._alpha_slider.valueChanged.connect(self.frostedAlphaChanged.emit)
        slider_row.addWidget(self._alpha_slider)

        self._alpha_value_label = QLabel("63%")
        self._alpha_value_label.setFixedWidth(36)
        self._alpha_value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._alpha_value_label.setStyleSheet(f"color: {Colors.TEXT_PRIMARY};")
        self._alpha_slider.valueChanged.connect(
            lambda v: self._alpha_value_label.setText(f"{v}%")
        )
        slider_row.addWidget(self._alpha_value_label)
        layout.addLayout(slider_row)

        layout.addStretch()
        self.hide()

    # ------------------------------------------------------------------
    # 毛玻璃渲染
    # ------------------------------------------------------------------

    def showEvent(self, event) -> None:
        """首次显示时初始化模糊捕获引擎 & 异步抓取背景."""
        super().showEvent(event)
        if self.parent() and not self._blur_capture:
            self._blur_capture = BackdropBlurCapture(
                self, blur_radius=25, capture_target=self.parent()
            )
        self._schedule_backdrop_capture()

    def paintEvent(self, event) -> None:
        """渲染毛玻璃背板: 模糊背景(如有缓存) → 着色 → 噪点 → 边框.

        注意: 决不在 paintEvent 内调用 capture() — parent.grab() 会触发
        递归 paintEvent, 导致 C++ 层栈溢出。
        """
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        backdrop = None
        if self._blur_capture is not None:
            geo = self.geometry()
            if not geo.isEmpty() and geo.width() > 0 and geo.height() > 0:
                if (self._blur_capture._cached_geo == geo
                        and self._blur_capture._cached_pixmap
                        and not self._blur_capture._cached_pixmap.isNull()
                        and not self._capture_pending):
                    backdrop = self._blur_capture._cached_pixmap

        tint_alpha = int(self._frosted_alpha * 255)
        self._frosted_painter.tint = QColor(255, 255, 255, tint_alpha)
        self._frosted_painter.noise = self._noise_pixmap
        self._frosted_painter.paint(painter, self.rect(), backdrop)

        painter.end()
        super().paintEvent(event)

    def _schedule_backdrop_capture(self) -> None:
        """异步抓取父控件背景 — 避免在 paintEvent 中递归 grab().

        合并式刷新：同时最多只有一次待处理的捕获。
        """
        if self.isHidden() or self._capture_pending:
            return

        self._capture_pending = True

        def _capture() -> None:
            self._capture_pending = False
            if self._blur_capture is not None and not self.isHidden():
                self._blur_capture.invalidate()
                self._blur_capture.capture()
                self.update()

        QTimer.singleShot(0, _capture)

    def resizeEvent(self, event) -> None:
        """大小变化时失效模糊缓存 & 重新生成噪点并异步重抓取."""
        super().resizeEvent(event)
        if self._blur_capture:
            self._blur_capture.invalidate()
        w, h = self.width(), self.height()
        if w > 0 and h > 0:
            noise_opacity = 0.02 + self._frosted_alpha * 0.03
            self._noise_pixmap = generate_noise_pixmap(w, h, opacity=noise_opacity)
        self._schedule_backdrop_capture()

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
        """更新毛玻璃透明度并刷新."""
        self._frosted_alpha = max(0.0, min(1.0, alpha))
        if self._blur_capture:
            self._blur_capture.invalidate()
        w, h = self.width(), self.height()
        if w > 0 and h > 0:
            noise_opacity = 0.02 + self._frosted_alpha * 0.03
            self._noise_pixmap = generate_noise_pixmap(w, h, opacity=noise_opacity)
        self._schedule_backdrop_capture()
        self.update()

    def request_backdrop_refresh(self) -> None:
        """外部请求刷新毛玻璃背景（拖拽/缩放后防抖调用）."""
        self._schedule_backdrop_capture()

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
