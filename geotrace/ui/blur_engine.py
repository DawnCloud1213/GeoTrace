"""Frosted glass rendering engine — backdrop blur + noise texture.

Strategy A: Backdrop capture + GPU-accelerated separable Gaussian blur (FBO + GLSL)
Strategy B: Multi-scale procedural noise texture overlay (grain on all frosted surfaces)
Strategy C: Specular highlight + directional border + adaptive tint for realism
"""

import logging
import random

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import (
    QColor, QImage, QOffscreenSurface, QOpenGLContext, QPainter, QPixmap,
    QSurfaceFormat,
)
from PySide6.QtOpenGL import (
    QOpenGLFramebufferObject, QOpenGLShader, QOpenGLShaderProgram,
    QOpenGLTexture, QOpenGLVertexArrayObject,
)
from PySide6.QtWidgets import QGraphicsBlurEffect, QGraphicsScene, QWidget
from PySide6.QtOpenGLWidgets import QOpenGLWidget

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OpenGL constants (not exposed by PySide6)
# ---------------------------------------------------------------------------

_GL_TRIANGLES = 0x0004
_GL_COLOR_BUFFER_BIT = 0x00004000
_GL_TEXTURE_2D = 0x0DE1
_GL_TEXTURE0 = 0x84C0
_GL_CLAMP_TO_EDGE = 0x812F
_GL_LINEAR = 0x2601
_GL_TEXTURE_WRAP_S = 0x2802
_GL_TEXTURE_WRAP_T = 0x2803
_GL_TEXTURE_MAG_FILTER = 0x2800
_GL_TEXTURE_MIN_FILTER = 0x2801

# ---------------------------------------------------------------------------
# GPU separable Gaussian blur (fullscreen triangle + GLSL)
# ---------------------------------------------------------------------------

# Vertex shader: attribute-less fullscreen triangle via gl_VertexID
_BLUR_VS = b"""#version 330 core
out vec2 vTexCoord;
void main() {
    float x = float((gl_VertexID & 1) << 2) - 1.0;
    float y = float((gl_VertexID & 2) << 1) - 1.0;
    vTexCoord = vec2(x, y) * 0.5 + 0.5;
    gl_Position = vec4(x, y, 0.0, 1.0);
}
"""

# Fragment shader: 9-tap 1D Gaussian (uBlurScale stretches the kernel to match requested radius)
_BLUR_FS = b"""#version 330 core
in vec2 vTexCoord;
out vec4 fragColor;
uniform sampler2D uTexture;
uniform vec2 uTexelSize;
uniform float uBlurScale;
void main() {
    vec2 step = uTexelSize * uBlurScale;
    float sigma = uBlurScale * 1.65;
    float twoSigma2 = 2.0 * sigma * sigma;
    // auto-calculated 9-tap Gaussian weights
    vec4 color = vec4(0.0);
    float weightSum = 0.0;
    for (int i = -4; i <= 4; i++) {
        float w = exp(-float(i * i) / twoSigma2);
        color += texture(uTexture, vTexCoord + float(i) * step) * w;
        weightSum += w;
    }
    fragColor = color / weightSum;
}
"""


