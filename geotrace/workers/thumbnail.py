"""缩略图生成异步 Worker — 批量生成照片缩略图并缓存.

在 QThread 中运行, 生成 320px 长边 JPEG 缩略图,
存储到磁盘缓存目录, 并更新数据库.
"""

import logging
import os
from pathlib import Path

from PIL import Image, ImageOps
from PySide6.QtCore import Signal, Slot

from geotrace.database.manager import DatabaseManager
from geotrace.workers import Worker

logger = logging.getLogger(__name__)

# 缩略图参数
THUMBNAIL_LONG_SIDE = 320
THUMBNAIL_QUALITY = 75
THUMBNAIL_FORMAT = "JPEG"


def get_thumbnail_cache_dir() -> Path:
    """获取缩略图磁盘缓存目录.

    Windows: %APPDATA%/GeoTrace/thumbnails/
    其他: ~/.geotrace/thumbnails/
    """
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path.home() / ".geotrace"

    cache_dir = base / "GeoTrace" / "thumbnails"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def thumbnail_key(file_path: str) -> str:
    """对文件路径取 MD5 前 16 位作为缓存键.

    返回三级哈希分片路径: ab/cd/ef{rest}.jpg
    每级目录最多 256 个条目，避免单目录文件数爆炸。
    """
    import hashlib
    h = hashlib.md5(file_path.encode("utf-8")).hexdigest()[:16]
    return f"{h[0:2]}/{h[2:4]}/{h[4:]}.jpg"


class ThumbnailWorker(Worker):
    """缩略图生成 Worker.

    Signals:
        progress(int, int): 进度 (current, total).
        thumbnailReady(int, str): 缩略图生成完成 (photo_id, thumbnail_path).
        error(str): 错误消息.
        finished(): 任务完成.
    """

    progress = Signal(int, int)
    thumbnailReady = Signal(int, str)

    def __init__(self, db: DatabaseManager) -> None:
        super().__init__()
        self._db = db
        self._cache_dir = get_thumbnail_cache_dir()
        self._cancelled = False

    @Slot()
    def run(self) -> None:
        """在线程中执行缩略图生成 (由 QThread.started 信号触发)."""
        try:
            self._do_generate()
        except Exception as e:
            logger.exception("缩略图生成过程发生未处理异常")
            self.error.emit(str(e))
        finally:
            self.finished.emit()

    def cancel(self) -> None:
        """取消生成."""
        self._cancelled = True

    # ------------------------------------------------------------------
    # 核心生成逻辑
    # ------------------------------------------------------------------

    def _do_generate(self) -> None:
        photos = self._db.get_photos_missing_thumbnails()
        total = len(photos)

        if total == 0:
            logger.info("没有需要生成缩略图的照片")
            return

        logger.info("开始生成 %d 张缩略图...", total)

        for i, photo in enumerate(photos):
            if self._cancelled:
                break

            self.progress.emit(i + 1, total)

            photo_id = photo["id"]
            file_path = photo["file_path"]

            thumbnail_path = self._generate_one(file_path)
            if thumbnail_path:
                self._db.update_thumbnail_path(photo_id, str(thumbnail_path))
                self.thumbnailReady.emit(photo_id, str(thumbnail_path))

        logger.info("缩略图生成完成, 共 %d 张", total)

    def _generate_one(self, file_path: str) -> Path | None:
        """生成单张缩略图.

        Args:
            file_path: 原始图片路径.

        Returns:
            缩略图文件路径, 失败返回 None.
        """
        try:
            key = thumbnail_key(file_path)
            output_path = self._cache_dir / key

            # 确保哈希分片子目录存在
            output_path.parent.mkdir(parents=True, exist_ok=True)

            # 如果已存在且比原图新, 跳过
            if output_path.exists():
                try:
                    thumb_mtime = output_path.stat().st_mtime
                    src_mtime = os.path.getmtime(file_path)
                    if thumb_mtime >= src_mtime:
                        return output_path
                except OSError:
                    pass

            # Pillow 加载并处理
            img = Image.open(file_path)
            img = ImageOps.exif_transpose(img)  # 自动旋转

            # 缩放到长边 320px
            w, h = img.size
            if w > h:
                new_w = THUMBNAIL_LONG_SIDE
                new_h = int(h * THUMBNAIL_LONG_SIDE / w)
            else:
                new_h = THUMBNAIL_LONG_SIDE
                new_w = int(w * THUMBNAIL_LONG_SIDE / h)

            img_resized = img.resize((new_w, new_h), Image.LANCZOS)

            # 转 RGB 再保存 (RGBA/CMYK -> RGB)
            if img_resized.mode in ("RGBA", "P", "CMYK"):
                img_rgb = Image.new("RGB", img_resized.size, (255, 255, 255))
                if img_resized.mode == "P":
                    img_resized = img_resized.convert("RGBA")
                img_rgb.paste(img_resized, mask=img_resized.split()[-1] if img_resized.mode == "RGBA" else None)
            else:
                img_rgb = img_resized.convert("RGB")

            img_rgb.save(output_path, THUMBNAIL_FORMAT, quality=THUMBNAIL_QUALITY)
            return output_path

        except Exception as e:
            logger.warning("无法生成缩略图 %s: %s", file_path, e)
            return None
