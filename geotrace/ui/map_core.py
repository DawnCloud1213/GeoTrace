"""GeoTrace V2.0 瓦片引擎核心 — Web Mercator 投影 + MBTiles 离线瓦片 + TileManager.

设计约束:
  - 纯离线优先: 无 QNetworkAccessManager, 无在线回退.
  - MBTiles 为可选依赖: 文件不存在时自动降级为纯色背景 (Colors.MAP_BG).
  - 瓦片加载不在 paintEvent 内阻塞: TileManager 维护异步加载状态,
    缺失瓦片先用占位色渲染, 后台线程回调后触发 update().

坐标系:
  - 场景坐标 = 瓦片像素 (Web Mercator, zoom 层级浮动).
  - 屏幕坐标 = widget 本地像素.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPixmap, QPixmapCache

logger = logging.getLogger(__name__)

# Web Mercator 常量
TILE_SIZE: int = 256
MAX_ZOOM: int = 18
MIN_ZOOM: int = 3


class MercatorProjection:
    """Web Mercator (EPSG:3857) 投影计算."""

    @staticmethod
    def lnglat_to_pixel(lng: float, lat: float, zoom: float) -> tuple[float, float]:
        """经纬度 → 瓦片像素坐标 (px, py) 于给定 zoom 层级."""
        n = 2.0 ** zoom
        rad = math.radians(lat)
        px = (lng + 180.0) / 360.0 * n * TILE_SIZE
        py = (
            (1.0 - math.log(math.tan(rad) + (1.0 / math.cos(rad))) / math.pi)
            / 2.0
            * n
            * TILE_SIZE
        )
        return px, py

    @staticmethod
    def pixel_to_lnglat(px: float, py: float, zoom: float) -> tuple[float, float]:
        """瓦片像素坐标 → 经纬度."""
        n = 2.0 ** zoom
        lng = px / (n * TILE_SIZE) * 360.0 - 180.0
        lat_rad = math.atan(math.sinh(math.pi * (1.0 - 2.0 * py / (n * TILE_SIZE))))
        lat = math.degrees(lat_rad)
        return lng, lat

    @staticmethod
    def tile_index(px: float, py: float) -> tuple[int, int]:
        """像素坐标 → 瓦片 (x, y) 索引."""
        return int(px // TILE_SIZE), int(py // TILE_SIZE)

    @staticmethod
    def tile_bounds(x: int, y: int, z: int) -> tuple[float, float, float, float]:
        """瓦片索引 → 像素范围 (min_px, min_py, max_px, max_py)."""
        return (
            x * TILE_SIZE,
            y * TILE_SIZE,
            (x + 1) * TILE_SIZE,
            (y + 1) * TILE_SIZE,
        )


class MBTilesProvider:
    """读取本地 .mbtiles SQLite 数据库 (TMS 规范, Y 轴翻转)."""

    def __init__(self, mbtiles_path: str | Path) -> None:
        self._path = Path(mbtiles_path)
        self._conn: Optional[sqlite3.Connection] = None
        if self._path.exists():
            try:
                self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
                self._conn.execute("SELECT 1 FROM tiles LIMIT 1")
                logger.info("MBTiles 已加载: %s", self._path)
            except Exception as e:
                logger.warning("MBTiles 加载失败 %s: %s", self._path, e)
                self._conn = None
        else:
            logger.info("MBTiles 不存在，纯色背景模式: %s", self._path)

    @property
    def available(self) -> bool:
        return self._conn is not None

    def get_tile(self, x: int, y: int, z: int) -> bytes | None:
        """获取瓦片二进制数据 (PNG/JPEG bytes).

        MBTiles 采用 TMS: Y 轴原点在左下角。
        查询前必须翻转: tms_y = (1 << z) - 1 - xyz_y.
        """
        if self._conn is None:
            return None
        try:
            tms_y = (1 << z) - 1 - y
            row = self._conn.execute(
                "SELECT tile_data FROM tiles WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?",
                (z, x, tms_y),
            ).fetchone()
            return row[0] if row else None
        except Exception as e:
            logger.warning("MBTiles 查询失败 z=%d x=%d y=%d: %s", z, x, y, e)
            return None

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None


class TileManager(QObject):
    """管理当前视口瓦片: 内存缓存 → MBTiles → 纯色占位.

    瓦片加载委托给 ThreadPoolExecutor, 完成后通过 tileLoaded Signal
    通知 MapCanvas update().
    """

    tileLoaded = Signal()  # 任意瓦片就绪时通知重绘

    def __init__(
        self,
        providers: dict[str, str | Path] | None = None,
        default_provider: str = "standard",
        placeholder_color: QColor | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._placeholder = placeholder_color or QColor("#FEF9F0")
        self._providers: dict[str, MBTilesProvider] = {}
        if providers:
            for key, path in providers.items():
                provider = MBTilesProvider(path)
                if provider.available:
                    self._providers[key] = provider
        self._active_key = default_provider
        if self._active_key not in self._providers and self._providers:
            self._active_key = next(iter(self._providers))
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tile")
        self._pending: set[tuple[int, int, int]] = set()
        self._cache_prefix = "geotrace_tile"

    @property
    def mbtiles_available(self) -> bool:
        return bool(self._providers)

    @property
    def can_switch(self) -> bool:
        return len(self._providers) > 1

    @property
    def active_key(self) -> str:
        return self._active_key

    def set_active_provider(self, key: str) -> bool:
        if key in self._providers:
            if key != self._active_key:
                self._active_key = key
                self._pending.clear()
            return True
        return False

    def cycle_provider(self) -> str:
        if not self.can_switch:
            return self._active_key
        keys = list(self._providers.keys())
        idx = keys.index(self._active_key)
        next_key = keys[(idx + 1) % len(keys)]
        self.set_active_provider(next_key)
        return next_key

    def _active_mbtiles(self) -> MBTilesProvider | None:
        return self._providers.get(self._active_key)

    def _cache_key(self, x: int, y: int, z: int) -> str:
        return f"{self._cache_prefix}_{self._active_key}_{z}_{x}_{y}"

    def _load_tile_sync(self, x: int, y: int, z: int) -> QPixmap | None:
        """同步瓦片加载 (在 worker 线程中调用)."""
        provider = self._active_mbtiles()
        if provider is None:
            return None
        data = provider.get_tile(x, y, z)
        if not data:
            return None
        pixmap = QPixmap()
        if pixmap.loadFromData(data):
            return pixmap
        return None

    def _on_tile_ready(self, future, x: int, y: int, z: int) -> None:
        """线程池回调 → 主线程插入 QPixmapCache."""
        self._pending.discard((x, y, z))
        try:
            pixmap: QPixmap | None = future.result()
            if pixmap and not pixmap.isNull():
                key = self._cache_key(x, y, z)
                QPixmapCache.insert(key, pixmap)
                self.tileLoaded.emit()
        except Exception as e:
            logger.warning("瓦片加载异常 z=%d x=%d y=%d: %s", z, x, y, e)

    def request_tile(self, x: int, y: int, z: int) -> None:
        """发起异步瓦片请求 (幂等: 已在 pending 或 cache 中则跳过)."""
        key = self._cache_key(x, y, z)
        cached = QPixmap()
        if QPixmapCache.find(key, cached):
            return
        if (x, y, z) in self._pending:
            return
        if not self._active_mbtiles():
            return
        self._pending.add((x, y, z))
        future = self._executor.submit(self._load_tile_sync, x, y, z)
        future.add_done_callback(lambda f, xx=x, yy=y, zz=z: self._on_tile_ready(f, xx, yy, zz))

    def paint_tiles(
        self,
        painter: QPainter,
        viewport_rect: QRect,
        zoom: float,
        center_px: float,
        center_py: float,
    ) -> None:
        """在 painter 上绘制当前视口所需的所有瓦片.

        Args:
            painter: 已准备好的 QPainter.
            viewport_rect: widget 可视矩形.
            zoom: 当前 zoom 层级 (float, 支持分数 zoom).
            center_px, center_py: 视口中心对应的场景像素坐标.
        """
        # 计算视口四个角对应的场景像素范围
        vw = viewport_rect.width()
        vh = viewport_rect.height()
        half_w = vw / 2.0
        half_h = vh / 2.0
        scale = 2.0 ** zoom

        min_px = center_px - half_w
        max_px = center_px + half_w
        min_py = center_py - half_h
        max_py = center_py + half_h

        # 瓦片索引范围
        min_tx = max(int(min_px // TILE_SIZE) - 1, 0)
        max_tx = min(int(max_px // TILE_SIZE) + 1, int(scale) - 1)
        min_ty = max(int(min_py // TILE_SIZE) - 1, 0)
        max_ty = min(int(max_py // TILE_SIZE) + 1, int(scale) - 1)

        # 瓦片到屏幕的缩放: 实际显示尺寸 = TILE_SIZE (因为场景像素 1:1 对应屏幕像素在 zoom 整数时).
        # 当 zoom 为小数时，需要缩放 TILE_SIZE 以匹配.
        tile_display_size = TILE_SIZE * (2.0 ** (zoom - int(zoom))) if zoom != int(zoom) else TILE_SIZE
        # 更简洁的方案: 使用整体变换矩阵把场景像素映射到屏幕.
        # 这里直接在 painter 上变换.
        painter.save()
        # 场景 → 屏幕: translate(-center_px + half_w, -center_py + half_h)
        painter.translate(-center_px + half_w, -center_py + half_h)

        has_any_tile = False
        for ty in range(min_ty, max_ty + 1):
            for tx in range(min_tx, max_tx + 1):
                z_int = int(zoom)
                key = self._cache_key(tx, ty, z_int)
                cached = QPixmap()
                if QPixmapCache.find(key, cached):
                    painter.drawPixmap(tx * TILE_SIZE, ty * TILE_SIZE, cached)
                    has_any_tile = True
                else:
                    # 占位色
                    painter.fillRect(
                        QRectF(tx * TILE_SIZE, ty * TILE_SIZE, TILE_SIZE, TILE_SIZE),
                        QBrush(self._placeholder),
                    )
                    # 按需异步请求
                    self.request_tile(tx, ty, z_int)

        painter.restore()

        # 若当前 provider 不可用且无任何瓦片, 整个视口填充背景色
        if not has_any_tile and self._active_mbtiles() is None:
            painter.fillRect(viewport_rect, QBrush(self._placeholder))

    def shutdown(self) -> None:
        """清理资源."""
        self._executor.shutdown(wait=False)
        for provider in self._providers.values():
            provider.close()
