"""左侧浮动侧边栏 — 毛玻璃 + 分段控件(省份/照片切换)."""

from PySide6.QtCore import Qt, QTimer, QRect, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
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
    generate_noise_pixmap_multiscale,
)
from geotrace.ui.photo_grid import PhotoGrid
from geotrace.ui.province_list import ProvinceListPanel
from geotrace.ui.theme import Colors, Fonts, Metrics


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
        self._frosted_alpha: float = 0.63
        self._blur_capture: BackdropBlurCapture | None = None
        self._capture_pending = False
        self._noise_pixmap = None
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
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(8, 0, 4, 0)
        top_layout.setSpacing(0)

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(24, 24)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setProperty("cssClass", "ghost")
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
            """ % Colors.TEXT_SECONDARY)

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
        for child in self._province_list.findChildren(QPushButton):
            if child.text() == "✕":
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
        """在分段按钮下方绘制毛玻璃滑块指示器."""
        container = self._seg_container
        w = container.width()
        h = container.height()

        seg_count = 2
        seg_width = (w - 4) // seg_count
        slider_w = seg_width - 4
        slider_h = 4
        slider_y = h - slider_h - 2
        slider_x = 2 + int(self._slider_pos * seg_width)

        path = QPainterPath()
        r = Metrics.BORDER_RADIUS_SM
        path.addRoundedRect(QRect(slider_x, slider_y, slider_w, slider_h), r, r)

        tint = QColor(Colors.FROSTED_TINT_R, Colors.FROSTED_TINT_G,
                      Colors.FROSTED_TINT_B, 160)
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
                """ % Colors.TEXT_SECONDARY)

    # ------------------------------------------------------------------
    # 毛玻璃渲染
    # ------------------------------------------------------------------

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self.parent() and not self._blur_capture:
            self._blur_capture = BackdropBlurCapture(
                self, blur_radius=25, capture_target=self.parent()
            )
        self._init_frosted_painter()
        self._schedule_backdrop_capture()

    def _init_frosted_painter(self) -> None:
        if self._frosted_painter is None:
            self._frosted_painter = FrostedSurfacePainter(
                tint_color=QColor(255, 255, 255, int(self._frosted_alpha * 255)),
                border_color=QColor(Colors.FROSTED_TINT_R, Colors.FROSTED_TINT_G,
                                    Colors.FROSTED_TINT_B, 40),
                border_radius=float(Metrics.BORDER_RADIUS_MD),
            )
        w, h = self.width(), self.height()
        if w > 0 and h > 0:
            self._noise_pixmap = generate_noise_pixmap_multiscale(w, h)

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
        self._frosted_painter.tint = QColor(255, 255, 255, tint_alpha)
        self._frosted_painter.noise = self._noise_pixmap
        self._frosted_painter.paint(painter, self.rect(), backdrop)

        # 滑块指示器（在内容区左上角区域绘制）
        self._draw_slider_indicator(painter)

        painter.end()
        super().paintEvent(event)

    def _schedule_backdrop_capture(self) -> None:
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

    def _invalidate_blur(self) -> None:
        if self._blur_capture:
            self._blur_capture.invalidate()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._invalidate_blur()
        w, h = self.width(), self.height()
        if w > 0 and h > 0:
            noise_total = 0.02 + self._frosted_alpha * 0.03
            self._noise_pixmap = generate_noise_pixmap_multiscale(
                w, h,
                fine_opacity=noise_total * 0.625,
                coarse_opacity=noise_total * 0.375,
            )
        self._schedule_backdrop_capture()

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def set_frosted_alpha(self, alpha: float) -> None:
        self._frosted_alpha = max(0.0, min(1.0, alpha))
        if self._blur_capture:
            self._blur_capture.invalidate()
        w, h = self.width(), self.height()
        if w > 0 and h > 0:
            noise_total = 0.02 + self._frosted_alpha * 0.03
            self._noise_pixmap = generate_noise_pixmap_multiscale(
                w, h,
                fine_opacity=noise_total * 0.625,
                coarse_opacity=noise_total * 0.375,
            )
        self._schedule_backdrop_capture()
        self.update()

    def request_backdrop_refresh(self) -> None:
        self._schedule_backdrop_capture()

    def switch_to_photos_tab(self) -> None:
        self._btn_photos.setChecked(True)
        self._on_segment_changed(1)

    def switch_to_provinces_tab(self) -> None:
        self._btn_provinces.setChecked(True)
        self._on_segment_changed(0)

    def refresh_province_list(self, stats: list[dict]) -> None:
        self._province_list.refresh(stats)