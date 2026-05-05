"""GeoTrace V2.0 瓦片引擎核心 — Web Mercator 投影 + 在线/离线混合瓦片 + TileManager.

设计约束:
  - 在线优先，离线兜底: NetworkTileProvider (QNetworkAccessManager + QNetworkDiskCache)
    加载在线 XYZ 瓦片，断网时依赖 disk cache 或 fallback 到 MBTiles / 纯色背景.
  - 瓦片加载不在 paintEvent 内阻塞: TileManager 维护异步加载状态,
    缺失瓦片先用占位色渲染, 后台异步回调后触发 update().

坐标系:
  - 场景坐标 = 瓦片像素 (Web Mercator, zoom 层级浮动).
  - 屏幕坐标 = widget 本地像素.
"""

from __future__ import annotations

import logging
import math
import random
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from PySide6.QtCore import (
    QObject,
    QStandardPaths,
    QUrl,
    QRect,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QBrush, QColor, QPainter, QPixmap, QPixmapCache
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkDiskCache,
    QNetworkReply,
    QNetworkRequest,
)

logger = logging.getLogger(__name__)

# Web Mercator 常量
TILE_SIZE: int = 256
MAX_ZOOM: int = 18
MIN_ZOOM: int = 3

# 默认在线瓦片地址：高德矢量中文简约地图
_DEFAULT_TILE_URL = "https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}"


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
            logger.info("MBTiles 不存在: %s", self._path)

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


