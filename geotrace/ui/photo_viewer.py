"""单张照片大图查看对话框."""

import logging
from pathlib import Path

from io import BytesIO

from PIL import Image, ImageOps
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QScrollArea,
    QVBoxLayout,
)

logger = logging.getLogger(__name__)


class PhotoViewer(QDialog):
    """照片大图查看器 — 支持缩放和 EXIF 方向校正."""

    def __init__(self, file_path: str, parent=None) -> None:
        super().__init__(parent)
        self._file_path = file_path
        self._scale_factor = 1.0
        self._pixmap: QPixmap | None = None

        self.setWindowTitle(Path(file_path).name)
        self.resize(1200, 800)
        self.setMinimumSize(400, 300)

        # UI
        self._label = QLabel()
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setScaledContents(False)

        scroll = QScrollArea()
        scroll.setWidget(self._label)
        scroll.setWidgetResizable(False)
        scroll.setAlignment(Qt.AlignCenter)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)

        # 快捷键
        zoom_in = QAction("放大", self)
        zoom_in.setShortcut(QKeySequence.ZoomIn)
        zoom_in.triggered.connect(self._zoom_in)
        self.addAction(zoom_in)

        zoom_out = QAction("缩小", self)
        zoom_out.setShortcut(QKeySequence.ZoomOut)
        zoom_out.triggered.connect(self._zoom_out)
        self.addAction(zoom_out)

        fit_action = QAction("适应窗口", self)
        fit_action.setShortcut(QKeySequence("Ctrl+0"))
        fit_action.triggered.connect(self._fit_to_window)
        self.addAction(fit_action)

        self._load_image()

    def _load_image(self) -> None:
        """加载并显示图片 (含 EXIF 自动旋转)."""
        try:
            img = Image.open(self._file_path)
            img = ImageOps.exif_transpose(img)  # 根据 EXIF Orientation 自动旋转
            img_rgb = img.convert("RGB")
        except Exception as e:
            logger.error("无法加载图片 %s: %s", self._file_path, e)
            self._label.setText(f"无法加载图片:\n{self._file_path}\n\n{e}")
            return

        # PIL -> QPixmap (通过 JPEG 字节流)
        buf = BytesIO()
        img_rgb.save(buf, format="JPEG", quality=90)
        pixmap = QPixmap()
        pixmap.loadFromData(buf.getvalue())
        self._pixmap = pixmap
        self._fit_to_window()

    def _zoom_in(self) -> None:
        if self._pixmap is None:
            return
        self._scale_factor *= 1.25
        self._apply_scale()

    def _zoom_out(self) -> None:
        if self._pixmap is None:
            return
        self._scale_factor /= 1.25
        self._apply_scale()

    def _fit_to_window(self) -> None:
        if self._pixmap is None:
            return
        avail = self.size()
        scaled = self._pixmap.scaled(
            avail, Qt.KeepAspectRatio, Qt.SmoothTransformation,
        )
        self._label.setPixmap(scaled)
        self._label.resize(scaled.size())
        self._scale_factor = 1.0

    def _apply_scale(self) -> None:
        if self._pixmap is None:
            return
        new_size = self._pixmap.size() * self._scale_factor
        scaled = self._pixmap.scaled(
            new_size, Qt.KeepAspectRatio, Qt.SmoothTransformation,
        )
        self._label.setPixmap(scaled)
        self._label.resize(scaled.size())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._scale_factor <= 1.01:
            self._fit_to_window()
