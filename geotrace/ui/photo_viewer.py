"""单张照片大图查看对话框 — GPU 纹理缩放、左键拖拽平移、前后导航.

v2.0: QOpenGLWidget 替换 QScrollArea+QLabel, 缩放不再走 CPU QPixmap.scaled(),
      而是通过 QPainter (OpenGL 后端) 在 GPU 上直接完成纹理采样缩放.
"""

import logging
from pathlib import Path
from io import BytesIO

from PIL import Image, ImageOps
from PySide6.QtCore import Qt, QPoint, QEvent, QPointF
from PySide6.QtGui import QAction, QColor, QKeySequence, QPainter, QPixmap
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QPushButton,
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


class _GpuPhotoWidget(QOpenGLWidget):
    """OpenGL 加速的照片显示 widget — QPainter 纹理缩放 + 鼠标拖拽平移."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self._scale: float = 1.0
        self._offset_x: float = 0.0
        self._offset_y: float = 0.0
        self._panning: bool = False
        self._pan_last: QPointF | None = None
        self.setMouseTracking(True)
        self.setCursor(Qt.OpenHandCursor)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    def setPhoto(self, pixmap: QPixmap) -> None:
        self._pixmap = pixmap
        self._offset_x = 0.0
        self._offset_y = 0.0
        self._scale = 1.0

    def photoSize(self) -> tuple[int, int]:
        if self._pixmap is None:
            return 0, 0
        return self._pixmap.width(), self._pixmap.height()

    def scale(self) -> float:
        return self._scale

    def setScale(self, value: float) -> None:
        self._scale = max(0.05, min(20.0, value))

    # ------------------------------------------------------------------
    # Coordinate helpers
    # ------------------------------------------------------------------

    def _content_rect(self) -> tuple[float, float, float, float]:
        """Return (cx, cy, cw, ch) — the centred content rect in widget space."""
        pw, ph = self.photoSize()
        cw = pw * self._scale
        ch = ph * self._scale
        cx = (self.width() - cw) / 2.0 + self._offset_x
        cy = (self.height() - ch) / 2.0 + self._offset_y
        return cx, cy, cw, ch

    def _widget_to_pix(self, wx: float, wy: float) -> tuple[float, float]:
        """Convert widget coords → pixmap pixel coords."""
        cx, cy, cw, ch = self._content_rect()
        pw, ph = self.photoSize()
        return ((wx - cx) / max(cw / pw, 0.001),
                (wy - cy) / max(ch / ph, 0.001))

    # ------------------------------------------------------------------
    # GL lifecycle
    # ------------------------------------------------------------------

    def initializeGL(self) -> None:
        pass

    def resizeGL(self, w: int, h: int) -> None:
        pass

    def paintGL(self) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)

        if self._pixmap is None or self._pixmap.isNull():
            p.fillRect(self.rect(), QColor(Colors.MAP_BG))
            p.end()
            return

        cx, cy, cw, ch = self._content_rect()
        if cw > 0 and ch > 0:
            p.drawPixmap(int(cx), int(cy), int(cw), int(ch), self._pixmap)
        p.end()

    # ------------------------------------------------------------------
    # Mouse events — pan + wheel zoom
    # ------------------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._panning = True
            self._pan_last = event.position()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._panning and self._pan_last is not None:
            pos = event.position()
            dx = pos.x() - self._pan_last.x()
            dy = pos.y() - self._pan_last.y()
            self._offset_x += dx
            self._offset_y += dy
            self._pan_last = pos
            self.update()
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._panning = False
            self._pan_last = None
            self.setCursor(Qt.OpenHandCursor)
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def wheelEvent(self, event) -> None:
        if self._pixmap is None:
            return
        dy = event.angleDelta().y()
        if dy == 0:
            return

        # Mouse-anchored zoom
        pos = event.position()
        pix_x, pix_y = self._widget_to_pix(pos.x(), pos.y())

        old_scale = self._scale
        factor = 1.0008 ** dy
        new_scale = max(0.05, min(20.0, old_scale * factor))

        pw, ph = self.photoSize()
        new_cw = pw * new_scale
        new_ch = ph * new_scale
        new_cx = (self.width() - new_cw) / 2.0
        new_cy = (self.height() - new_ch) / 2.0

        # Offset so the pixmap point under the mouse stays fixed
        self._offset_x = pos.x() - new_cx - pix_x * new_scale
        self._offset_y = pos.y() - new_cy - pix_y * new_scale
        self._scale = new_scale

        self.update()
        event.accept()

    # ------------------------------------------------------------------
    # Public zoom API (used by keyboard shortcuts)
    # ------------------------------------------------------------------

    def zoom(self, factor: float) -> None:
        if self._pixmap is None:
            return
        old = self._scale
        new_scale = max(0.05, min(20.0, old * factor))
        if new_scale == old:
            return

        # Anchor at viewport centre
        pw, ph = self.photoSize()
        cx_pix = pw / 2.0
        cy_pix = ph / 2.0

        new_cw = pw * new_scale
        new_ch = ph * new_scale
        new_cx = (self.width() - new_cw) / 2.0
        new_cy = (self.height() - new_ch) / 2.0

        self._offset_x = self.width() / 2.0 - new_cx - cx_pix * new_scale
        self._offset_y = self.height() / 2.0 - new_cy - cy_pix * new_scale
        self._scale = new_scale
        self.update()

    def fit_to_window(self) -> None:
        if self._pixmap is None:
            return
        pw, ph = self.photoSize()
        avail_w = self.width()
        avail_h = self.height()
        if avail_w <= 0 or avail_h <= 0:
            return

        s = min(avail_w / pw, avail_h / ph)
        self._scale = s
        self._offset_x = 0.0
        self._offset_y = 0.0
        self.update()


class PhotoViewer(QDialog):
    """照片大图查看器 — GPU 纹理缩放、左键拖拽平移、左右箭头切换."""

    def __init__(
        self, current_path: str, all_paths: list[str],
        current_index: int = 0, parent=None,
    ) -> None:
        super().__init__(parent)
        self._current_index = current_index
        self._all_paths = all_paths
        self._pixmap: QPixmap | None = None

        self.setWindowTitle(Path(current_path).name)
        self.resize(1200, 800)
        self.setMinimumSize(400, 300)
        self.setStyleSheet(f"QDialog {{ background-color: {Colors.MAP_BG}; }}")

        # GPU 照片渲染层 (替换旧 QScrollArea+QLabel)
        self._photo_widget = _GpuPhotoWidget(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._photo_widget)

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

        self._load_image()

    # ------------------------------------------------------------------
    # 浮动控件定位
    # ------------------------------------------------------------------

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._layout_overlays()
        self._photo_widget.fit_to_window()

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
            self._load_image()

    def _next(self) -> None:
        if self._current_index < len(self._all_paths) - 1:
            self._current_index += 1
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
            self._update_nav_state()
            return

        buf = BytesIO()
        img_rgb.save(buf, format="JPEG", quality=90)
        pixmap = QPixmap()
        pixmap.loadFromData(buf.getvalue())
        self._pixmap = pixmap

        self._photo_widget.setPhoto(pixmap)
        self._photo_widget.fit_to_window()
        self._update_nav_state()

    # ------------------------------------------------------------------
    # 缩放
    # ------------------------------------------------------------------

    def _zoom_in(self) -> None:
        self._photo_widget.zoom(1.25)

    def _zoom_out(self) -> None:
        self._photo_widget.zoom(1.0 / 1.25)

    def _fit_to_window(self) -> None:
        self._photo_widget.fit_to_window()
