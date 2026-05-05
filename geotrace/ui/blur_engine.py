"""Frosted glass rendering engine — backdrop blur + noise texture.

Strategy A: Backdrop capture + GPU-accelerated separable Gaussian blur (FBO + GLSL)
Strategy B: Multi-scale procedural noise texture overlay (grain on all frosted surfaces)
Strategy C: Specular highlight + directional border + adaptive tint for realism
"""

import logging
import random

from PySide6.QtCore import QPoint, QRect, QSize, Qt
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
_GL_READ_FRAMEBUFFER = 0x8CA8
_GL_DRAW_FRAMEBUFFER = 0x8CA9

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

# Fragment shader: Dual Kawase downsample (5-tap, writes to half-res FBO)
_KAWASE_DOWN_FS = b"""#version 330 core
in vec2 vTexCoord;
out vec4 fragColor;
uniform sampler2D uTexture;
uniform vec2 uHalfTexel;
void main() {
    vec2 uv = vTexCoord;
    vec4 col = texture(uTexture, uv) * 4.0;
    col += texture(uTexture, uv + vec2(-1, -1) * uHalfTexel);
    col += texture(uTexture, uv + vec2(-1,  1) * uHalfTexel);
    col += texture(uTexture, uv + vec2( 1, -1) * uHalfTexel);
    col += texture(uTexture, uv + vec2( 1,  1) * uHalfTexel);
    fragColor = col * 0.125;
}
"""

# Fragment shader: Liquid Glass -- full-color fiber-optic edge dispersion.
#
# Center ~78% area: pure passthrough — no distortion, no color shift,
# no tint, no noise. The background shows through exactly as-is.
#
# Outer edge ring (~22%): fiber-optic chromatic dispersion simulating
# light refracting through curved glass edges. R/G/B channels sample
# at slightly different radial offsets, producing a prism-like color
# fringe that intensifies toward the very edge.
#
# No blur kernel — Apple Liquid Glass is transparent glass, not frosted.
_LIQUID_GLASS_FS = b"""#version 330 core
in vec2 vTexCoord;
out vec4 fragColor;
uniform sampler2D uTexture;
uniform float uTime;
uniform vec2 uPanelSize;
uniform float uCornerRadius;

void main() {
    vec2 uv = vTexCoord;

    // -- Rectangular edge zone: center 55% = clear, outer 45% = glass --
    float mx = abs(uv.x - 0.5) * 2.0;
    float my = abs(uv.y - 0.5) * 2.0;
    float margin = max(mx, my);
    float edgeZone = smoothstep(0.55, 1.0, margin);

    // -- Dispersion direction: outward from nearest edge --
    vec2 edgeDir;
    if (mx > my) {
        edgeDir = vec2(sign(uv.x - 0.5), 0.0);
    } else {
        edgeDir = vec2(0.0, sign(uv.y - 0.5));
    }

    // -- 1. Spatial refractive warping --
    // 0.30 UV at full edge zone: visible distortion without being extreme.
    float warp = 0.30 * edgeZone * margin * margin;
    warp += sin(uv.y * 35.0 + uTime * 0.3) * 0.012 * edgeZone;
    vec2 warpedUV = uv + edgeDir * warp;

    // -- 2. Sample at warped position (geometric distortion) --
    vec4 col = texture(uTexture, warpedUV);

    // -- 3. Chromatic aberration on warped base --
    float chroma = edgeZone * 0.20;
    float r = texture(uTexture, warpedUV + edgeDir * chroma).r;
    float b = texture(uTexture, warpedUV - edgeDir * chroma * 1.4).b;
    col.r = mix(col.r, r, edgeZone * 0.7);
    col.b = mix(col.b, b, edgeZone * 0.7);

    // -- 4. Edge glass-thickness shadow --
    float thickness = smoothstep(0.75, 1.0, margin) * edgeZone * 0.10;
    col.rgb *= 1.0 - thickness;

    // -- 5. Cool rim glow --
    float rimGlow = smoothstep(0.85, 1.0, margin) * 0.03;
    col.rgb += vec3(0.85, 0.92, 1.0) * rimGlow;

    // -- 6. Warm specular highlight --
    float spec = smoothstep(0.78, 0.94, margin) * 0.015;
    float dir = 0.5 + 0.5 * dot(normalize(uv - 0.5 + 0.001), normalize(vec2(-0.7, -0.7)));
    col.rgb += vec3(1.0, 0.97, 0.92) * (spec * dir);

    fragColor = col;
}
"""



