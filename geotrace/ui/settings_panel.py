"""右侧设置面板 — 浮动覆盖在地图上, 真实毛玻璃背板."""

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
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
)
from geotrace.ui.material import THIN
from geotrace.ui.province_list import _RoundedItemDelegate
from geotrace.ui.theme import CloseButton, Colors, Fonts, Metrics


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
        self._tier = THIN
        self._frosted_alpha: float = self._tier.tint_alpha
        self._blur_capture: BackdropBlurCapture | None = None
        self._capture_pending = False
        self._live_capturing = False
        self._frosted_painter = FrostedSurfacePainter.from_tier(self._tier)

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
        title.setProperty("cssClass", "sectionLabel")
        header.addWidget(title)
        header.addStretch()
        close_btn = CloseButton()
        close_btn.clicked.connect(self.closeRequested.emit)
        header.addWidget(close_btn)
        layout.addLayout(header)

        # ── 照片目录 ──
        dir_label = QLabel("照片目录")
        dir_label.setFont(Fonts.title(10))
        dir_label.setProperty("cssClass", "sectionLabel")
        layout.addWidget(dir_label)

        self._dir_list = QListWidget()
        self._dir_list.setItemDelegate(_RoundedItemDelegate(self._dir_list))
        self._dir_list.setStyleSheet(f"""
            QListWidget {{
                border: 1px solid rgba(232,224,208,0.50);
                border-radius: 12px;
                background: rgba(255,253,250,0.20);
                font-size: 12px;
                color: {Colors.TEXT_PRIMARY};
                padding: 4px;
            }}
            QListWidget::item {{
                padding: 5px 10px;
                border: none;
                color: {Colors.TEXT_PRIMARY};
            }}
        """)
        layout.addWidget(self._dir_list)

        add_btn = QPushButton("+ 添加目录")
        add_btn.setProperty("cssClass", "success")
        add_btn.setStyleSheet("""
            QPushButton[cssClass="success"] {
                color: #3A1E08; font-weight: bold;
                border-radius: 12px; padding: 6px 12px;
            }
        """)
        add_btn.clicked.connect(self._on_add_directory)
        layout.addWidget(add_btn)

        remove_btn = QPushButton("- 移除选中")
        remove_btn.setProperty("cssClass", "danger")
        remove_btn.setStyleSheet("""
            QPushButton[cssClass="danger"] {
                color: #3A1E08; font-weight: bold;
                border-radius: 12px; padding: 6px 12px;
            }
        """)
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
        scan_label.setProperty("cssClass", "sectionLabel")
        layout.addWidget(scan_label)

        rescan_btn = QPushButton("重新扫描所有目录")
        rescan_btn.setProperty("cssClass", "primary")
        rescan_btn.setStyleSheet(f"""
            QPushButton[cssClass="primary"] {{
                font-size: 13px; padding: 8px 16px;
                border-radius: 12px;
                background-color: {Colors.ACCENT_PRIMARY};
                color: #3A1E08;
                font-weight: bold;
                border: none;
            }}
        """)
        rescan_btn.clicked.connect(self._on_rescan)
        layout.addWidget(rescan_btn)

        # ── 分隔 ──
        sep2 = QLabel()
        sep2.setFixedHeight(1)
        sep2.setStyleSheet(f"background: rgba(232,224,208,0.35);")
        layout.addWidget(sep2)

        # ── 显示效果 ──
        fx_label = QLabel("显示效果")
        fx_label.setFont(Fonts.title(10))
        fx_label.setProperty("cssClass", "sectionLabel")
        layout.addWidget(fx_label)

        self._thumb_check = QCheckBox("全国视图显示缩略图")
        self._thumb_check.setStyleSheet(f"""
            QCheckBox {{
                color: {Colors.TEXT_PRIMARY};
                font-size: 12px;
                background: rgba(255,253,250,0.20);
                border-radius: 6px;
                padding: 4px 8px;
            }}
        """)
        self._thumb_check.toggled.connect(self.thumbnailToggleChanged.emit)
        layout.addWidget(self._thumb_check)

        layout.addStretch()
        self.hide()

    # ------------------------------------------------------------------
    # 毛玻璃渲染
    # ------------------------------------------------------------------

    def showEvent(self, event) -> None:
        """首次显示时初始化模糊捕获引擎."""
        super().showEvent(event)
        if self.parent() and not self._blur_capture:
            self._blur_capture = BackdropBlurCapture.from_tier(
                self, self._tier, capture_target=self.parent()
            )
        # 初始截图由外部调用 capture_backdrop_now() 触发

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        if self._blur_capture:
            self._blur_capture.invalidate()
        if not self._live_capturing:
            self._schedule_backdrop_capture()

    def capture_backdrop_now(self) -> None:
        """同步抓取背景 — 由滑入动画完成信号触发."""
        if self._blur_capture and not self.isHidden():
            self._blur_capture.invalidate()
            self._blur_capture.capture()
            self.update()

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
        self._frosted_painter.tint = QColor(255, 250, 242, tint_alpha)
        self._frosted_painter.paint(painter, self.rect(), backdrop)

        painter.end()
        super().paintEvent(event)

    def _schedule_backdrop_capture(self) -> None:
        """异步抓取父控件背景 — 避免在 paintEvent 中递归 grab().

        合并式刷新：同时最多只有一次待处理的捕获。
        """
        if self.isHidden() or self._capture_pending or self._live_capturing:
            return

        self._capture_pending = True

        def _capture() -> None:
            self._capture_pending = False
            if self._blur_capture is not None and not self.isHidden():
                saved = self._blur_capture._cached_geo
                self._blur_capture._cached_geo = None
                self._blur_capture.capture()
                if self._blur_capture._cached_pixmap is None:
                    self._blur_capture._cached_geo = saved
                self.update()

        QTimer.singleShot(0, _capture)

    def resizeEvent(self, event) -> None:
        """大小变化时失效模糊缓存 & 重新生成噪点并异步重抓取."""
        super().resizeEvent(event)
        if self._blur_capture:
            self._blur_capture.invalidate()
        if not self._live_capturing:
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
        self._schedule_backdrop_capture()
        self.update()

    def apply_live_backdrop(self, raw_pixmap: QPixmap,
                            target_size: "QSize") -> None:
        """Apply Liquid Glass refraction to a shared raw backdrop capture."""
        import time
        if self.isHidden() or self._blur_capture is None:
            return
        if not hasattr(self, '_gpu_time_start'):
            self._gpu_time_start = time.monotonic()
        self._blur_capture._time_sec = time.monotonic() - self._gpu_time_start
        result = self._blur_capture.refract_raw(
            raw_pixmap, live=True, target_size=target_size)
        if result and not result.isNull():
            self._blur_capture._cached_pixmap = result
            self._blur_capture._cached_geo = self.geometry()
            self.update()

    def request_backdrop_refresh(self) -> None:
        """外部请求刷新毛玻璃背景（拖拽/缩放后防抖调用）."""
        self._schedule_backdrop_capture()

    def request_backdrop_live(self) -> None:
        """Real-time Liquid Glass refresh — every other frame during drag/zoom."""
        import time
        if self.isHidden() or self._blur_capture is None:
            return

        # Frame skip: refresh every 2nd frame (~30 fps effective)
        if not hasattr(self, '_live_frame'):
            self._live_frame = 0
        self._live_frame += 1
        if self._live_frame % 2 == 0:
            return

        self._live_capturing = True
        try:
            if not hasattr(self, '_gpu_time_start'):
                self._gpu_time_start = time.monotonic()
            self._blur_capture._time_sec = time.monotonic() - self._gpu_time_start
            backdrop = self._blur_capture.capture_live()
            if backdrop and not backdrop.isNull():
                self._blur_capture._cached_pixmap = backdrop
                self._blur_capture._cached_geo = self.geometry()
                self.update()
        finally:
            self._live_capturing = False

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
