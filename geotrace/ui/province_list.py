"""左侧省份列表面板 — 浮动覆盖在地图上, 真实毛玻璃背板."""

from PySide6.QtCore import QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStyledItemDelegate,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from geotrace.ui.blur_engine import (
    BackdropBlurCapture,
    FrostedSurfacePainter,
    generate_noise_pixmap_multiscale,
)
from geotrace.ui.theme import Colors, Fonts, Metrics, CloseButton


class _RoundedItemDelegate(QStyledItemDelegate):
    """省份列表圆角药丸形 item 背景 — QSS 不支持 QListWidget::item 圆角."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._radius = 10

    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)

        rect = option.rect.adjusted(2, 1, -2, -1)

        # 圆角背景 — 低不透明度融入毛玻璃
        if option.state & QStyle.State_Selected:
            bg = QColor(255, 204, 128, 180)
        elif option.state & QStyle.State_MouseOver:
            bg = QColor(255, 243, 224, 120)
        elif index.row() % 2 == 1:
            bg = QColor(255, 252, 250, 30)
        else:
            bg = QColor(255, 253, 250, 55)

        path = QPainterPath()
        path.addRoundedRect(rect, self._radius, self._radius)
        painter.fillPath(path, bg)

        # 文本
        text = index.data(Qt.DisplayRole)
        if text:
            painter.setPen(QColor(Colors.TEXT_PRIMARY))
            font = painter.font()
            font.setPixelSize(13)
            painter.setFont(font)
            painter.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter, text)

        painter.restore()

    def sizeHint(self, option, index) -> QSize:
        fm = option.fontMetrics
        return QSize(200, fm.height() + 14)


class ProvinceListPanel(QFrame):
    """浮动省份列表: 按照片数降序, 点击切换到对应省份."""

    provinceClicked = Signal(str)
    closeRequested = Signal()

    def __init__(self, parent=None, capture_target: QWidget | None = None,
                 frosted: bool = True) -> None:
        super().__init__(parent)
        self._capture_target = capture_target
        self._frosted = frosted
        self._stats: list[dict] = []
        self.setObjectName("floatingPanel")
        self.setMinimumWidth(170)

        # ── 毛玻璃引擎 ──
        self._frosted_alpha: float = 0.63
        self._blur_capture: BackdropBlurCapture | None = None
        self._noise_pixmap = None
        self._frosted_painter = None
        if self._frosted:
            self._noise_pixmap = generate_noise_pixmap_multiscale(250, 400)
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
        layout.setSpacing(6)

        # 标题栏
        header = QHBoxLayout()
        title = QLabel("省份")
        title.setFont(Fonts.title(11))
        title.setProperty("cssClass", "sectionLabel")
        header.addWidget(title)
        header.addStretch()
        close_btn = CloseButton()
        close_btn.clicked.connect(self.closeRequested.emit)
        header.addWidget(close_btn)
        layout.addLayout(header)

        # 列表 (半透明背景以融入毛玻璃)
        self._list = QListWidget()
        self._list.setItemDelegate(_RoundedItemDelegate(self._list))
        self._list.setAlternatingRowColors(True)
        self._list.setStyleSheet(f"""
            QListWidget {{
                border: 1px solid rgba(232,224,208,0.50);
                border-radius: 12px;
                background: rgba(255,253,250,0.20);
                font-size: 13px;
                padding: 4px;
            }}
            QListWidget::item {{
                padding: 6px 12px;
                border: none;
                border-radius: 10px;
            }}
            QListWidget::item:hover {{
                background: rgba(255,243,224,0.60);
            }}
            QListWidget::item:selected {{
                background: rgba(255,204,128,0.70);
                color: {Colors.TEXT_PRIMARY};
            }}
            QListWidget::item:alternate {{
                background: rgba(245,240,232,0.30);
            }}
        """)
        self._list.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self._list)

        self.hide()

    # ------------------------------------------------------------------
    # 毛玻璃渲染
    # ------------------------------------------------------------------

    def showEvent(self, event) -> None:
        """首次显示时初始化模糊捕获引擎 & 异步抓取背景."""
        super().showEvent(event)
        if self._frosted and self.parent() and not self._blur_capture:
            self._blur_capture = BackdropBlurCapture(
                self, blur_radius=25, capture_target=self._capture_target,
            )
        if self._frosted:
            self._schedule_backdrop_capture()

    def paintEvent(self, event) -> None:
        """渲染毛玻璃背板: 模糊背景(如有缓存) → 着色 → 噪点 → 边框.

        注意: 决不在 paintEvent 内调用 capture() — parent.grab() 会触发
        递归 paintEvent, 导致 C++ 层栈溢出。
        """
        if not self._frosted:
            super().paintEvent(event)
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # 仅使用已缓存的 backdrop, 不在此调用 capture()
        backdrop = None
        if self._blur_capture is not None:
            geo = self.geometry()
            if not geo.isEmpty() and geo.width() > 0 and geo.height() > 0:
                if (self._blur_capture._cached_geo == geo
                        and self._blur_capture._cached_pixmap
                        and not self._blur_capture._cached_pixmap.isNull()):
                    backdrop = self._blur_capture._cached_pixmap

        tint_alpha = int(self._frosted_alpha * 255)
        self._frosted_painter.tint = QColor(255, 255, 255, tint_alpha)
        self._frosted_painter.noise = self._noise_pixmap
        self._frosted_painter.paint(painter, self.rect(), backdrop)

        painter.end()

        super().paintEvent(event)

    def _schedule_backdrop_capture(self) -> None:
        """异步抓取父控件背景 — 避免在 paintEvent 中递归 grab()."""
        if not self._frosted or self.isHidden():
            return

        def _capture() -> None:
            if self._blur_capture is not None and not self.isHidden():
                self._blur_capture.invalidate()
                self._blur_capture.capture()
                self.update()

        QTimer.singleShot(50, _capture)

    def resizeEvent(self, event) -> None:
        """大小变化时失效模糊缓存 & 重新生成噪点并异步重抓取."""
        super().resizeEvent(event)
        if not self._frosted:
            return
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

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

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
        """更新毛玻璃透明度并刷新."""
        if not self._frosted:
            return
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

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        name = item.data(Qt.UserRole)
        if name:
            self.provinceClicked.emit(name)
