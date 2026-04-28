"""ECharts 中国地图视图 — QWebEngineView 封装.

通过 QWebEngineView 加载基于 ECharts 的中国地图,
使用 QWebChannel 实现 Python <-> JS 双向通信.
"""

import json
import logging
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView

from geotrace.ui.bridge import MapBridge

logger = logging.getLogger(__name__)

_RESOURCES_DIR = Path(__file__).parent / "resources"


class _MapPage(QWebEnginePage):
    """自定义页面, 捕获 JS 控制台消息用于诊断."""

    def javaScriptConsoleMessage(self, level, message, line_number, source_id):
        try:
            lv = level.value if hasattr(level, "value") else int(level)
        except (TypeError, ValueError):
            lv = 0
        names = {
            QWebEnginePage.JavaScriptConsoleMessageLevel.InfoMessageLevel.value: "JS",
            QWebEnginePage.JavaScriptConsoleMessageLevel.WarningMessageLevel.value: "JS-WARN",
            QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel.value: "JS-ERR",
        }
        logger.info("[%s] %s (line %s)", names.get(lv, f"JS-{lv}"), message, line_number)


class MapView(QWebEngineView):
    """ECharts 中国地图视图组件."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._page = _MapPage(self)
        self.setPage(self._page)

        self._bridge = MapBridge()
        self._channel = QWebChannel()
        self._channel.registerObject("bridge", self._bridge)
        self._page.setWebChannel(self._channel)

        # 允许本地文件访问
        s = self._page.settings()
        s.setAttribute(s.WebAttribute.LocalContentCanAccessFileUrls, True)
        s.setAttribute(s.WebAttribute.JavascriptEnabled, True)
        s.setAttribute(s.WebAttribute.ErrorPageEnabled, False)

        # 性能优化: 禁用 WebGL (ECharts 用 Canvas 2D 就够了)
        s.setAttribute(s.WebAttribute.WebGLEnabled, False)
        s.setAttribute(s.WebAttribute.Accelerated2dCanvasEnabled, True)

        self._geo_json_loaded = False
        self._page.loadFinished.connect(self._on_load_finished)

    @property
    def bridge(self) -> MapBridge:
        return self._bridge

    def _on_load_finished(self, ok: bool) -> None:
        if ok:
            logger.info("地图页面加载完成")
        else:
            logger.error("地图页面加载失败!")

    # ------------------------------------------------------------------
    # 地图加载
    # ------------------------------------------------------------------

    def load_map(self, geojson_path: str | Path) -> bool:
        """加载中国地图.

        读取 HTML 模板, 将 GeoJSON 注入为 JS 变量,
        通过 setHtml 加载 (JS 资源通过 baseUrl 相对路径引用).
        """
        geojson_path = Path(geojson_path)

        if not geojson_path.exists():
            logger.error("GeoJSON 文件不存在: %s", geojson_path)
            self.setHtml(self._error_html(f"GeoJSON 文件不存在:<br>{geojson_path}"))
            return False

        try:
            with open(geojson_path, "r", encoding="utf-8") as f:
                geojson_data = json.load(f)
            # 简化多边形以减少 ECharts 渲染顶点数 (容忍 0.01 度 ≈ 1.1km)
            geojson_data = self._simplify_geojson(geojson_data, tolerance=0.02)
            geojson_str = json.dumps(geojson_data, ensure_ascii=False)
        except (json.JSONDecodeError, OSError) as e:
            logger.error("GeoJSON 解析失败: %s", e)
            self.setHtml(self._error_html(f"GeoJSON 解析失败:<br>{e}"))
            return False

        html_template = (_RESOURCES_DIR / "map.html").read_text(encoding="utf-8")

        # 在 </body> 前注入 GeoJSON 和 initMap 调用
        injection = (
            "<script>var _GEOJSON_DATA = "
            + geojson_str
            + ";\ninitMap(_GEOJSON_DATA);</script>\n</body>"
        )
        html = html_template.replace("</body>", injection)

        # baseUrl 指向 resources 目录, 使 echarts.min.js / qwebchannel.js 可被加载
        base_url = QUrl.fromLocalFile(str(_RESOURCES_DIR / "map.html"))
        self.setHtml(html, base_url)
        self._geo_json_loaded = True
        logger.info("地图已加载 (baseUrl: %s)", _RESOURCES_DIR)
        return True

    # ------------------------------------------------------------------
    # 数据更新
    # ------------------------------------------------------------------

    def update_stats(self, stats: list[dict]) -> None:
        if not self._geo_json_loaded:
            return
        json_str = json.dumps(stats, ensure_ascii=False)
        self._bridge.provinceStatsChanged.emit(json_str)

    def highlight(self, province_name: str) -> None:
        self._bridge.highlightProvince.emit(province_name)

    # ------------------------------------------------------------------
    # GeoJSON 优化
    # ------------------------------------------------------------------

    @staticmethod
    def _simplify_geojson(geojson: dict, tolerance: float = 0.01) -> dict:
        """使用 Douglas-Peucker 算法简化 GeoJSON 多边形.

        tolerance=0.01 度约等于 1.1km, 对省级地图视觉无损,
        但可将顶点数减少 60-90%, 大幅提升 ECharts 渲染帧率.
        """
        from shapely.geometry import shape, mapping
        from shapely.ops import unary_union

        simplified_features = []
        for feature in geojson.get("features", []):
            geom_data = feature.get("geometry")
            if not geom_data:
                simplified_features.append(feature)
                continue
            try:
                geom = shape(geom_data)
                if geom.is_empty:
                    simplified_features.append(feature)
                    continue
                # 修复无效几何体
                if not geom.is_valid:
                    geom = geom.buffer(0)
                simplified = geom.simplify(tolerance, preserve_topology=True)
                new_feature = dict(feature)
                new_feature["geometry"] = mapping(simplified)
                simplified_features.append(new_feature)
            except Exception:
                simplified_features.append(feature)

        return {"type": "FeatureCollection", "features": simplified_features}

    # ------------------------------------------------------------------
    # 错误页面
    # ------------------------------------------------------------------

    @staticmethod
    def _error_html(message: str) -> str:
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><style>
* {{ margin: 0; padding: 0; }}
body {{ display: flex; align-items: center; justify-content: center;
        height: 100vh; font-family: sans-serif; background: #1a1a2e;
        color: #e94560; }}
.container {{ text-align: center; padding: 40px; }}
h2 {{ margin-bottom: 16px; }}
p {{ color: #aaa; font-size: 14px; }}
</style></head>
<body><div class="container">
<h2>地图加载失败</h2><p>{message}</p>
</div></body></html>"""
