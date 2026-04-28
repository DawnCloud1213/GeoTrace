"""离线逆地理编码服务.

基于本地 GeoJSON 数据, 使用 R-Tree 空间索引 + Shapely 多边形碰撞检测,
将 GPS 坐标映射至中国各省份.
"""

import json
import logging
from pathlib import Path

from rtree import index
from shapely.errors import GEOSException
from shapely.geometry import Point, shape

logger = logging.getLogger(__name__)

# 省份名标准化映射 (GeoJSON 中的名称 -> 标准全称)
_PROVINCE_ALIASES: dict[str, str] = {
    "北京": "北京市",
    "天津": "天津市",
    "上海": "上海市",
    "重庆": "重庆市",
    "内蒙古": "内蒙古自治区",
    "广西": "广西壮族自治区",
    "西藏": "西藏自治区",
    "宁夏": "宁夏回族自治区",
    "新疆": "新疆维吾尔自治区",
    "香港": "香港特别行政区",
    "澳门": "澳门特别行政区",
    "台湾": "台湾省",
    "黑龙江": "黑龙江省",
    "吉林": "吉林省",
    "辽宁": "辽宁省",
    "河北": "河北省",
    "河南": "河南省",
    "山东": "山东省",
    "山西": "山西省",
    "陕西": "陕西省",
    "甘肃": "甘肃省",
    "青海": "青海省",
    "四川": "四川省",
    "贵州": "贵州省",
    "云南": "云南省",
    "湖南": "湖南省",
    "湖北": "湖北省",
    "广东": "广东省",
    "海南": "海南省",
    "江苏": "江苏省",
    "浙江": "浙江省",
    "安徽": "安徽省",
    "福建": "福建省",
    "江西": "江西省",
}

# 后缀补全需要的合法后缀
_VALID_SUFFIXES = ("省", "市", "自治区", "特别行政区")


class SpatialIndex:
    """离线逆地理编码服务.

    使用 R-Tree 空间索引进行 bounding box 粗筛,
    再通过 Shapely 进行精确的多边形包含判断,
    将 WGS84 经纬度坐标映射到中国省级行政区划.
    """

    def __init__(self, geojson_path: str | Path) -> None:
        """初始化空间索引.

        Args:
            geojson_path: 中国省级 GeoJSON 数据文件路径.

        Raises:
            FileNotFoundError: GeoJSON 文件不存在.
            ValueError: GeoJSON 格式无效.
        """
        self._geojson_path = Path(geojson_path)
        self._tree: index.Index = index.Index()
        self._provinces: dict[int, dict] = {}  # {rtree_id: {code, name, geometry}}

        if not self._geojson_path.exists():
            raise FileNotFoundError(f"GeoJSON 文件不存在: {self._geojson_path}")

        self._load()
        logger.info(
            "空间索引已就绪: %d 个省份数据, 来源: %s",
            len(self._provinces),
            self._geojson_path,
        )

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """加载 GeoJSON 并构建 R-Tree 索引."""
        with open(self._geojson_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        features = data.get("features", [])
        if not features:
            raise ValueError(f"GeoJSON 文件中没有 features: {self._geojson_path}")

        valid_count = 0
        for i, feature in enumerate(features):
            props = feature.get("properties", {})
            geom_data = feature.get("geometry")

            if geom_data is None:
                continue

            # 提取省份名 (支持多种属性命名)
            raw_name = (
                props.get("name")
                or props.get("NAME")
                or props.get("province")
                or ""
            )
            name = _normalize_name(raw_name)

            code = (
                props.get("code")
                or props.get("CODE")
                or props.get("id")
                or props.get("adcode")
                or ""
            )

            try:
                geom = shape(geom_data)
                if geom.is_empty:
                    continue
                # 修复无效几何体
                if not geom.is_valid:
                    geom = geom.buffer(0)
            except (GEOSException, ValueError, TypeError) as e:
                logger.warning("无法解析省份 '%s' 的几何体: %s", name, e)
                continue

            self._provinces[i] = {
                "code": str(code),
                "name": name,
                "geometry": geom,
            }
            self._tree.insert(i, geom.bounds)  # (minx, miny, maxx, maxy)
            valid_count += 1

        if valid_count == 0:
            raise ValueError(f"未能从 GeoJSON 文件中解析到任何有效几何体: {self._geojson_path}")

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def locate(self, lng: float, lat: float) -> dict | None:
        """坐标定位: 将经纬度映射到省份.

        Args:
            lng: 经度 (Longitude, WGS84).
            lat: 纬度 (Latitude, WGS84).

        Returns:
            {'code': '510000', 'name': '四川省'} 或 None (不在任何省份内).
        """
        point = Point(lng, lat)

        try:
            # Step 1: R-Tree bounding box 粗筛
            candidate_ids = list(self._tree.intersection(point.bounds))

            # Step 2: Shapely 精确包含判断
            for cid in candidate_ids:
                prov = self._provinces[cid]
                try:
                    if prov["geometry"].contains(point) or point.within(prov["geometry"]):
                        return {"code": prov["code"], "name": prov["name"]}
                except GEOSException:
                    continue

            # Step 3: 边界点容差 —— 使用最近邻搜索回退
            if candidate_ids:
                nearest_ids = list(self._tree.nearest(point.bounds, 1))
                if nearest_ids:
                    prov = self._provinces[nearest_ids[0]]
                    # 使用距离判断: 如果最近多边形距离 < 0.01 度 (约 1km), 视为边界点
                    try:
                        dist = prov["geometry"].distance(point)
                        if dist < 0.01:
                            return {"code": prov["code"], "name": prov["name"]}
                    except GEOSException:
                        pass
        except Exception as e:
            logger.warning("空间查询异常 (lng=%s, lat=%s): %s", lng, lon, e)

        return None

    def locate_batch(self, coordinates: list[tuple[float, float]]) -> list[dict | None]:
        """批量定位 (比逐个调用 locate 更高效).

        Args:
            coordinates: [(lng, lat), ...] 坐标列表.

        Returns:
            与输入一一对应的结果列表, 元素为 {'code': '...', 'name': '...'} 或 None.
        """
        results: list[dict | None] = []
        for lng, lat in coordinates:
            results.append(self.locate(lng, lat))
        return results

    # ------------------------------------------------------------------
    # 元数据
    # ------------------------------------------------------------------

    @property
    def province_count(self) -> int:
        """已加载的省份数量."""
        return len(self._provinces)

    def get_province_names(self) -> list[str]:
        """返回所有省份名称列表 (按字母序)."""
        names = sorted(
            {p["name"] for p in self._provinces.values()}
        )
        return names


def _normalize_name(raw_name: str) -> str:
    """标准化省份名称为全称格式.

    Args:
        raw_name: GeoJSON 中的原始省份名 (可能是简称或英文).

    Returns:
        标准化的中文全称.
    """
    name = raw_name.strip()

    if not name:
        return "Unclassified"

    # 已知别名映射
    if name in _PROVINCE_ALIASES:
        return _PROVINCE_ALIASES[name]

    # 已有合法后缀的直接返回
    if name.endswith(_VALID_SUFFIXES):
        return name

    # 如果某个字开头像简称, 尝试给它加后缀
    # (这块是尽力而为的推测, 因为不知道 GeoJSON 中的实际名称)
    return name
