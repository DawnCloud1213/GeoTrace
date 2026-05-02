"""单张照片大图查看对话框 — 滚轮缩放、左键拖拽平移、前后导航."""

import logging
from pathlib import Path
from io import BytesIO

from PIL import Image, ImageOps
from PySide6.QtCore import Qt, QPoint, QEvent
from PySide6.QtGui import QAction, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
)

from geotrace.ui.theme import Colors, Fonts

logger = logging.getLogger(__name__)

_FROSTED_BTN = """
    QPushButton {{
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
            stop:0 rgba(0,0,0,0.38),
            stop:1 rgba(0,0,0,0.22));
        color: rgba(255,255,255,0.80);
        border: 1px solid rgba(255,255,255,0.15);
        border-radius: 8px;
        font-size: 18px;
    }}
    QPushButton:hover {{
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
            stop:0 rgba(0,0,0,0.58),
            stop:1 rgba(0,0,0,0.42));
        color: white;
        border-color: rgba(255,255,255,0.35);
    }}
    QPushButton:disabled {{
        background: rgba(0,0,0,0.10);
        color: rgba(255,255,255,0.15);
        border-color: rgba(255,255,255,0.06);
    }}
"""


class PhotoViewer(QDialog):
    """照片大图查看器 — 滚轮缩放、左键拖拽平移、左右箭头切换."""

    def __init__(
        self, current_path: str, all_paths: list[str],
        current_index: int = 0, parent=None,
    ) -> None:
        super().__init__(parent)
        self._current_index = current_index
        self._all_paths = all_paths
        self._scale_factor = 1.0
        self._pixmap: QPixmap | None = None
        self._panning = False
        self._pan_last = QPoint()

        self.setWindowTitle(Path(current_path).name)
        self.resize(1200, 800)
        self.setMinimumSize(400, 300)
        self.setStyleSheet(f"QDialog {{ background-color: {Colors.MAP_BG}; }}")

        # 图片显示层
        self._label = QLabel()
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setScaledContents(False)

        scroll = QScrollArea()
        scroll.setWidget(self._label)
        scroll.setWidgetResizable(False)
        scroll.setAlignment(Qt.AlignCenter)
        scroll.setFrameShape(QScrollArea.NoFrame)
        self._scroll_area = scroll

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)

        # 浮动切换按钮 (左右两侧, 毛玻璃)
        self._btn_prev = QPushButton("◀", self)
        self._btn_prev.setFixedSize(44, 80)
        self._btn_prev.setCursor(Qt.PointingHandCursor)
        self._btn_prev.setStyleSheet(_FROSTED_BTN)
        self._btn_prev.clicked.connect(self._prev)

        self._btn_next = QPushButton("▶", self)
        self._btn_next.setFixedSize(44, 80)
        self._btn_next.setCursor(Qt.PointingHandCursor)
        self._btn_next.setStyleSheet(_FROSTED_BTN)
        self._btn_next.clicked.connect(self._next)

        # 浮动计数器 (底部居中)
        self._counter_label = QLabel(self)
        self._counter_label.setFont(Fonts.ui(12))
        self._counter_label.setStyleSheet(
            "color: rgba(255,255,255,0.80);"
            "background: qlineargradient(x1:0, y1:0, x2:0, y2:1,"
            "    stop:0 rgba(0,0,0,0.42), stop:1 rgba(0,0,0,0.28));"
            "border: 1px solid rgba(255,255,255,0.12);"
            "border-radius: 10px;"
            "padding: 4px 14px;"
        )
        self._counter_label.setAlignment(Qt.AlignCenter)

        # 快捷键
        for shortcut, slot in [
            (QKeySequence.ZoomIn, self._zoom_in),
            (QKeySequence.ZoomOut, self._zoom_out),
            ("Ctrl+0", self._fit_to_window),
            ("Left", self._prev),
            ("Right", self._next),
            ("Escape", self.close),
        ]:
            action = QAction(self)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(slot)
            self.addAction(action)

        # 事件过滤器: 左键拖拽平移
        scroll.viewport().installEventFilter(self)
        scroll.viewport().setCursor(Qt.OpenHandCursor)

        self._load_image()

    # ------------------------------------------------------------------
    # 事件过滤器 — 左键拖拽平移 + 滚轮无级缩放
    # ------------------------------------------------------------------

    def eventFilter(self, obj, event) -> bool:
        vp = self._scroll_area.viewport()
        if obj is not vp:
            return super().eventFilter(obj, event)

        if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            self._panning = True
            self._pan_last = event.globalPosition().toPoint()
            vp.setCursor(Qt.ClosedHandCursor)
            return True

        if event.type() == QEvent.MouseMove and self._panning:
            cur = event.globalPosition().toPoint()
            delta = cur - self._pan_last
            self._pan_last = cur
            hb = self._scroll_area.horizontalScrollBar()
            vb = self._scroll_area.verticalScrollBar()
            hb.setValue(hb.value() - delta.x())
            vb.setValue(vb.value() - delta.y())
            return True

        if event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
            self._panning = False
            vp.setCursor(Qt.OpenHandCursor)
            return True

        if event.type() == QEvent.Wheel:
            self._handle_wheel_zoom(event)
            return True

        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------
    # 浮动控件定位
    # ------------------------------------------------------------------

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._layout_overlays()
        if self._scale_factor <= 1.01:
            self._fit_to_window()

    def _layout_overlays(self) -> None:
        w, h = self.width(), self.height()
        self._btn_prev.move(12, h // 2 - 40)
        self._btn_next.move(w - 56, h // 2 - 40)
        self._counter_label.adjustSize()
        cw = self._counter_label.width()
        self._counter_label.move(w // 2 - cw // 2, h - 44)

    # ------------------------------------------------------------------
    # 导航
    # ------------------------------------------------------------------

    def _update_nav_state(self) -> None:
        total = len(self._all_paths)
        idx = self._current_index
        self._counter_label.setText(f"{idx + 1} / {total}")
        self._btn_prev.setEnabled(idx > 0)
        self._btn_next.setEnabled(idx < total - 1)
        self.setWindowTitle(Path(self._all_paths[idx]).name)

    def _prev(self) -> None:
        if self._current_index > 0:
            self._current_index -= 1
            self._scale_factor = 1.0
            self._load_image()

    def _next(self) -> None:
        if self._current_index < len(self._all_paths) - 1:
            self._current_index += 1
            self._scale_factor = 1.0
            self._load_image()

    # ------------------------------------------------------------------
    # 图片加载
    # ------------------------------------------------------------------

    def _load_image(self) -> None:
        file_path = self._all_paths[self._current_index]
        try:
            img = Image.open(file_path)
            img = ImageOps.exif_transpose(img)
            img_rgb = img.convert("RGB")
        except Exception as e:
            logger.error("无法加载图片 %s: %s", file_path, e)
            self._label.setStyleSheet(
                f"color: {Colors.TEXT_SECONDARY}; font-size: 14px;"
            )
            self._label.setText(f"无法加载图片:\n{file_path}\n\n{e}")
            self._update_nav_state()
            return

        buf = BytesIO()
        img_rgb.save(buf, format="JPEG", quality=90)
        pixmap = QPixmap()
        pixmap.loadFromData(buf.getvalue())
        self._pixmap = pixmap
        self._fit_to_window()
        self._update_nav_state()

    # ------------------------------------------------------------------
    # 缩放
    # ------------------------------------------------------------------

    def _zoom_in(self) -> None:
        if self._pixmap:
            self._zoom(1.25)

    def _zoom_out(self) -> None:
        if self._pixmap:
            self._zoom(1.0 / 1.25)

    def _zoom(self, factor: float) -> None:
        old = self._scale_factor
        self._scale_factor = max(0.05, min(20.0, self._scale_factor * factor))
        self._apply_scale_with_anchor(old)

    def _fit_to_window(self) -> None:
        if self._pixmap is None:
            return
        self._scale_factor = 1.0
        avail = self._scroll_area.viewport().size()
        scaled = self._pixmap.scaled(
            avail, Qt.KeepAspectRatio, Qt.SmoothTransformation,
        )
        self._label.setPixmap(scaled)
        self._label.resize(scaled.size())

    def _apply_scale_with_anchor(self, old_scale: float) -> None:
        vp = self._scroll_area.viewport()
        hb = self._scroll_area.horizontalScrollBar()
        vb = self._scroll_area.verticalScrollBar()

        cx = vp.width() / 2 + hb.value()
        cy = vp.height() / 2 + vb.value()

        ratio = self._scale_factor / max(old_scale, 0.001)
        new_size = self._pixmap.size() * self._scale_factor
        scaled = self._pixmap.scaled(
            new_size, Qt.KeepAspectRatio, Qt.SmoothTransformation,
        )
        self._label.setPixmap(scaled)
        self._label.resize(scaled.size())

        hb.setValue(max(0, min(hb.maximum(), int(cx * ratio - vp.width() / 2))))
        vb.setValue(max(0, min(vb.maximum(), int(cy * ratio - vp.height() / 2))))

    # ------------------------------------------------------------------
    # 鼠标滚轮无级缩放 (供 eventFilter 调用)
    # ------------------------------------------------------------------

    def _handle_wheel_zoom(self, event) -> None:
        if self._pixmap is None:
            return
        dy = event.angleDelta().y()
        if dy == 0:
            return

        old = self._scale_factor
        factor = 1.0008 ** dy
        self._scale_factor = max(0.05, min(20.0, self._scale_factor * factor))

        vp = self._scroll_area.viewport()
        hb = self._scroll_area.horizontalScrollBar()
        vb = self._scroll_area.verticalScrollBar()

        mouse_vp = self._scroll_area.mapFrom(self, event.position().toPoint())
        mx = mouse_vp.x() + hb.value()
        my = mouse_vp.y() + vb.value()

        ratio = self._scale_factor / max(old, 0.001)
        new_size = self._pixmap.size() * self._scale_factor
        scaled = self._pixmap.scaled(
            new_size, Qt.KeepAspectRatio, Qt.SmoothTransformation,
        )
        self._label.setPixmap(scaled)
        self._label.resize(scaled.size())

        hb.setValue(max(0, min(hb.maximum(), int(mx * ratio - mouse_vp.x()))))
        vb.setValue(max(0, min(vb.maximum(), int(my * ratio - mouse_vp.y()))))
