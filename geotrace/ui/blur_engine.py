"""Frosted glass rendering engine — backdrop blur + noise texture.

Strategy A: Backdrop capture + QGraphicsBlurEffect (widget-level, for floating panels)
Strategy B: Procedural noise texture overlay (grain on all frosted surfaces)
"""

import random

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QGraphicsBlurEffect, QGraphicsScene, QWidget

# ---------------------------------------------------------------------------
# Noise texture
# ---------------------------------------------------------------------------

_NOISE_CACHE: dict[tuple[int, int, float, int], QPixmap] = {}


def generate_noise_pixmap(
    width: int,
    height: int,
    opacity: float = 0.04,
    grain_scale: int = 2,
) -> QPixmap:
    """Generate a subtle noise/grain texture pixmap for glass surfaces.

    Uses a fixed random seed for consistent appearance across frames.
    Generates a small noise image then scales up to create grain clumps.
    """
    key = (width, height, opacity, grain_scale)
    if key in _NOISE_CACHE:
        return _NOISE_CACHE[key]

    small_w = max(4, width // grain_scale)
    small_h = max(4, height // grain_scale)

    rng = random.Random(42)  # fixed seed for visual consistency
    image = QImage(small_w, small_h, QImage.Format_ARGB32)
    for y in range(small_h):
        for x in range(small_w):
            v = rng.randint(0, 255)
            alpha = int(opacity * 255)
            image.setPixelColor(x, y, QColor(v, v, v, alpha))

    pixmap = QPixmap.fromImage(image)
    result = pixmap.scaled(width, height, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    _NOISE_CACHE[key] = result
    return result


# ---------------------------------------------------------------------------
# Backdrop blur capture (for floating panels over the map)
# ---------------------------------------------------------------------------


class BackdropBlurCapture:
    """Capture and blur content behind a child widget.

    Used by floating panels (ProvinceListPanel, SettingsPanel) that overlay
    the MapWidget. Captures the rendered map content in the widget's geometry,
    applies Gaussian blur, and caches the result.

    If capture_target is provided, the backdrop is captured from that widget
    instead of the child's parent — useful when the child is not a direct
    overlay of the target (e.g., sidebar panel vs. map in QSplitter).

    IMPORTANT: Never call capture() from within the child's paintEvent —
    parent.grab() would render the child, causing recursive paintEvent → stack overflow.
    """

    def __init__(self, child: QWidget, blur_radius: int = 25,
                 capture_target: QWidget | None = None):
        self._child = child
        self._blur_radius = blur_radius
        self._capture_target = capture_target
        self._cached_pixmap: QPixmap | None = None
        self._cached_geo: QRect | None = None

    def _compute_source_rect(self) -> QRect | None:
        """Map child's global rect into capture-target's local coords.

        Returns None if the rect has zero overlap with the target.
        """
        if self._capture_target is None:
            return self._child.geometry()

        child_global_top_left = self._child.mapToGlobal(QPoint(0, 0))
        child_rect_global = QRect(child_global_top_left, self._child.size())
        target_global_rect = QRect(
            self._capture_target.mapToGlobal(QPoint(0, 0)),
            self._capture_target.size(),
        )

        intersection_global = child_rect_global.intersected(target_global_rect)
        if intersection_global.isEmpty():
            return None

        top_left_in_target = self._capture_target.mapFromGlobal(
            intersection_global.topLeft()
        )

        return QRect(top_left_in_target, intersection_global.size())

    def capture(self) -> QPixmap | None:
        """Capture content behind this widget, blurred.

        Returns a pixmap sized to the widget, or None on failure.
        """
        geo = self._child.geometry()
        if geo.isEmpty() or geo.width() <= 0 or geo.height() <= 0:
            return None

        if self._cached_geo == geo and self._cached_pixmap and not self._cached_pixmap.isNull():
            return self._cached_pixmap

        target = self._capture_target or self._child.parentWidget()
        if target is None:
            return None

        try:
            full = target.grab()
        except (AttributeError, RuntimeError):
            full = QPixmap(target.size())
            full.fill(Qt.transparent)
            target.render(full)

        if full.isNull():
            return None

        if self._capture_target is not None:
            # Map child's position into the capture target's coordinate system
            source_rect = self._compute_source_rect()
            if source_rect is None:
                return None

            # Clamp source rect to the target pixmap bounds
            target_rect = QRect(QPoint(0, 0), full.size())
            clamped = source_rect.intersected(target_rect)
            if clamped.isEmpty():
                return None

            # Create a pixmap sized to the CHILD, initialized with edge-extended
            # pixels from target. For complete non-overlap the rect will be empty
            # and we fall back to filling with the dominant color.
            result_base = QPixmap(clamped.width(), clamped.height())
            result_base.fill(Qt.transparent)
            painter_base = QPainter(result_base)
            painter_base.drawPixmap(QPoint(0, 0), full, clamped)
            painter_base.end()

            cropped = result_base
        else:
            cropped = full.copy(geo)

        if cropped.isNull():
            return None

        # Blur via QGraphicsScene + QGraphicsBlurEffect
        scene = QGraphicsScene()
        pixmap_item = scene.addPixmap(cropped)
        blur = QGraphicsBlurEffect()
        blur.setBlurRadius(self._blur_radius)
        blur.setBlurHints(QGraphicsBlurEffect.PerformanceHint)
        pixmap_item.setGraphicsEffect(blur)

        result = QPixmap(cropped.size())
        result.fill(Qt.transparent)
        painter = QPainter(result)
        scene.render(painter, QRect(0, 0, cropped.width(), cropped.height()),
                     QRect(0, 0, cropped.width(), cropped.height()))
        painter.end()

        self._cached_pixmap = result
        self._cached_geo = geo
        return result

    def invalidate(self) -> None:
        self._cached_pixmap = None
        self._cached_geo = None


# ---------------------------------------------------------------------------
# Frosted surface composition helper
# ---------------------------------------------------------------------------


class FrostedSurfacePainter:
    """Compose frosted glass layers onto a QPainter.

    Layers (bottom → top):
      1. Blurred backdrop pixmap
      2. Semi-transparent tint color fill
      3. Noise texture overlay
      4. Subtle inner border highlight (glass edge)
    """

    def __init__(
        self,
        tint_color: QColor | None = None,
        border_color: QColor | None = None,
        border_radius: float = 8.0,
    ):
        self.tint = tint_color or QColor(255, 255, 255, 160)
        self.border = border_color or QColor(255, 255, 255, 60)
        self.border_radius = border_radius
        self.noise: QPixmap | None = None

    def paint(
        self,
        painter: QPainter,
        rect: QRect,
        backdrop: QPixmap | None = None,
    ) -> None:
        """Paint the complete frosted glass surface in rect."""
        from PySide6.QtGui import QPainterPath

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)

        clip_path = QPainterPath()
        clip_path.addRoundedRect(rect, self.border_radius, self.border_radius)
        painter.setClipPath(clip_path)

        # Layer 1: Blurred backdrop
        if backdrop and not backdrop.isNull():
            painter.drawPixmap(rect, backdrop)

        # Layer 2: Tint color
        painter.fillRect(rect, self.tint)

        # Layer 3: Noise texture
        if self.noise and not self.noise.isNull():
            painter.drawPixmap(rect, self.noise)

        painter.setClipping(False)

        # Layer 4: Inner border highlight (glass edge)
        painter.setPen(QColor(self.border))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(
            rect.adjusted(0, 0, -1, -1),
            self.border_radius,
            self.border_radius,
        )

        painter.restore()
