"""左侧浮动侧边栏 — 毛玻璃 + 分段控件(省份/照片切换)."""

from PySide6.QtCore import QSize, Qt, QTimer, QRect, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
    QButtonGroup,
)

from geotrace.database.manager import DatabaseManager
from geotrace.ui.blur_engine import (
    BackdropBlurCapture,
    FrostedSurfacePainter,
)
from geotrace.ui.material import REGULAR
from geotrace.ui.photo_grid import PhotoGrid
from geotrace.ui.province_list import ProvinceListPanel
from geotrace.ui.theme import CloseButton, Colors, Fonts, Metrics


class FloatingSidebar(QFrame):
    """浮动侧边栏: 毛玻璃容器 + 分段控件切换省份列表/照片网格.

    毛玻璃背板由 paintEvent 渲染，贯穿整个侧边栏，无纯色填充层。
    """

    provinceClicked = Signal(str)
    closeRequested = Signal()

    def __init__(self, db: DatabaseManager, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("floatingSidebar")
        self.setMinimumWidth(260)
        self.setMaximumWidth(500)

        # ── 毛玻璃引擎 ──
        self._tier = REGULAR
        self._frosted_alpha: float = self._tier.tint_alpha
        self._blur_capture: BackdropBlurCapture | None = None
        self._capture_pending = False
        self._live_capturing = False
        self._frosted_painter = None

        self.setStyleSheet(
            "QFrame#floatingSidebar { background: transparent; border: none; }"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── 拖拽调整宽度手柄 ──
        self._resize_handle = QWidget()
        self._resize_handle.setFixedWidth(6)
        self._resize_handle.setCursor(Qt.SizeHorCursor)
        self._resize_handle.setStyleSheet("background: transparent;")
        self._resize_handle.installEventFilter(self)

        # ── 顶部栏（仅关闭按钮） ──
        top_bar = QFrame()
        top_bar.setFixedHeight(36)
        top_bar.setStyleSheet("background: transparent; border: none;")
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(8, 0, 4, 0)
        top_layout.setSpacing(0)

        close_btn = CloseButton()
        close_btn.clicked.connect(self.closeRequested.emit)
        top_layout.addWidget(close_btn)
        top_layout.addStretch()

        # 顶部栏 + 手柄水平排列
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 4, 8, 0)
        header_layout.setSpacing(0)
        header_layout.addWidget(top_bar)
        header_layout.addWidget(self._resize_handle)
        layout.addLayout(header_layout)

        # ── 内容区（分段控件 + 内容栈） ──
        content_layout = QVBoxLayout()
        content_layout.setContentsMargins(12, 0, 12, 12)
        content_layout.setSpacing(6)

        # 分段控件 — 透明背景，让 paintEvent 的滑块指示器穿透
        self._seg_container = QFrame()
        self._seg_container.setFixedHeight(36)
        self._seg_container.setAttribute(Qt.WA_TranslucentBackground)
        self._seg_container.setStyleSheet("background: transparent; border: none;")

        # 两个切换按钮
        self._btn_provinces = QPushButton("省份")
        self._btn_photos = QPushButton("照片")
        for btn in (self._btn_provinces, self._btn_photos):
            btn.setCheckable(False)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFont(Fonts.ui(12))
            btn.setStyleSheet("""
                QPushButton {
                    background: transparent;
                    border: none;
                    color: %s;
                    padding: 0;
                }
            """ % Colors.TEXT_PRIMARY)

        self._btn_group = QButtonGroup(self)
        self._btn_group.addButton(self._btn_provinces, 0)
        self._btn_group.addButton(self._btn_photos, 1)
        self._btn_group.setExclusive(True)
        self._btn_provinces.setChecked(True)

        seg_layout = QHBoxLayout(self._seg_container)
        seg_layout.setContentsMargins(0, 0, 0, 0)
        seg_layout.setSpacing(0)

        # 透明占位，给滑块指示器留出绘制空间
        seg_layout.addSpacing(2)

        btn_wrapper_left = QFrame()
        btn_wrapper_left.setStyleSheet("background: transparent;")
        btn_wrapper_left.setLayout(QHBoxLayout())
        btn_wrapper_left.layout().setContentsMargins(0, 0, 0, 0)
        btn_wrapper_left.layout().setSpacing(0)
        btn_wrapper_left.layout().addWidget(self._btn_provinces)
        btn_wrapper_left.layout().addStretch()

        btn_wrapper_right = QFrame()
        btn_wrapper_right.setStyleSheet("background: transparent;")
        btn_wrapper_right.setLayout(QHBoxLayout())
        btn_wrapper_right.layout().setContentsMargins(0, 0, 0, 0)
        btn_wrapper_right.layout().setSpacing(0)
        btn_wrapper_right.layout().addWidget(self._btn_photos)
        btn_wrapper_right.layout().addStretch()

        seg_layout.addWidget(btn_wrapper_left, 1)
        seg_layout.addWidget(btn_wrapper_right, 1)
        seg_layout.addSpacing(2)

        # 滑块指示器（paintEvent 绘制）
        self._slider_pos = 0.0  # 0.0=省份, 1.0=照片
        self._target_slider_pos = 0.0
        self._slider_animation = None

        self._btn_group.idClicked.connect(self._on_segment_changed)

        content_layout.addWidget(self._seg_container)

        # 内容栈（省份列表 / 照片网格）
        self._content_stack = QStackedWidget()
        self._content_stack.setStyleSheet("""
            QStackedWidget {
                border: none;
                background: transparent;
            }
        """)

        self._province_list = ProvinceListPanel(frosted=False)
        self._province_list.provinceClicked.connect(self.provinceClicked.emit)
        for child in self._province_list.findChildren(CloseButton):
            child.setVisible(False)
        self._content_stack.addWidget(self._province_list)

        self._photo_grid = PhotoGrid(db)
        self._content_stack.addWidget(self._photo_grid)

        content_layout.addWidget(self._content_stack, 1)

        layout.addLayout(content_layout)

        self.hide()

    # ------------------------------------------------------------------
    # 拖拽调整宽度
    # ------------------------------------------------------------------

    def eventFilter(self, obj, event) -> bool:
        if obj is self._resize_handle:
            et = event.type()
            if et == event.Type.MouseButtonPress:
                if event.button() == Qt.LeftButton:
                    self._resize_start_x = event.globalPosition().x()
                    self._resize_start_w = self.width()
                    return True
            elif et == event.Type.MouseMove:
                if event.buttons() & Qt.LeftButton:
                    dx = event.globalPosition().x() - self._resize_start_x
                    new_w = max(260, min(500, self._resize_start_w + dx))
                    self.setFixedWidth(new_w)
                    self.resize(new_w, self.height())
                    self._invalidate_blur()
                    return True
            elif et == event.Type.MouseButtonRelease:
                if event.button() == Qt.LeftButton:
                    self._schedule_backdrop_capture()
                    return True
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------
    # 分段切换
    # ------------------------------------------------------------------

    def _on_segment_changed(self, index: int) -> None:
        """分段按钮点击切换内容."""
        self._content_stack.setCurrentIndex(index)
        self._target_slider_pos = float(index)
        self._animate_slider()

    def _animate_slider(self) -> None:
        """简单线性动画滑块指示器."""
        self._slider_animation = True

        def _step():
            if not self._slider_animation:
                return
            diff = self._target_slider_pos - self._slider_pos
            if abs(diff) < 0.01:
                self._slider_pos = self._target_slider_pos
                self._slider_animation = False
                self._seg_container.update()
                return
            self._slider_pos += diff * 0.25
            self._seg_container.update()
            QTimer.singleShot(16, _step)

        self._slider_animation = True
        QTimer.singleShot(16, _step)

    # ------------------------------------------------------------------
    # paintEvent 滑块指示器
    # ------------------------------------------------------------------

    def _draw_slider_indicator(self, painter: QPainter) -> None:
        """绘制药丸形滑块指示器 — iOS 风格胶囊选中背景."""
        container = self._seg_container
        w = container.width()
        h = container.height()

        seg_count = 2
        seg_width = (w - 8) // seg_count
        # Pill shape: slight inset, nearly full height
        pill_x = 4 + int(self._slider_pos * seg_width)
        pill_y = 3
        pill_w = seg_width
        pill_h = h - 6
        pill_r = pill_h / 2.0  # half-height = pill shape

        path = QPainterPath()
        path.addRoundedRect(QRect(pill_x, pill_y, pill_w, pill_h),
                           pill_r, pill_r)

        # 药丸填充 — 足够不透明以衬托深色文字
        tint = QColor(255, 255, 255, 160)
        painter.fillPath(path, tint)

        # 动态更新按钮选中颜色
        for i, btn in enumerate([self._btn_provinces, self._btn_photos]):
            if i == int(round(self._slider_pos)):
                btn.setStyleSheet("""
                    QPushButton {
                        background: transparent;
                        border: none;
                        color: %s;
                        font-weight: bold;
                        padding: 0;
                    }
                """ % Colors.TEXT_PRIMARY)
            else:
                btn.setStyleSheet("""
                    QPushButton {
                        background: transparent;
                        border: none;
                        color: %s;
                        font-weight: normal;
                        padding: 0;
                    }
                """ % Colors.TEXT_PRIMARY)

    # ------------------------------------------------------------------
    # 毛玻璃渲染
    # ------------------------------------------------------------------

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self.parent() and not self._blur_capture:
            self._blur_capture = BackdropBlurCapture.from_tier(
                self, self._tier, capture_target=self.parent()
            )
        self._init_frosted_painter()
        # 初始截图由外部调用 capture_backdrop_now() 触发，
        # 避免在滑入动画完成前截图到错误位置

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

    def _init_frosted_painter(self) -> None:
        if self._frosted_painter is None:
            self._frosted_painter = FrostedSurfacePainter.from_tier(self._tier)

    def paintEvent(self, event) -> None:
        """渲染毛玻璃背板 + 滑块指示器."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # 毛玻璃背景
        backdrop = None
        if self._blur_capture is not None:
            geo = self.geometry()
            if not geo.isEmpty() and geo.width() > 0 and geo.height() > 0:
                if (self._blur_capture._cached_geo == geo
                        and self._blur_capture._cached_pixmap
                        and not self._blur_capture._cached_pixmap.isNull()
                        and not self._capture_pending):
                    backdrop = self._blur_capture._cached_pixmap

        if self._frosted_painter is None:
            self._init_frosted_painter()

        tint_alpha = int(self._frosted_alpha * 255)
        self._frosted_painter.tint = QColor(255, 250, 242, tint_alpha)
        self._frosted_painter.paint(painter, self.rect(), backdrop)

        # 滑块指示器（在内容区左上角区域绘制）
        self._draw_slider_indicator(painter)

        painter.end()
        super().paintEvent(event)

    def _schedule_backdrop_capture(self) -> None:
        if self.isHidden() or self._capture_pending or self._live_capturing:
            return

        self._capture_pending = True

        def _capture() -> None:
            self._capture_pending = False
            if self._blur_capture is not None and not self.isHidden():
                # Force fresh capture by bypassing geometry cache
                saved = self._blur_capture._cached_geo
                self._blur_capture._cached_geo = None
                self._blur_capture.capture()
                if self._blur_capture._cached_pixmap is None:
                    self._blur_capture._cached_geo = saved
                self.update()

        QTimer.singleShot(0, _capture)

    def _invalidate_blur(self) -> None:
        if self._blur_capture:
            self._blur_capture.invalidate()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._invalidate_blur()
        self._schedule_backdrop_capture()

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def set_frosted_alpha(self, alpha: float) -> None:
        self._frosted_alpha = max(0.0, min(1.0, alpha))
        if self._blur_capture:
            self._blur_capture.invalidate()
        self._schedule_backdrop_capture()
        self.update()

    def request_backdrop_refresh(self) -> None:
        self._schedule_backdrop_capture()

    def request_backdrop_live(self) -> None:
        """Real-time Liquid Glass refresh — every other frame during drag/zoom.

        Downsampled 2× capture + frame skip = ~8× less GPU load than
        full-resolution every frame. Human perception cannot distinguish
        30 fps glass updates from 60 fps during rapid map movement.
        """
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
            # Update shader time for dynamic Liquid Glass effects
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

    def apply_live_backdrop(self, raw_pixmap: QPixmap,
                            target_size: QSize) -> None:
        """Apply Liquid Glass refraction to a shared raw backdrop capture.

        Used by main_window when both panels are visible: one
        grabFramebuffer serves both sidebars.
        """
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

    def switch_to_photos_tab(self) -> None:
        self._btn_photos.setChecked(True)
        self._on_segment_changed(1)

    def switch_to_provinces_tab(self) -> None:
        self._btn_provinces.setChecked(True)
        self._on_segment_changed(0)

    def refresh_province_list(self, stats: list[dict]) -> None:
        self._province_list.refresh(stats)