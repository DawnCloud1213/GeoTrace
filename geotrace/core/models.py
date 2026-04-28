"""EXIF 提取结果与照片元数据模型."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PhotoMetadata:
    """从 EXIF 提取的照片元数据."""

    file_path: str
    file_name: str
    file_size: int = 0
    file_mtime: float = 0.0
    md5_hash: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    altitude: Optional[float] = None
    province_code: Optional[str] = None
    province_name: Optional[str] = None
    date_taken: Optional[str] = None
    camera_model: Optional[str] = None
    orientation: int = 1
    thumbnail_path: Optional[str] = None


@dataclass
class ProvinceStat:
    """省份统计信息."""

    province_code: str
    province_name: str
    photo_count: int
