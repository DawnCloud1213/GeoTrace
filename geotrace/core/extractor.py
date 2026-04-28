"""EXIF GPS 元数据提取器.

从照片文件中提取 GPS 坐标、拍摄日期、相机型号等 EXIF 信息,
返回统一的 PhotoMetadata 数据模型.
"""

import hashlib
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import exifread
from PIL import Image, UnidentifiedImageError

from geotrace.core.models import PhotoMetadata

logger = logging.getLogger(__name__)

# 支持的照片扩展名
SUPPORTED_EXTENSIONS = frozenset({
    ".jpg", ".jpeg", ".png", ".tiff", ".tif", ".heic", ".heif",
    ".webp", ".bmp",
})


class EXIFExtractor:
    """照片 EXIF 元数据提取器.

    结合 Pillow (图像基本信息) 与 exifread (EXIF GPS 标签),
    提供统一的照片元数据提取接口.
    """

    @staticmethod
    def extract(file_path: str | Path) -> PhotoMetadata:
        """提取单张照片的完整元数据.

        Args:
            file_path: 照片文件的绝对路径.

        Returns:
            PhotoMetadata 实例. 若提取失败则返回仅有文件信息的实例
            (latitude/longitude 为 None).
        """
        file_path = str(file_path)
        path_obj = Path(file_path)
        stat = os.stat(file_path)

        meta = PhotoMetadata(
            file_path=file_path,
            file_name=path_obj.name,
            file_size=stat.st_size,
            file_mtime=stat.st_mtime,
        )

        try:
            meta.md5_hash = EXIFExtractor._fast_md5(file_path)
        except (OSError, PermissionError) as e:
            logger.warning("无法计算 MD5: %s - %s", file_path, e)

        # Pillow: 图像基本信息
        try:
            with Image.open(file_path) as img:
                meta.width, meta.height = img.size
                # 提取 EXIF Orientation (Pillow 方式)
                exif_data = img.getexif()
                if exif_data:
                    meta.orientation = exif_data.get(0x0112, 1)  # Orientation tag
        except (UnidentifiedImageError, OSError, Exception) as e:
            logger.warning("Pillow 无法打开图片: %s - %s", file_path, e)
            return meta

        # exifread: GPS 与其他 EXIF 标签
        try:
            with open(file_path, "rb") as f:
                tags = exifread.process_file(f, details=False)
        except (OSError, Exception) as e:
            logger.warning("exifread 处理失败: %s - %s", file_path, e)
            return meta

        # GPS 坐标解析
        lat, lon, alt = EXIFExtractor._parse_gps(tags)
        meta.latitude = lat
        meta.longitude = lon
        meta.altitude = alt

        # 拍摄日期
        meta.date_taken = EXIFExtractor._parse_date(tags)

        # 相机型号
        meta.camera_model = EXIFExtractor._parse_camera(tags)

        return meta

    # ------------------------------------------------------------------
    # GPS 坐标解析
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_gps(tags: dict) -> tuple[Optional[float], Optional[float], Optional[float]]:
        """从 exifread 标签中解析 GPS 坐标.

        Returns:
            (latitude, longitude, altitude), 缺失时对应值为 None.
        """
        lat_tag = tags.get("GPS GPSLatitude")
        lat_ref = tags.get("GPS GPSLatitudeRef")
        lon_tag = tags.get("GPS GPSLongitude")
        lon_ref = tags.get("GPS GPSLongitudeRef")
        alt_tag = tags.get("GPS GPSAltitude")

        lat = EXIFExtractor._dms_to_decimal(lat_tag, lat_ref) if lat_tag and lat_ref else None
        lon = EXIFExtractor._dms_to_decimal(lon_tag, lon_ref) if lon_tag and lon_ref else None
        alt = float(alt_tag.values[0]) if alt_tag else None

        # 过滤无效坐标 (0, 0) 或超出中国经纬度范围
        if lat is not None and lon is not None:
            if abs(lat) < 0.001 and abs(lon) < 0.001:
                logger.debug("GPS 坐标为 (0,0), 视为无效: 标记为无 GPS")
                return None, None, alt
            if not (3.0 <= lat <= 54.0 and 73.0 <= lon <= 136.0):
                logger.debug("GPS 坐标超出中国范围: (%s, %s), 标记为无 GPS", lat, lon)
                return None, None, alt

        return lat, lon, alt

    @staticmethod
    def _dms_to_decimal(dms_tag, ref_tag) -> Optional[float]:
        """将 EXIF 度分秒转换为十进制小数度.

        Args:
            dms_tag: exifread 的 GPSLatitude/GPSLongitude 标签 (Ratio 值列表).
            ref_tag: 'N'/'S' 或 'E'/'W' 参考方向.

        Returns:
            十进制经纬度值 (float), 或 None.
        """
        try:
            values = dms_tag.values
            degrees = float(values[0])
            minutes = float(values[1]) / 60.0
            seconds = float(values[2]) / 3600.0
        except (IndexError, TypeError, ValueError, AttributeError) as e:
            logger.debug("DMS 转换失败: %s", e)
            return None

        decimal = degrees + minutes + seconds

        ref_str = str(ref_tag.values) if hasattr(ref_tag, "values") else str(ref_tag)
        if ref_str in ("S", "W"):
            decimal = -decimal

        return round(decimal, 6)

    # ------------------------------------------------------------------
    # 其他 EXIF 标签解析
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_date(tags: dict) -> Optional[str]:
        """解析拍摄日期, 返回 ISO 8601 格式字符串."""
        date_tag = tags.get("EXIF DateTimeOriginal") or tags.get("Image DateTime")

        if date_tag is None:
            return None

        date_str = str(date_tag.values) if hasattr(date_tag, "values") else str(date_tag)

        try:
            dt = datetime.strptime(date_str, "%Y:%m:%d %H:%M:%S")
            return dt.isoformat()
        except (ValueError, TypeError):
            return date_str

    @staticmethod
    def _parse_camera(tags: dict) -> Optional[str]:
        """解析相机型号."""
        make = tags.get("Image Make")
        model = tags.get("Image Model")

        make_str = str(make.values) if hasattr(make, "values") else (str(make) if make else "")
        model_str = str(model.values) if hasattr(model, "values") else (str(model) if model else "")

        if make_str and model_str:
            return f"{make_str.strip()} {model_str.strip()}"
        if model_str:
            return model_str.strip()
        if make_str:
            return make_str.strip()
        return None

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _fast_md5(file_path: str) -> str:
        """计算文件前 64KB 的 MD5 (快速去重)."""
        md5 = hashlib.md5()
        with open(file_path, "rb") as f:
            md5.update(f.read(65536))
        return md5.hexdigest()

    @staticmethod
    def is_supported(file_path: str | Path) -> bool:
        """检查文件扩展名是否为支持的图片格式."""
        return Path(file_path).suffix.lower() in SUPPORTED_EXTENSIONS