class _GpuBlurEngine:
    """Singleton: standalone GL context + Liquid Glass refraction program.

    Creates an independent OpenGL 3.3 context (no sharing needed — all
    rendering goes to our own FBO). If globalShareContext is available
    it will be used for sharing, but lack of it is not a blocker.

    refract(): Single-pass GPU shader applying fiber-optic edge dispersion
    — no blur, no tint, full-color passthrough at center.
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
        try:
            fmt = QSurfaceFormat.defaultFormat()

            # Create a standalone OpenGL context — no sharing needed since
            # we render to our own FBO and don't share textures.
            self._ctx = QOpenGLContext()
            # Try sharing with global context if available (Qt < 6), but
            # don't bail if unavailable — standalone works fine for FBO ops.
            share_ctx = QOpenGLContext.globalShareContext()
            if share_ctx is not None:
                self._ctx.setShareContext(share_ctx)
            self._ctx.setFormat(fmt)
            if not self._ctx.create():
                raise RuntimeError("Failed to create GL context")

            self._surface = QOffscreenSurface()
            self._surface.setFormat(fmt)
            self._surface.create()

            if not self._ctx.makeCurrent(self._surface):
                raise RuntimeError("Failed to make GL context current")

            # Compile Dual Kawase down/up programs
            self._prog_down = QOpenGLShaderProgram()
            self._prog_down.addShaderFromSourceCode(QOpenGLShader.Vertex, _BLUR_VS)
            self._prog_down.addShaderFromSourceCode(QOpenGLShader.Fragment, _KAWASE_DOWN_FS)
            self._prog_down.link()
            self._loc_down_tex = self._prog_down.uniformLocation(b"uTexture")
            self._loc_down_htexel = self._prog_down.uniformLocation(b"uHalfTexel")
            self._prog_up = QOpenGLShaderProgram()
            vs_ok = self._prog_up.addShaderFromSourceCode(QOpenGLShader.Vertex, _BLUR_VS)
            fs_ok = self._prog_up.addShaderFromSourceCode(QOpenGLShader.Fragment, _LIQUID_GLASS_FS)
            if not vs_ok or not fs_ok:
                logger.warning("Liquid Glass shader compile issue: vs=%s fs=%s log=%s",
                              vs_ok, fs_ok, self._prog_up.log())
            self._prog_up.link()
            if not self._prog_up.isLinked():
                logger.warning("Liquid Glass shader link failed: %s", self._prog_up.log())
            self._loc_up_tex = self._prog_up.uniformLocation(b"uTexture")
            self._loc_up_time = self._prog_up.uniformLocation(b"uTime")
            self._loc_up_panel_size = self._prog_up.uniformLocation(b"uPanelSize")
            self._loc_up_corner_radius = self._prog_up.uniformLocation(b"uCornerRadius")

            # Log shader link status
            for name, prog in [("down", self._prog_down), ("up", self._prog_up)]:
                if not prog.isLinked():
                    logger.warning("Shader %s link failed: %s", name, prog.log())

            # VAO (required by core profile, empty — we use gl_VertexID)
            self._vao = QOpenGLVertexArrayObject()
            if not self._vao.isCreated():
                if not self._vao.create():
                    raise RuntimeError("VAO creation failed")

            # Pooled resources — recreated on size change
            self._pool_w = 0
            self._pool_h = 0
            self._input_tex: QOpenGLTexture | None = None
            self._up_fbo: QOpenGLFramebufferObject | None = None

            self._ctx.doneCurrent()
            self._ready = True
            return True
        except Exception:
            logger.warning("GPU blur not available, falling back to CPU", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Dual Kawase blur (quality path)
    # ------------------------------------------------------------------

    def blur(self, input_pixmap: QPixmap, blur_radius: float,
             saturation: float = 1.0) -> QPixmap | None:
        """Dual Kawase blur. Returns None on failure.

        blur_radius controls the half-texel offset scale (1.0 = standard).
        saturation is applied inline during upsample (1.0 = no boost).
        """
        if not self._init_gl():
            return None

        w, h = input_pixmap.width(), input_pixmap.height()
        hw, hh = max(1, w // 2), max(1, h // 2)
        if w <= 0 or h <= 0 or hw <= 0 or hh <= 0:
            return None

        if not self._ctx.makeCurrent(self._surface):
            return None

        try:
            # Re/create pooled resources on size change
            if w != self._pool_w or h != self._pool_h:
                self._pool_w = w
                self._pool_h = h
                self._input_tex = QOpenGLTexture(QOpenGLTexture.Target2D)
                self._input_tex.setSize(w, h)
                self._input_tex.setFormat(QOpenGLTexture.RGBA8_UNorm)
                self._input_tex.setMinMagFilters(QOpenGLTexture.Linear, QOpenGLTexture.Linear)
                self._input_tex.setWrapMode(QOpenGLTexture.ClampToEdge)
                self._input_tex.allocateStorage()
                self._down_fbo = None  # force recreate
                self._up_fbo = None

            if self._down_fbo is None:
                self._down_fbo = QOpenGLFramebufferObject(hw, hh)
            if self._up_fbo is None:
                self._up_fbo = QOpenGLFramebufferObject(w, h)

            # Upload pixmap → pooled texture
            image = input_pixmap.toImage().convertToFormat(QImage.Format_RGBA8888)
            self._input_tex.setData(0, QOpenGLTexture.RGBA, QOpenGLTexture.UInt8,
                                     image.constBits())

            gl = self._ctx.functions()

            # --- Downsample pass: input_tex → down_fbo (half-res) ---
            self._prog_down.bind()
            self._vao.bind()
            self._down_fbo.bind()
            gl.glViewport(0, 0, hw, hh)
            gl.glClear(_GL_COLOR_BUFFER_BIT)

            gl.glActiveTexture(_GL_TEXTURE0)
            self._input_tex.bind()
            self._prog_down.setUniformValue(self._loc_down_tex, 0)
            scale = max(0.5, blur_radius / 20.0)
            self._prog_down.setUniformValue(self._loc_down_htexel,
                                              scale / hw, scale / hh)
            gl.glDrawArrays(_GL_TRIANGLES, 0, 3)
            self._input_tex.release()
            self._down_fbo.release()

            # --- Upsample pass: down_fbo → up_fbo (full-res) ---
            self._prog_up.bind()
            self._up_fbo.bind()
            gl.glViewport(0, 0, w, h)
            gl.glClear(_GL_COLOR_BUFFER_BIT)

            fbo_tex_id = self._down_fbo.texture()
            gl.glActiveTexture(_GL_TEXTURE0)
            gl.glBindTexture(_GL_TEXTURE_2D, fbo_tex_id)
            gl.glTexParameteri(_GL_TEXTURE_2D, _GL_TEXTURE_WRAP_S, _GL_CLAMP_TO_EDGE)
            gl.glTexParameteri(_GL_TEXTURE_2D, _GL_TEXTURE_WRAP_T, _GL_CLAMP_TO_EDGE)
            gl.glTexParameteri(_GL_TEXTURE_2D, _GL_TEXTURE_MAG_FILTER, _GL_LINEAR)
            gl.glTexParameteri(_GL_TEXTURE_2D, _GL_TEXTURE_MIN_FILTER, _GL_LINEAR)
            self._prog_up.setUniformValue(self._loc_up_tex, 0)
            self._prog_up.setUniformValue(self._loc_up_refraction, 0.08)
            self._prog_up.setUniformValue(self._loc_up_highlight, 0.25)
            self._prog_up.setUniformValue(self._loc_up_time, 0.0)
            self._prog_up.setUniformValue(self._loc_up_panel_size,
                                           float(w), float(h))
            self._prog_up.setUniformValue(self._loc_up_corner_radius, 14.0)
            gl.glDrawArrays(_GL_TRIANGLES, 0, 3)

            self._vao.release()
            self._prog_up.release()
            self._up_fbo.release()

            result_image = self._up_fbo.toImage()
            result = QPixmap.fromImage(result_image)

            self._ctx.doneCurrent()
            return result
        except Exception:
            logger.warning("GPU blur pass failed", exc_info=True)
            self._ctx.doneCurrent()
            return None

    # ------------------------------------------------------------------
    # Mipmap live blur (fast path for drag/zoom)
    # ------------------------------------------------------------------

    def blur_live(self, input_pixmap: QPixmap) -> QPixmap | None:
        """Ultra-fast mipmap blur: glGenerateMipmap + textureLod sampling.

        No custom shader — just GPU-hardware mipmap generation and
        linear-mipmap-linear sampling. ~1ms total.
        """
        if not self._init_gl():
            return None

        w, h = input_pixmap.width(), input_pixmap.height()
        if w <= 0 or h <= 0:
            return None

        if not self._ctx.makeCurrent(self._surface):
            return None

        try:
            # Upload to texture with mipmaps
            image = input_pixmap.toImage().convertToFormat(QImage.Format_RGBA8888)
            tex = QOpenGLTexture(QOpenGLTexture.Target2D)
            tex.setSize(w, h)
            tex.setFormat(QOpenGLTexture.RGBA8_UNorm)
            tex.setMinMagFilters(QOpenGLTexture.Linear, QOpenGLTexture.Linear)
            tex.setWrapMode(QOpenGLTexture.ClampToEdge)
            tex.setMipLevels(tex.maximumMipLevels())
            tex.allocateStorage()
            tex.setData(0, QOpenGLTexture.RGBA, QOpenGLTexture.UInt8, image.constBits())

            # Render to full-res FBO, sampling from mip level
            fbo = QOpenGLFramebufferObject(w, h)
            fbo.bind()
            gl = self._ctx.functions()
            gl.glViewport(0, 0, w, h)
            gl.glClear(_GL_COLOR_BUFFER_BIT)

            # Use a simple copy shader that reads from a specific mip level
            # We reuse the _BLUR_VS and a minimal FS
            if not hasattr(self, '_prog_mip'):
                mip_fs = b"""#version 330 core
