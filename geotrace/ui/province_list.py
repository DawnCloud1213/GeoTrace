"""左侧省份列表面板 — 浮动覆盖在地图上, 真实毛玻璃背板."""

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from geotrace.ui.blur_engine import (
    BackdropBlurCapture,
    FrostedSurfacePainter,
    generate_noise_pixmap,
)
from geotrace.ui.theme import Colors, Fonts, Metrics


class ProvinceListPanel(QFrame):
    """浮动省份列表: 按照片数降序, 点击切换到对应省份."""

    provinceClicked = Signal(str)
    closeRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._stats: list[dict] = []
        self.setObjectName("floatingPanel")
        self.setMinimumWidth(170)

        # ── 毛玻璃引擎 ──
        self._frosted_alpha: float = 0.63
        self._blur_capture: BackdropBlurCapture | None = None
        self._noise_pixmap = generate_noise_pixmap(250, 400, opacity=0.04)
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
        header.addWidget(title)
        header.addStretch()
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(24, 24)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setProperty("cssClass", "ghost")
        close_btn.clicked.connect(self.closeRequested.emit)
        header.addWidget(close_btn)
        layout.addLayout(header)

        # 列表 (半透明背景以融入毛玻璃)
        self._list = QListWidget()
        self._list.setAlternatingRowColors(True)
        self._list.setStyleSheet(f"""
            QListWidget {{
                border: 1px solid rgba(232,224,208,0.40);
                border-radius: 4px;
                background: rgba(250,250,245,0.50);
                font-size: 13px;
            }}
            QListWidget::item {{
                padding: 5px 10px;
                border-bottom: 1px solid rgba(232,224,208,0.30);
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
        if self.parent() and not self._blur_capture:
            self._blur_capture = BackdropBlurCapture(self, blur_radius=25)
        self._schedule_backdrop_capture()

    def paintEvent(self, event) -> None:
        """渲染毛玻璃背板: 模糊背景(如有缓存) → 着色 → 噪点 → 边框.

        注意: 决不在 paintEvent 内调用 capture() — parent.grab() 会触发
        递归 paintEvent, 导致 C++ 层栈溢出。
        """
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
        if self.isHidden():
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
        self._frosted_alpha = max(0.0, min(1.0, alpha))
        if self._blur_capture:
            self._blur_capture.invalidate()
        w, h = self.width(), self.height()
        if w > 0 and h > 0:
            noise_opacity = 0.02 + self._frosted_alpha * 0.03
            self._noise_pixmap = generate_noise_pixmap(w, h, opacity=noise_opacity)
        self._schedule_backdrop_capture()
        self.update()

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        name = item.data(Qt.UserRole)
        if name:
            self.provinceClicked.emit(name)