class _GpuBlurEngine:
    """Singleton: offscreen GL context + separable Gaussian blur program.

    Lazily initialises an OpenGL 3.3 core context that shares resources with
    QOpenGLWidget instances.  Two-pass blur: horizontal then vertical, each a
    single glDrawArrays call with the fullscreen-triangle vertex shader.
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._ready = False
        return cls._instance

    def _init_gl(self) -> bool:
        if self._ready:
            return True
        # Only attempt GPU blur when another QOpenGLWidget has already
        # initialised the global share context.
        if QOpenGLContext.globalShareContext() is None:
            return False
        try:
            fmt = QSurfaceFormat.defaultFormat()

            self._ctx = QOpenGLContext()
            self._ctx.setShareContext(QOpenGLContext.globalShareContext())
            self._ctx.setFormat(fmt)
            if not self._ctx.create():
                raise RuntimeError("Failed to create shared GL context")

            self._surface = QOffscreenSurface()
            self._surface.setFormat(fmt)
            self._surface.create()

            if not self._ctx.makeCurrent(self._surface):
                raise RuntimeError("Failed to make GL context current")

            # Compile shader
            self._program = QOpenGLShaderProgram()
            if not self._program.addShaderFromSourceCode(QOpenGLShader.Vertex, _BLUR_VS):
                raise RuntimeError(f"VS compile: {self._program.log()}")
            if not self._program.addShaderFromSourceCode(QOpenGLShader.Fragment, _BLUR_FS):
                raise RuntimeError(f"FS compile: {self._program.log()}")
            if not self._program.link():
                raise RuntimeError(f"Link: {self._program.log()}")

            # Cache uniform locations (int-based for PySide6 compatibility)
            self._loc_texture = self._program.uniformLocation(b"uTexture")
            self._loc_texel_size = self._program.uniformLocation(b"uTexelSize")
            self._loc_blur_scale = self._program.uniformLocation(b"uBlurScale")

            # VAO required by core profile (can be empty — we use gl_VertexID)
            self._vao = QOpenGLVertexArrayObject()
            if not self._vao.isCreated():
                if not self._vao.create():
                    raise RuntimeError("VAO creation failed")

            self._ctx.doneCurrent()
            self._ready = True
            return True
        except Exception:
            logger.warning("GPU blur not available, falling back to CPU", exc_info=True)
            return False

    def blur(self, input_pixmap: QPixmap, blur_radius: float) -> QPixmap | None:
        """Two-pass separable Gaussian blur.  Returns None on failure."""
        if not self._init_gl():
            return None

        w, h = input_pixmap.width(), input_pixmap.height()
        if w <= 0 or h <= 0:
            return None

        if not self._ctx.makeCurrent(self._surface):
            return None

        # Map QGraphicsBlurEffect "radius" to Gaussian sigma (~ radius / 3)
        blur_scale = max(0.33, blur_radius / 3.0)

        try:
            # Step 1: upload QPixmap → OpenGL texture
            image = input_pixmap.toImage().convertToFormat(QImage.Format_RGBA8888)
            tex = QOpenGLTexture(QOpenGLTexture.Target2D)
            tex.setSize(w, h)
            tex.setFormat(QOpenGLTexture.RGBA8_UNorm)
            tex.setMinMagFilters(QOpenGLTexture.Linear, QOpenGLTexture.Linear)
            tex.setWrapMode(QOpenGLTexture.ClampToEdge)
            tex.allocateStorage()
            tex.setData(0, QOpenGLTexture.RGBA, QOpenGLTexture.UInt8, image.constBits())

            # Step 2: create ping-pong FBOs
            fbo = QOpenGLFramebufferObject(w, h)

            # Step 3: horizontal blur  input_tex → fbo
            self._program.bind()
            self._vao.bind()

            fbo.bind()
            self._ctx.functions().glViewport(0, 0, w, h)
            self._ctx.functions().glClear(_GL_COLOR_BUFFER_BIT)

            self._ctx.functions().glActiveTexture(_GL_TEXTURE0)
            tex.bind()
            self._program.setUniformValue(self._loc_texture, 0)
            self._program.setUniformValue(self._loc_texel_size, 1.0 / w, 0.0)
            self._program.setUniformValue(self._loc_blur_scale, blur_scale)
            self._ctx.functions().glDrawArrays(_GL_TRIANGLES, 0, 3)
            tex.release()
            fbo.release()

            # Step 4: vertical blur  fbo_tex → result FBO
            result_fbo = QOpenGLFramebufferObject(w, h)
            result_fbo.bind()
            self._ctx.functions().glClear(_GL_COLOR_BUFFER_BIT)

            fbo_tex_id = fbo.texture()
            self._ctx.functions().glActiveTexture(_GL_TEXTURE0)
            self._ctx.functions().glBindTexture(_GL_TEXTURE_2D, fbo_tex_id)
            # Wrap / filter needed because the raw GL texture may not inherit params
            self._ctx.functions().glTexParameteri(_GL_TEXTURE_2D, _GL_TEXTURE_WRAP_S, _GL_CLAMP_TO_EDGE)
            self._ctx.functions().glTexParameteri(_GL_TEXTURE_2D, _GL_TEXTURE_WRAP_T, _GL_CLAMP_TO_EDGE)
            self._ctx.functions().glTexParameteri(_GL_TEXTURE_2D, _GL_TEXTURE_MAG_FILTER, _GL_LINEAR)
            self._ctx.functions().glTexParameteri(_GL_TEXTURE_2D, _GL_TEXTURE_MIN_FILTER, _GL_LINEAR)
            self._program.setUniformValue(self._loc_texture, 0)
            self._program.setUniformValue(self._loc_texel_size, 0.0, 1.0 / h)
            self._program.setUniformValue(self._loc_blur_scale, blur_scale)
            self._ctx.functions().glDrawArrays(_GL_TRIANGLES, 0, 3)

            self._vao.release()
            self._program.release()
            result_fbo.release()

            # Step 5: read back
            result_image = result_fbo.toImage()
            result = QPixmap.fromImage(result_image)

            self._ctx.doneCurrent()
            return result
        except Exception:
            logger.warning("GPU blur pass failed", exc_info=True)
            self._ctx.doneCurrent()
            return None


def _gpu_blur(pixmap: QPixmap, radius: float) -> QPixmap | None:
    """Convenience wrapper — returns None if GPU blur is unavailable."""
    return _GpuBlurEngine().blur(pixmap, radius)

# ---------------------------------------------------------------------------
# Noise texture
# ---------------------------------------------------------------------------

_NOISE_CACHE: dict[tuple[int, int, float, int, int], QPixmap] = {}


def generate_noise_pixmap(
    width: int,
    height: int,
    opacity: float = 0.04,
    grain_scale: int = 2,
    seed: int = 42,
) -> QPixmap:
    """Generate a subtle noise/grain texture pixmap for glass surfaces.

    Uses a fixed random seed for consistent appearance across frames.
    Generates a small noise image then scales up to create grain clumps.
    """
    key = (width, height, opacity, grain_scale, seed)
    if key in _NOISE_CACHE:
        return _NOISE_CACHE[key]

    small_w = max(4, width // grain_scale)
    small_h = max(4, height // grain_scale)

    rng = random.Random(seed)
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


def generate_noise_pixmap_multiscale(
    width: int,
    height: int,
    fine_opacity: float = 0.025,
    fine_scale: int = 2,
    coarse_opacity: float = 0.015,
    coarse_scale: int = 8,
) -> QPixmap:
    """Generate multi-scale noise mimicking real frosted glass surface.

    Real frosted glass has:
    - Fine grain: microscopic surface roughness (high frequency)
    - Coarse grain: acid-etching patterns from manufacturing (low frequency)

    The two layers use different seeds to avoid correlated patterns.
    """
    fine_noise = generate_noise_pixmap(
        width, height, opacity=fine_opacity, grain_scale=fine_scale, seed=42,
    )
    coarse_noise = generate_noise_pixmap(
        width, height, opacity=coarse_opacity, grain_scale=coarse_scale, seed=137,
    )

    result = QPixmap(width, height)
    result.fill(Qt.transparent)
    painter = QPainter(result)
    painter.drawPixmap(0, 0, coarse_noise)
    painter.drawPixmap(0, 0, fine_noise)
    painter.end()
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

    Downsampling: the captured region is scaled to 1/downsample before blurring
    and upscaled back afterward. At downsample=3 the pixel count drops to 1/9,
    making high-quality blur affordable.

    IMPORTANT: Never call capture() from within the child's paintEvent —
    parent.grab() would render the child, causing recursive paintEvent → stack overflow.
    """

    def __init__(self, child: QWidget, blur_radius: int = 25,
                 capture_target: QWidget | None = None,
                 downsample: int = 3):
        self._child = child
        self._blur_radius = blur_radius
        self._capture_target = capture_target
        self._downsample = max(1, downsample)
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

    def _find_gl_widget(self) -> QOpenGLWidget | None:
        """Find a visible QOpenGLWidget descendant in the capture target tree."""
        target = self._capture_target or (self._child.parentWidget() if self._child else None)
        if target is None:
            return None
        if isinstance(target, QOpenGLWidget) and target.isVisible():
            return target
        for child in target.findChildren(QOpenGLWidget):
            if child.isVisible():
                return child
        return None

    def capture(self) -> QPixmap | None:
        """Capture content behind this widget, blurred.

        Returns a pixmap sized to the widget, or None on failure.

        Uses QOpenGLWidget.grabFramebuffer() when available — avoids
        interfering with the OpenGL rendering pipeline and prevents
        self-capture of floating child widgets.
        """
        geo = self._child.geometry()
        if geo.isEmpty() or geo.width() <= 0 or geo.height() <= 0:
            return None

        if self._cached_geo == geo and self._cached_pixmap and not self._cached_pixmap.isNull():
            return self._cached_pixmap

        target = self._capture_target or self._child.parentWidget()
        if target is None:
            return None

        gl_widget = self._find_gl_widget()

        if gl_widget is not None:
            # ── GPU path: grabFramebuffer() won't corrupt OpenGL state ──
            source_rect = self._compute_source_rect()
            if source_rect is None:
                return None

            gl_offset = gl_widget.mapFrom(target, QPoint(0, 0))
            gl_source = QRect(source_rect.topLeft() + gl_offset, source_rect.size())
            gl_source = gl_source.intersected(gl_widget.rect())
            if gl_source.isEmpty():
                return None

            gl_widget.makeCurrent()
            fb = gl_widget.grabFramebuffer()
            gl_widget.doneCurrent()

            if fb.isNull():
                return None
            cropped = QPixmap.fromImage(fb.copy(gl_source))
        elif self._capture_target is not None:
            # ── Software fallback: hide child before render() to avoid self-capture ──
            source_rect = self._compute_source_rect()
            if source_rect is None:
                return None

            cropped = QPixmap(source_rect.size())
            cropped.fill(Qt.transparent)
            was_visible = self._child.isVisible()
            if was_visible:
                self._child.setVisible(False)
            target.render(cropped, QPoint(0, 0),
                          QRect(source_rect.topLeft(), source_rect.size()))
            if was_visible:
                self._child.setVisible(True)
            if cropped.isNull():
                return None
        else:
            try:
                full = target.grab()
            except (AttributeError, RuntimeError):
                full = QPixmap(target.size())
                full.fill(Qt.transparent)
                target.render(full)

            if full.isNull():
                return None
            cropped = full.copy(geo)

        if cropped.isNull():
            return None

        # Downsample before blur — reduces pixels to 1/(downsample²)
        if self._downsample > 1:
            small_w = max(1, cropped.width() // self._downsample)
            small_h = max(1, cropped.height() // self._downsample)
            blur_input = cropped.scaled(small_w, small_h,
                                        Qt.IgnoreAspectRatio,
                                        Qt.SmoothTransformation)
            blur_radius = max(2, self._blur_radius // self._downsample)
            blur_output_size = blur_input.size()
        else:
            blur_input = cropped
            blur_radius = self._blur_radius
            blur_output_size = cropped.size()

        # GPU-accelerated separable Gaussian blur; falls back to CPU
        result = _gpu_blur(blur_input, blur_radius)
        if result is None:
            # CPU fallback: QGraphicsScene + QGraphicsBlurEffect
            scene = QGraphicsScene()
            pixmap_item = scene.addPixmap(blur_input)
            blur = QGraphicsBlurEffect()
            blur.setBlurRadius(blur_radius)
            pixmap_item.setGraphicsEffect(blur)
            result = QPixmap(blur_output_size)
            result.fill(Qt.transparent)
            painter = QPainter(result)
            scene.render(painter, QRect(QPoint(0, 0), blur_output_size),
                         QRect(QPoint(0, 0), blur_output_size))
            painter.end()

        # Upsample back to original size with smooth interpolation
        if self._downsample > 1:
            result = result.scaled(cropped.size(),
                                   Qt.IgnoreAspectRatio,
                                   Qt.SmoothTransformation)

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
      2. Adaptive tint color fill (warm base blended with backdrop average)
      3. Specular highlight gradient (diagonal light reflection)
      4. Multi-scale noise texture overlay
      5. Directional inner border (top-left bright, bottom-right dark)
    """

    def __init__(
        self,
        tint_color: QColor | None = None,
        border_color: QColor | None = None,
        border_radius: float = 8.0,
        highlight_intensity: float = 0.6,
        adaptive_tint: bool = True,
    ):
        self._base_tint = tint_color or QColor(255, 255, 255, 160)
        self.tint = QColor(self._base_tint)
        self.border = border_color or QColor(255, 255, 255, 60)
        self.border_radius = border_radius
        self.highlight_intensity = max(0.0, min(1.0, highlight_intensity))
        self._adaptive_tint = adaptive_tint
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

        # Layer 2: Tint color (adaptive — blends backdrop average with warm base)
        tint = self._compute_adaptive_tint(backdrop) if self._adaptive_tint else self.tint
        painter.fillRect(rect, tint)

        # Layer 3: Specular highlight (diagonal light reflection)
        self._draw_specular_highlight(painter, rect)

        # Layer 4: Noise texture
        if self.noise and not self.noise.isNull():
            painter.drawPixmap(rect, self.noise)

        painter.setClipping(False)

        # Layer 5: Directional inner border (light from top-left)
        self._draw_directional_border(painter, rect)

        painter.restore()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _draw_specular_highlight(self, painter: QPainter, rect: QRect) -> None:
        """Draw a diagonal specular highlight simulating glass reflection.

        Creates a soft gradient from top-left (bright) to bottom-right (dark),
        mimicking light reflecting off the glass surface at a glancing angle.
        """
        from PySide6.QtGui import QLinearGradient

        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())

        hi = self.highlight_intensity
        # Hot spot near top-left corner (~20% of diagonal)
        hot_alpha = int(35 * hi)
        gradient.setColorAt(0.0, QColor(255, 255, 255, hot_alpha))
        gradient.setColorAt(0.15, QColor(255, 255, 255, max(0, hot_alpha - 15)))
        gradient.setColorAt(0.4, QColor(255, 255, 255, 0))
        gradient.setColorAt(1.0, QColor(0, 0, 0, int(15 * hi)))

        painter.fillRect(rect, gradient)

    def _draw_directional_border(self, painter: QPainter, rect: QRect) -> None:
        """Draw an inner border with top-left light and bottom-right shadow.

        This creates the illusion of a bevelled glass edge catching light
        from the top-left.
        """
        from PySide6.QtGui import QLinearGradient, QPainterPath, QPen

        path = QPainterPath()
        path.addRoundedRect(
            rect.adjusted(0, 0, -1, -1),
            self.border_radius, self.border_radius,
        )

        b = self.border
        top_color = QColor(b.red(), b.green(), b.blue(),
                           min(255, b.alpha() * 2))
        bottom_color = QColor(b.red(), b.green(), b.blue(),
                              max(10, b.alpha() // 2))

        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0.0, top_color)
        gradient.setColorAt(0.5, QColor(b))
        gradient.setColorAt(1.0, bottom_color)

        pen = QPen(gradient, 1.0)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)

    def _compute_adaptive_tint(self, backdrop: QPixmap | None) -> QColor:
        """Sample average color of backdrop and blend with base tint.

        This makes the glass 'react' to its background:
        - Over ocean (blue) → slightly cooler tint
        - Over land (green/brown) → slightly warmer tint
        - Over urban (gray) → neutral tint

        Returns the final tint QColor.
        """
        if backdrop is None or backdrop.isNull():
            return self.tint

        # Sample a downscaled version for fast average
        sample = backdrop.scaled(16, 16, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        image = sample.toImage()

        r_sum, g_sum, b_sum, count = 0, 0, 0, 0
        for y in range(image.height()):
            for x in range(image.width()):
                c = image.pixelColor(x, y)
                if c.alpha() > 0:
                    r_sum += c.red()
                    g_sum += c.green()
                    b_sum += c.blue()
                    count += 1

        if count == 0:
            return self.tint

        avg_r = r_sum // count
        avg_g = g_sum // count
        avg_b = b_sum // count

        # Blend: 70% base tint, 30% background average
        blend = 0.30
        bt = self._base_tint
        blended_r = int(bt.red() * (1 - blend) + avg_r * blend)
        blended_g = int(bt.green() * (1 - blend) + avg_g * blend)
        blended_b = int(bt.blue() * (1 - blend) + avg_b * blend)

        return QColor(blended_r, blended_g, blended_b, self.tint.alpha())