in vec2 vTexCoord; out vec4 fragColor;
uniform sampler2D uTexture; uniform float uLod;
void main() { fragColor = textureLod(uTexture, vTexCoord, uLod); }
"""
                self._prog_mip = QOpenGLShaderProgram()
                self._prog_mip.addShaderFromSourceCode(QOpenGLShader.Vertex, _BLUR_VS)
                self._prog_mip.addShaderFromSourceCode(QOpenGLShader.Fragment, mip_fs)
                self._prog_mip.link()
                self._loc_mip_tex = self._prog_mip.uniformLocation(b"uTexture")
                self._loc_mip_lod = self._prog_mip.uniformLocation(b"uLod")

            self._prog_mip.bind()
            self._vao.bind()
            gl.glActiveTexture(_GL_TEXTURE0)
            tex.bind()
            gl.glGenerateMipmap(_GL_TEXTURE_2D)
            self._prog_mip.setUniformValue(self._loc_mip_tex, 0)
            self._prog_mip.setUniformValue(self._loc_mip_lod, 3.0)
            gl.glDrawArrays(_GL_TRIANGLES, 0, 3)
            tex.release()
            self._vao.release()
            self._prog_mip.release()
            fbo.release()

            result_image = fbo.toImage()
            result = QPixmap.fromImage(result_image)

            self._ctx.doneCurrent()
            return result
        except Exception:
            logger.warning("GPU live blur failed", exc_info=True)
            self._ctx.doneCurrent()
            return None

    # ------------------------------------------------------------------
    # Liquid Glass refraction (clear glass — no blur, optical effects only)
    # ------------------------------------------------------------------

    def refract(
        self,
        input_pixmap: QPixmap,
        refraction: float = 0.08,
        highlight_intensity: float = 0.25,
        corner_radius: float = 14.0,
        time_sec: float = 0.0,
    ) -> QPixmap | None:
        """Single-pass Liquid Glass — fiber-optic edge dispersion, no blur.

        Center ~78%: pure passthrough (original color).
        Edge ~22%: fiber-optic RGB dispersion (prism-like color fringe).
        No saturation shift, no warm tint, no noise.

        Returns the final composited QPixmap at input resolution.
        """
        if not self._init_gl():
            return None

        w, h = input_pixmap.width(), input_pixmap.height()
        if w <= 0 or h <= 0:
            return None

        if not self._ctx.makeCurrent(self._surface):
            return None

        try:
            # Re/create resources on size change
            if w != self._pool_w or h != self._pool_h:
                self._pool_w = w
                self._pool_h = h
                self._input_tex = QOpenGLTexture(QOpenGLTexture.Target2D)
                self._input_tex.setSize(w, h)
                self._input_tex.setFormat(QOpenGLTexture.RGBA8_UNorm)
                self._input_tex.setMinMagFilters(QOpenGLTexture.Linear, QOpenGLTexture.Linear)
                self._input_tex.setWrapMode(QOpenGLTexture.ClampToEdge)
                self._input_tex.allocateStorage()
                self._up_fbo = None

            if self._up_fbo is None:
                self._up_fbo = QOpenGLFramebufferObject(w, h)

            # Upload pixmap → texture
            image = input_pixmap.toImage().convertToFormat(QImage.Format_RGBA8888)
            self._input_tex.setData(0, QOpenGLTexture.RGBA, QOpenGLTexture.UInt8,
                                     image.constBits())

            gl = self._ctx.functions()

            # --- Single pass: Liquid Glass refraction ---
            self._prog_up.bind()
            self._vao.bind()
            self._up_fbo.bind()
            gl.glViewport(0, 0, w, h)
            gl.glClear(_GL_COLOR_BUFFER_BIT)

            gl.glActiveTexture(_GL_TEXTURE0)
            self._input_tex.bind()
            self._prog_up.setUniformValue(self._loc_up_tex, 0)
            self._prog_up.setUniformValue(self._loc_up_time, time_sec)
            self._prog_up.setUniformValue(self._loc_up_panel_size,
                                           float(w), float(h))
            self._prog_up.setUniformValue(self._loc_up_corner_radius, corner_radius)
            gl.glDrawArrays(_GL_TRIANGLES, 0, 3)

            self._vao.release()
            self._prog_up.release()
            self._input_tex.release()
            self._up_fbo.release()

            result_image = self._up_fbo.toImage()
            result = QPixmap.fromImage(result_image)

            self._ctx.doneCurrent()
            return result
        except Exception:
            logger.warning("GPU refract failed", exc_info=True)
            self._ctx.doneCurrent()
            return None

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
    and upscaled back afterward. At downsample=2 the pixel count drops to 1/4,
    balancing quality and performance for Liquid Glass aesthetics.

    IMPORTANT: Never call capture() from within the child's paintEvent —
    parent.grab() would render the child, causing recursive paintEvent → stack overflow.
    """

    def __init__(self, child: QWidget, blur_radius: int = 35,
                 capture_target: QWidget | None = None,
                 downsample: int = 2,
                 refraction: float = 0.08,
                 highlight_intensity: float = 0.25,
                 corner_radius: float = 14.0):
        self._child = child
        self._blur_radius = blur_radius
        self._capture_target = capture_target
        self._downsample = max(1, downsample)
        self._refraction = refraction
        self._highlight_intensity = highlight_intensity
        self._corner_radius = corner_radius
        self._time_sec: float = 0.0
        self._cached_pixmap: QPixmap | None = None
        self._cached_geo: QRect | None = None

    @staticmethod
    def _capture_sub_region(gl_widget: QOpenGLWidget, gl_source: QRect) -> QPixmap | None:
        """Capture sub-region via grabFramebuffer + CPU crop.

        Uses Qt's safe grabFramebuffer() (glReadPixels under the hood)
        to read the GL framebuffer, then crops to the panel region.
        No manual GL state manipulation — avoids interering with the
        map's rendering pipeline.
        """
        try:
            gl_widget.makeCurrent()
            fb = gl_widget.grabFramebuffer()
            gl_widget.doneCurrent()
            if fb.isNull():
                return None
            fb = fb.mirrored(False, True)  # GL origin → QImage origin
            dpr = fb.devicePixelRatioF() or 1.0
            if dpr != 1.0:
                src = QRect(
                    int(gl_source.x() * dpr),
                    int(gl_source.y() * dpr),
                    int(gl_source.width() * dpr),
                    int(gl_source.height() * dpr),
                )
            else:
                src = gl_source
            return QPixmap.fromImage(fb.copy(src))
        except Exception:
            try:
                gl_widget.doneCurrent()
            except Exception:
                pass
            return None

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

    def capture(self, live: bool = False) -> QPixmap | None:
        """Capture background + apply Liquid Glass refraction.

        Uses grabFramebuffer() (safe Qt API) to read the GL framebuffer,
        crops to panel region, applies Liquid Glass optical effects via
        GPU shader (refraction, chromatic aberration, specular highlight,
        vignette, tint, noise — no blur).

        When live=True, skips the geometry cache for real-time drag refresh.
        """
        geo = self._child.geometry()
        if geo.isEmpty() or geo.width() <= 0 or geo.height() <= 0:
            return None

        # During live refresh (drag/zoom), skip cache — background content changes
        if not live:
            if self._cached_geo == geo and self._cached_pixmap and not self._cached_pixmap.isNull():
                logger.debug("capture: cache hit")
                return self._cached_pixmap

        target = self._capture_target or self._child.parentWidget()
        if target is None:
            return None

        gl_widget = self._find_gl_widget()

        # ── GPU path: grabFramebuffer → crop → refract ──
        if gl_widget is not None:
            source_rect = self._compute_source_rect()
            if source_rect is not None:
                gl_offset = gl_widget.mapFrom(target, QPoint(0, 0))
                gl_source = QRect(source_rect.topLeft() + gl_offset, source_rect.size())
                gl_source = gl_source.intersected(gl_widget.rect())
                if (not gl_source.isEmpty()
                        and gl_source.width() >= source_rect.width() * 0.5
                        and gl_source.height() >= source_rect.height() * 0.5):
                    cropped = self._capture_sub_region(gl_widget, gl_source)
                    if cropped is not None and not cropped.isNull():
                        engine = _GpuBlurEngine()

                        # Live path: downsample 2x for 4x fewer shader pixels
                        if live and self._downsample > 1:
                            d = self._downsample
                            small_w = max(1, cropped.width() // d)
                            small_h = max(1, cropped.height() // d)
                            shader_input = cropped.scaled(
                                small_w, small_h,
                                Qt.IgnoreAspectRatio,
                                Qt.SmoothTransformation,
                            )
                        else:
                            shader_input = cropped
                            small_w, small_h = cropped.width(), cropped.height()

                        result = engine.refract(
                            input_pixmap=shader_input,
                            refraction=self._refraction,
                            highlight_intensity=self._highlight_intensity,
                            corner_radius=self._corner_radius,
                            time_sec=self._time_sec,
                        )
                        if result is not None and not result.isNull():
                            # Upsample live result back to panel resolution
                            if live and self._downsample > 1:
                                result = result.scaled(
                                    geo.width(), geo.height(),
                                    Qt.IgnoreAspectRatio,
                                    Qt.SmoothTransformation,
                                )
                            if not live:
                                self._cached_pixmap = result
                                self._cached_geo = geo
                            return result

        return self._capture_fallback_cpu(geo, target)

    def capture_live(self) -> QPixmap | None:
        """Real-time Liquid Glass capture — always re-captures, no cache.

        During map drag/zoom the background content changes continuously
        while the panel geometry stays fixed. Bypassing the geometry cache
        ensures every frame gets a fresh capture.
        """
        return self.capture(live=True)

    def refract_raw(self, raw_pixmap: QPixmap, live: bool = False,
                    target_size: QSize | None = None) -> QPixmap | None:
        """Apply Liquid Glass refraction to a pre-captured raw backdrop.

        Used by main_window for shared capture: one grabFramebuffer
        serves both sidebars instead of two independent captures.
        """
        geo = self._child.geometry()
        if live and self._downsample > 1:
            d = self._downsample
            small_w = max(1, raw_pixmap.width() // d)
            small_h = max(1, raw_pixmap.height() // d)
            shader_input = raw_pixmap.scaled(
                small_w, small_h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        else:
            shader_input = raw_pixmap

        engine = _GpuBlurEngine()
        result = engine.refract(
            input_pixmap=shader_input,
            refraction=self._refraction,
            highlight_intensity=self._highlight_intensity,
            corner_radius=self._corner_radius,
            time_sec=self._time_sec,
        )
        if result is None or result.isNull():
            return None

        if live and self._downsample > 1 and target_size is not None:
            result = result.scaled(
                target_size.width(), target_size.height(),
                Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        return result

    def invalidate(self) -> None:
        self._cached_pixmap = None
        self._cached_geo = None

    def _capture_fallback_cpu(self, geo: QRect, target: QWidget) -> QPixmap | None:
        """CPU fallback: grab → downsample → QGraphicsBlurEffect → upsample.

        Used when OpenGL 3.3 is unavailable or GL operations fail.
        Returns the blurred-then-upsampled pixmap (no compositing — the
        FrostedSurfacePainter will composite on top).
        """
        cropped = None
        if self._capture_target is not None:
            source_rect = self._compute_source_rect()
            if source_rect is not None:
                cropped = QPixmap(source_rect.size())
                cropped.fill(Qt.transparent)
                was_visible = self._child.isVisible()
                if was_visible:
                    self._child.setVisible(False)
                target.render(cropped, QPoint(0, 0),
                              QRect(source_rect.topLeft(), source_rect.size()))
                if was_visible:
                    self._child.setVisible(True)

        if cropped is None:
            try:
                full = target.grab()
            except (AttributeError, RuntimeError):
                full = QPixmap(target.size())
                full.fill(Qt.transparent)
                target.render(full)
            if not full.isNull():
                cropped = full.copy(geo)

        if cropped is None or cropped.isNull():
            return None

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

        scene = QGraphicsScene()
        pixmap_item = scene.addPixmap(blur_input)
        blur_effect = QGraphicsBlurEffect()
        blur_effect.setBlurRadius(blur_radius)
        pixmap_item.setGraphicsEffect(blur_effect)
        result = QPixmap(blur_output_size)
        result.fill(Qt.transparent)
        painter = QPainter(result)
        scene.render(painter, QRect(QPoint(0, 0), blur_output_size),
                     QRect(QPoint(0, 0), blur_output_size))
        painter.end()

        if self._downsample > 1:
            result = result.scaled(cropped.size(),
                                   Qt.IgnoreAspectRatio,
                                   Qt.SmoothTransformation)

        self._cached_pixmap = result
        self._cached_geo = geo
        return result

    @classmethod
    def from_tier(cls, child: QWidget, tier,
                  capture_target: QWidget | None = None) -> "BackdropBlurCapture":
        """Create a BackdropBlurCapture from a MaterialTier."""
        return cls(
            child,
            blur_radius=tier.blur_radius,
            capture_target=capture_target,
            downsample=tier.downsample,
            refraction=getattr(tier, 'refraction', 0.08),
            highlight_intensity=tier.highlight_intensity,
            corner_radius=float(tier.border_radius),
        )


# ---------------------------------------------------------------------------
# Frosted surface composition helper
# ---------------------------------------------------------------------------


class FrostedSurfacePainter:
    """Compose frosted glass layers onto a QPainter.

    Layers (bottom → top):
      1. Blurred backdrop pixmap (with pre-blur vibrancy from BackdropBlurCapture)
      2. Adaptive tint color fill (warm base blended with backdrop average)
      3. Specular highlight gradient (135° light reflection) + corner glow
      4. Multi-scale noise texture overlay
      5. Inset highlight (top-edge light catching glass rim)
      6. Directional inner border (top-left bright, bottom-right dark)
    """

    def __init__(
        self,
        tint_color: QColor | None = None,
        border_color: QColor | None = None,
        border_radius: float = 14.0,
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
        self._backdrop_luminance: float = 0.5  # 0-1, updated in paint()

    @classmethod
    def from_tier(cls, tier) -> "FrostedSurfacePainter":
        """Create a FrostedSurfacePainter from a MaterialTier."""
        # Warm white tint (not pure white) to avoid washed-out look
        warm_a = int(tier.tint_alpha * 255)
        return cls(
            tint_color=QColor(255, 250, 242, warm_a),
            border_color=QColor(255, 255, 255, tier.border_alpha),
            border_radius=float(tier.border_radius),
            highlight_intensity=tier.highlight_intensity,
        )

    def paint(
        self,
        painter: QPainter,
        rect: QRect,
        backdrop: QPixmap | None = None,
    ) -> None:
        """Draw the pre-composited Liquid Glass backdrop.

        The backdrop pixmap already contains blur, tint, specular highlight,
        chromatic aberration, refraction, vignette, noise, and rim light —
        all composited in a single GPU shader pass. Only the 1px directional
        inner border is drawn on CPU (trivial cosmetic stroke).
        """
        from PySide6.QtGui import QPainterPath

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)

        clip_path = QPainterPath()
        clip_path.addRoundedRect(rect, self.border_radius, self.border_radius)
        painter.setClipPath(clip_path)

        if backdrop and not backdrop.isNull():
            painter.drawPixmap(rect, backdrop)

        painter.setClipping(False)

        self._draw_directional_border(painter, rect)

        painter.restore()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _draw_specular_highlight(self, painter: QPainter, rect: QRect,
                                  hi: float = 0.6) -> None:
        """Draw a 135° diagonal specular highlight simulating Liquid Glass.

        Light enters from top-left, creates a bright spot near the corner,
        then fades across the panel surface. hi is modulated by backdrop luminance.
        """
        from PySide6.QtGui import QLinearGradient

        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0.0, QColor(255, 255, 255, int(250 * hi)))
        gradient.setColorAt(0.08, QColor(255, 255, 255, int(180 * hi)))
        gradient.setColorAt(0.25, QColor(255, 255, 255, 0))
        gradient.setColorAt(1.0, QColor(0, 0, 0, int(40 * hi)))
        painter.fillRect(rect, gradient)

    def _draw_corner_glow(self, painter: QPainter, rect: QRect,
                           hi: float = 0.6) -> None:
        """Draw subtle light bloom at each rounded corner."""
        from PySide6.QtGui import QRadialGradient

        corners = [
            rect.topLeft(), rect.topRight(),
            rect.bottomLeft(), rect.bottomRight(),
        ]
        radius = 20.0
        for corner in corners:
            gradient = QRadialGradient(corner, radius)
            gradient.setColorAt(0.0, QColor(255, 255, 255, int(80 * hi)))
            gradient.setColorAt(0.5, QColor(255, 255, 255, int(30 * hi)))
            gradient.setColorAt(1.0, QColor(255, 255, 255, 0))
            painter.fillRect(
                QRect(int(corner.x() - radius), int(corner.y() - radius),
                      int(radius * 2), int(radius * 2)),
                gradient,
            )

    def _draw_inset_highlight(self, painter: QPainter, rect: QRect,
                               hi: float = 0.6) -> None:
        """Draw a top-edge inset highlight simulating light on the glass rim."""
        from PySide6.QtGui import QLinearGradient, QPainterPath, QPen

        inset_rect = rect.adjusted(1, 1, -1, -1)
        if inset_rect.width() <= 0 or inset_rect.height() <= 0:
            return

        path = QPainterPath()
        path.addRoundedRect(inset_rect, self.border_radius - 1, self.border_radius - 1)

        gradient = QLinearGradient(inset_rect.topLeft(), inset_rect.bottomLeft())
        gradient.setColorAt(0.0, QColor(255, 255, 255, int(250 * hi)))
        gradient.setColorAt(0.15, QColor(255, 255, 255, int(160 * hi)))
        gradient.setColorAt(0.40, QColor(255, 255, 255, 0))

        pen = QPen(gradient, 1.0)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)

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
            self._backdrop_luminance = 0.5
            return self.tint

        avg_r = r_sum // count
        avg_g = g_sum // count
        avg_b = b_sum // count

        # Perceived luminance (ITU-R BT.601)
        self._backdrop_luminance = (avg_r * 0.299 + avg_g * 0.587 + avg_b * 0.114) / 255.0

        # Blend: 70% base tint, 30% background average
        blend = 0.30
        bt = self._base_tint
        blended_r = int(bt.red() * (1 - blend) + avg_r * blend)
        blended_g = int(bt.green() * (1 - blend) + avg_g * blend)
        blended_b = int(bt.blue() * (1 - blend) + avg_b * blend)

        return QColor(blended_r, blended_g, blended_b, self.tint.alpha())