class NetworkTileProvider(QObject):
    """在线 XYZ 瓦片下载器 — QNetworkAccessManager + QNetworkDiskCache.

    Signal:
        tileReady(x, y, z, data): 瓦片下载成功，data 为二进制图像字节.
        tileFailed(x, y, z, error): 瓦片下载失败.
    """

    tileReady = Signal(int, int, int, bytes)
    tileFailed = Signal(int, int, int, str)

    def __init__(
        self,
        url_template: str = _DEFAULT_TILE_URL,
        cache_size_mb: int = 500,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._url_template = url_template
        self._nam = QNetworkAccessManager(self)

        # 配置 QNetworkDiskCache
        self._disk_cache = QNetworkDiskCache(self)
        cache_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
        if not cache_dir:
            cache_dir = str(Path.home() / ".cache")
        self._disk_cache.setCacheDirectory(f"{cache_dir}/geotrace_tiles")
        self._disk_cache.setMaximumCacheSize(cache_size_mb * 1024 * 1024)
        self._nam.setCache(self._disk_cache)

        self._pending: set[tuple[int, int, int]] = set()

    @property
    def available(self) -> bool:
        """网络 Provider 始终视为可用 (disk cache 可在断网时提供数据)."""
        return True

    def request_tile(self, x: int, y: int, z: int) -> None:
        """发起异步瓦片请求 (幂等)."""
        if (x, y, z) in self._pending:
            return
        self._pending.add((x, y, z))

        subdomain = random.choice("1234")
        url = QUrl(self._url_template.format(x=x, y=y, z=z, s=subdomain))
        req = QNetworkRequest(url)
        req.setHeader(
            QNetworkRequest.KnownHeaders.UserAgentHeader,
            b"GeoTrace/0.1.0 (private offline photo app; contact: geotrace@local)",
        )
        req.setAttribute(
            QNetworkRequest.Attribute.CacheLoadControlAttribute,
            QNetworkRequest.CacheLoadControl.PreferCache,
        )
        reply = self._nam.get(req)
        reply.finished.connect(
            lambda r=reply, xx=x, yy=y, zz=z: self._on_reply_finished(r, xx, yy, zz)
        )

    def _on_reply_finished(self, reply: QNetworkReply, x: int, y: int, z: int) -> None:
        self._pending.discard((x, y, z))
        try:
            if reply.error() == QNetworkReply.NetworkError.NoError:
                data = reply.readAll().data()
                if data:
                    self.tileReady.emit(x, y, z, data)
                else:
                    self.tileFailed.emit(x, y, z, "Empty response")
            else:
                self.tileFailed.emit(x, y, z, reply.errorString())
        except Exception as e:
            logger.warning("网络瓦片回调异常 z=%d x=%d y=%d: %s", z, x, y, e)
            self.tileFailed.emit(x, y, z, str(e))
        finally:
            reply.deleteLater()


class TileManager(QObject):
    """管理当前视口瓦片: 内存缓存 → 在线瓦片 (Network) / 离线瓦片 (MBTiles) → 纯色占位.

    网络瓦片通过 QNetworkAccessManager 异步加载，MBTiles 通过 ThreadPoolExecutor 异步加载。
    任意瓦片就绪后通过 tileLoaded Signal 通知 MapCanvas update().
    """

    tileLoaded = Signal()  # 任意瓦片就绪时通知重绘

    def __init__(
        self,
        mbtiles_providers: dict[str, str | Path] | None = None,
        enable_network: bool = True,
        network_url_template: str = _DEFAULT_TILE_URL,
        placeholder_color: QColor | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._placeholder = placeholder_color or QColor("#FEF9F0")

        # MBTiles 离线 Provider
        self._mbtiles: dict[str, MBTilesProvider] = {}
        if mbtiles_providers:
            for key, path in mbtiles_providers.items():
                provider = MBTilesProvider(path)
                if provider.available:
                    self._mbtiles[key] = provider

        # 在线 Provider
        self._network: NetworkTileProvider | None = None
        if enable_network:
            self._network = NetworkTileProvider(network_url_template, parent=self)
            self._network.tileReady.connect(self._on_network_tile_ready)
            self._network.tileFailed.connect(self._on_network_tile_failed)

        # 决定默认 active_key: 优先在线，其次第一个 MBTiles
        if self._network:
            self._active_key = "online"
        elif self._mbtiles:
            self._active_key = next(iter(self._mbtiles))
        else:
            self._active_key = "placeholder"

        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tile")
        self._pending: set[tuple[int, int, int]] = set()
        self._cache_prefix = "geotrace_tile"

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def mbtiles_available(self) -> bool:
        return bool(self._mbtiles)

    @property
    def network_available(self) -> bool:
        return self._network is not None

    @property
    def can_switch(self) -> bool:
        keys = self._provider_keys()
        return len(keys) > 1

    @property
    def active_key(self) -> str:
        return self._active_key

    def _provider_keys(self) -> list[str]:
        keys: list[str] = []
        if self._network:
            keys.append("online")
        keys.extend(self._mbtiles.keys())
        if not keys:
            keys.append("placeholder")
        return keys

    # ------------------------------------------------------------------
    # Provider 切换
    # ------------------------------------------------------------------

    def set_active_provider(self, key: str) -> bool:
        if key == self._active_key:
            return True
        if key == "online" and self._network:
            self._active_key = key
            self._pending.clear()
            return True
        if key in self._mbtiles:
            self._active_key = key
            self._pending.clear()
            return True
        if key == "placeholder":
            self._active_key = key
            self._pending.clear()
            return True
        return False

    def cycle_provider(self) -> str:
        """循环切换 Provider: online → mbtiles... → placeholder → online..."""
        keys = self._provider_keys()
        if len(keys) <= 1:
            return self._active_key
        try:
            idx = keys.index(self._active_key)
        except ValueError:
            idx = -1
        next_key = keys[(idx + 1) % len(keys)]
        self.set_active_provider(next_key)
        return next_key

    # ------------------------------------------------------------------
    # 瓦片加载
    # ------------------------------------------------------------------

    def _cache_key(self, x: int, y: int, z: int) -> str:
        return f"{self._cache_prefix}_{self._active_key}_{z}_{x}_{y}"

    def _load_mbtiles_sync(self, x: int, y: int, z: int) -> QPixmap | None:
        """同步 MBTiles 加载 (在 worker 线程中调用)."""
        provider = self._mbtiles.get(self._active_key)
        if provider is None:
            return None
        data = provider.get_tile(x, y, z)
        if not data:
            return None
        pixmap = QPixmap()
        if pixmap.loadFromData(data):
            return pixmap
        return None

    def _on_mbtiles_ready(self, future, x: int, y: int, z: int) -> None:
        """ThreadPoolExecutor 回调 → 主线程插入 QPixmapCache."""
        self._pending.discard((x, y, z))
        try:
            pixmap: QPixmap | None = future.result()
            if pixmap and not pixmap.isNull():
                key = self._cache_key(x, y, z)
                QPixmapCache.insert(key, pixmap)
                self.tileLoaded.emit()
        except Exception as e:
            logger.warning("MBTiles 加载异常 z=%d x=%d y=%d: %s", z, x, y, e)

    def _on_network_tile_ready(self, x: int, y: int, z: int, data: bytes) -> None:
        """NetworkTileProvider 下载成功回调."""
        self._pending.discard((x, y, z))
        pixmap = QPixmap()
        if pixmap.loadFromData(data):
            key = self._cache_key(x, y, z)
            QPixmapCache.insert(key, pixmap)
            self.tileLoaded.emit()
        else:
            logger.warning("网络瓦片解码失败 z=%d x=%d y=%d", z, x, y)

    def _on_network_tile_failed(self, x: int, y: int, z: int, error: str) -> None:
        """NetworkTileProvider 下载失败回调."""
        self._pending.discard((x, y, z))
        logger.debug("网络瓦片加载失败 z=%d x=%d y=%d: %s", z, x, y, error)

    def request_tile(self, x: int, y: int, z: int) -> None:
        """发起异步瓦片请求 (幂等: 已在 pending 或 cache 中则跳过)."""
        key = self._cache_key(x, y, z)
        cached = QPixmap()
        if QPixmapCache.find(key, cached):
            return
        if (x, y, z) in self._pending:
            return

        if self._active_key == "online" and self._network:
            self._pending.add((x, y, z))
            self._network.request_tile(x, y, z)
        elif self._active_key in self._mbtiles:
            self._pending.add((x, y, z))
            future = self._executor.submit(self._load_mbtiles_sync, x, y, z)
            future.add_done_callback(
                lambda f, xx=x, yy=y, zz=z: self._on_mbtiles_ready(f, xx, yy, zz)
            )
        # placeholder 模式不发起任何请求

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------

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
        vw = viewport_rect.width()
        vh = viewport_rect.height()
        half_w = vw / 2.0
        half_h = vh / 2.0

        z_int = int(zoom)

        # -- stable_z: always follow the current zoom --
        # Old tiles at the previous zoom are scaled (slightly blurry)
        # while new tiles load asynchronously and appear seamlessly.
        render_z = z_int
        zoom_scale = 2.0 ** (zoom - render_z)
        world_tiles = 1 << render_z
        max_tile_index = world_tiles - 1

        center_px_z = center_px / zoom_scale
        center_py_z = center_py / zoom_scale

        min_px = center_px_z - half_w / zoom_scale
        max_px = center_px_z + half_w / zoom_scale
        min_py = center_py_z - half_h / zoom_scale
        max_py = center_py_z + half_h / zoom_scale

        min_tx = max(int(min_px // TILE_SIZE) - 1, 0)
        max_tx = min(int(max_px // TILE_SIZE) + 1, max_tile_index)
        min_ty = max(int(min_py // TILE_SIZE) - 1, 0)
        max_ty = min(int(max_py // TILE_SIZE) + 1, max_tile_index)

        painter.save()
        painter.translate(half_w, half_h)
        painter.scale(zoom_scale, zoom_scale)
        painter.translate(-center_px_z, -center_py_z)

        has_any_tile = False
        for ty in range(min_ty, max_ty + 1):
            for tx in range(min_tx, max_tx + 1):
                key = self._cache_key(tx, ty, render_z)
                cached = QPixmap()
                if QPixmapCache.find(key, cached):
                    painter.drawPixmap(tx * TILE_SIZE, ty * TILE_SIZE, cached)
                    has_any_tile = True
                else:
                    painter.fillRect(
                        QRectF(tx * TILE_SIZE, ty * TILE_SIZE, TILE_SIZE, TILE_SIZE),
                        QBrush(self._placeholder),
                    )
                    self.request_tile(tx, ty, render_z)

        painter.restore()

        if not has_any_tile and self._active_key == "placeholder":
            painter.fillRect(viewport_rect, QBrush(self._placeholder))

    def shutdown(self) -> None:
        """清理资源."""
        self._executor.shutdown(wait=False)
        for provider in self._mbtiles.values():
            provider.close()
        if self._network:
            self._network.deleteLater()
