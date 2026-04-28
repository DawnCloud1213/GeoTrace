"""SQLite 数据库管理器 - 连接管理、CRUD、聚合查询."""

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from geotrace.core.models import PhotoMetadata, ProvinceStat
from geotrace.database.schema import (
    ALL_DDL,
    PRAGMA_INIT,
    TABLE_DIRECTORIES,
    TABLE_PHOTOS,
    TABLE_PROVINCE_STATS,
    TABLE_SETTINGS,
)

logger = logging.getLogger(__name__)


class DatabaseManager:
    """线程安全的 SQLite 数据库管理器.

    每个线程持有独立的 sqlite3.Connection, 利用 WAL 模式支持并发读写.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._local = threading.local()
        self._init_database()

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    def _init_database(self) -> None:
        """创建数据库文件并初始化所有表和索引."""
        conn = self._connect()
        try:
            conn.executescript(PRAGMA_INIT)
            for ddl in ALL_DDL:
                conn.execute(ddl)
            conn.commit()
            logger.info("数据库已初始化: %s", self._db_path)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def get_connection(self) -> sqlite3.Connection:
        """返回当前线程的数据库连接 (不存在则自动创建)."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = self._connect()
        return self._local.conn

    def close(self) -> None:
        """关闭当前线程的数据库连接."""
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._local.conn.close()
            self._local.conn = None

    # ------------------------------------------------------------------
    # 照片 CRUD
    # ------------------------------------------------------------------

    def photo_needs_update(self, file_path: str, file_mtime: float) -> bool:
        """检查照片是否需要重新扫描 (新增或已修改).

        Args:
            file_path: 照片绝对路径.
            file_mtime: 当前文件的 os.path.getmtime() 值.

        Returns:
            True 表示需要扫描.
        """
        conn = self.get_connection()
        row = conn.execute(
            f"SELECT file_mtime FROM {TABLE_PHOTOS} WHERE file_path = ?",
            (file_path,),
        ).fetchone()
        if row is None:
            return True
        return abs(row["file_mtime"] - file_mtime) > 1e-6

    def upsert_photo(self, meta: PhotoMetadata) -> int:
        """插入或更新照片记录.

        Args:
            meta: 照片元数据.

        Returns:
            照片的 row id.
        """
        conn = self.get_connection()
        row = conn.execute(
            f"SELECT id FROM {TABLE_PHOTOS} WHERE file_path = ?",
            (meta.file_path,),
        ).fetchone()

        if row is not None:
            photo_id = row["id"]
            conn.execute(
                f"""UPDATE {TABLE_PHOTOS} SET
                    file_name=?, file_size=?, file_mtime=?, md5_hash=?,
                    width=?, height=?, latitude=?, longitude=?, altitude=?,
                    province_code=?, province_name=?, date_taken=?,
                    camera_model=?, orientation=?, updated_at=datetime('now')
                    WHERE id=?""",
                (
                    meta.file_name, meta.file_size, meta.file_mtime, meta.md5_hash,
                    meta.width, meta.height, meta.latitude, meta.longitude, meta.altitude,
                    meta.province_code, meta.province_name, meta.date_taken,
                    meta.camera_model, meta.orientation, photo_id,
                ),
            )
        else:
            cursor = conn.execute(
                f"""INSERT INTO {TABLE_PHOTOS} (
                    file_path, file_name, file_size, file_mtime, md5_hash,
                    width, height, latitude, longitude, altitude,
                    province_code, province_name, date_taken,
                    camera_model, orientation
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    meta.file_path, meta.file_name, meta.file_size, meta.file_mtime, meta.md5_hash,
                    meta.width, meta.height, meta.latitude, meta.longitude, meta.altitude,
                    meta.province_code, meta.province_name, meta.date_taken,
                    meta.camera_model, meta.orientation,
                ),
            )
            photo_id = cursor.lastrowid

        conn.commit()
        return photo_id

    def batch_upsert_photos(self, metas: list[PhotoMetadata]) -> int:
        """批量插入或更新照片 (比逐条 upsert 快 10-100 倍).

        Returns:
            新增照片数量.
        """
        conn = self.get_connection()
        new_count = 0

        for meta in metas:
            row = conn.execute(
                f"SELECT id FROM {TABLE_PHOTOS} WHERE file_path = ?",
                (meta.file_path,),
            ).fetchone()

            if row is not None:
                conn.execute(
                    f"""UPDATE {TABLE_PHOTOS} SET
                        file_name=?, file_size=?, file_mtime=?, md5_hash=?,
                        width=?, height=?, latitude=?, longitude=?, altitude=?,
                        province_code=?, province_name=?, date_taken=?,
                        camera_model=?, orientation=?, updated_at=datetime('now')
                        WHERE id=?""",
                    (
                        meta.file_name, meta.file_size, meta.file_mtime, meta.md5_hash,
                        meta.width, meta.height, meta.latitude, meta.longitude, meta.altitude,
                        meta.province_code, meta.province_name, meta.date_taken,
                        meta.camera_model, meta.orientation, row["id"],
                    ),
                )
            else:
                conn.execute(
                    f"""INSERT INTO {TABLE_PHOTOS} (
                        file_path, file_name, file_size, file_mtime, md5_hash,
                        width, height, latitude, longitude, altitude,
                        province_code, province_name, date_taken,
                        camera_model, orientation
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        meta.file_path, meta.file_name, meta.file_size, meta.file_mtime, meta.md5_hash,
                        meta.width, meta.height, meta.latitude, meta.longitude, meta.altitude,
                        meta.province_code, meta.province_name, meta.date_taken,
                        meta.camera_model, meta.orientation,
                    ),
                )
                new_count += 1

        conn.commit()
        return new_count

    def get_photo(self, photo_id: int) -> Optional[dict]:
        """获取单张照片信息."""
        conn = self.get_connection()
        row = conn.execute(
            f"SELECT * FROM {TABLE_PHOTOS} WHERE id = ?",
            (photo_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_photo_by_path(self, file_path: str) -> Optional[dict]:
        """通过文件路径查询照片."""
        conn = self.get_connection()
        row = conn.execute(
            f"SELECT * FROM {TABLE_PHOTOS} WHERE file_path = ?",
            (file_path,),
        ).fetchone()
        return dict(row) if row else None

    def delete_photo(self, photo_id: int) -> None:
        """删除照片记录."""
        conn = self.get_connection()
        conn.execute(f"DELETE FROM {TABLE_PHOTOS} WHERE id = ?", (photo_id,))
        conn.commit()

    def get_total_photo_count(self) -> int:
        """获取照片总数."""
        conn = self.get_connection()
        row = conn.execute(f"SELECT COUNT(*) as cnt FROM {TABLE_PHOTOS}").fetchone()
        return row["cnt"]

    # ------------------------------------------------------------------
    # 省份统计 (O(1) 查询)
    # ------------------------------------------------------------------

    def get_province_stats(self) -> list[ProvinceStat]:
        """获取各省份照片数量分布 (用于 ECharts 热力图).

        Returns:
            ProvinceStat 列表, 按 photo_count DESC 排序.
        """
        conn = self.get_connection()
        rows = conn.execute(
            f"""SELECT province_code, province_name, photo_count
                FROM {TABLE_PROVINCE_STATS}
                WHERE photo_count > 0
                ORDER BY photo_count DESC""",
        ).fetchall()
        return [ProvinceStat(
            province_code=r["province_code"],
            province_name=r["province_name"],
            photo_count=r["photo_count"],
        ) for r in rows]

    def get_province_stats_as_list(self) -> list[dict]:
        """获取省份统计 (dict 格式, 便于 JSON 序列化)."""
        conn = self.get_connection()
        rows = conn.execute(
            f"""SELECT province_name, photo_count
                FROM {TABLE_PROVINCE_STATS}
                WHERE photo_count > 0
                ORDER BY photo_count DESC""",
        ).fetchall()
        return [{"name": r["province_name"], "value": r["photo_count"]} for r in rows]

    # ------------------------------------------------------------------
    # 按省份查询照片 (分页)
    # ------------------------------------------------------------------

    def query_by_province(
        self,
        province_name: str,
        page: int = 1,
        page_size: int = 200,
    ) -> tuple[list[dict], int]:
        """分页查询某省份下的照片.

        Args:
            province_name: 省份名称.
            page: 页码 (从 1 开始).
            page_size: 每页数量.

        Returns:
            (照片列表, 总数量).
        """
        conn = self.get_connection()
        total_row = conn.execute(
            f"SELECT COUNT(*) as cnt FROM {TABLE_PHOTOS} WHERE province_name = ?",
            (province_name,),
        ).fetchone()
        total = total_row["cnt"]

        offset = (page - 1) * page_size
        rows = conn.execute(
            f"""SELECT id, file_path, file_name, thumbnail_path, date_taken,
                       width, height, latitude, longitude
                FROM {TABLE_PHOTOS}
                WHERE province_name = ?
                ORDER BY date_taken DESC
                LIMIT ? OFFSET ?""",
            (province_name, page_size, offset),
        ).fetchall()

        return [dict(r) for r in rows], total

    def get_unclassified_photos(self, page: int = 1, page_size: int = 200) -> tuple[list[dict], int]:
        """获取未分类 (无 GPS/无省份) 的照片."""
        conn = self.get_connection()
        total_row = conn.execute(
            f"SELECT COUNT(*) as cnt FROM {TABLE_PHOTOS} WHERE province_name IS NULL OR province_name = 'Unclassified'",
        ).fetchone()
        total = total_row["cnt"]

        offset = (page - 1) * page_size
        rows = conn.execute(
            f"""SELECT id, file_path, file_name, thumbnail_path, date_taken,
                       width, height, latitude, longitude
                FROM {TABLE_PHOTOS}
                WHERE province_name IS NULL OR province_name = 'Unclassified'
                ORDER BY date_taken DESC
                LIMIT ? OFFSET ?""",
            (page_size, offset),
        ).fetchall()

        return [dict(r) for r in rows], total

    # ------------------------------------------------------------------
    # 缩略图管理
    # ------------------------------------------------------------------

    def get_photos_missing_thumbnails(self, limit: int = 500) -> list[dict]:
        """获取缺少缩略图的照片."""
        conn = self.get_connection()
        rows = conn.execute(
            f"""SELECT id, file_path, file_name
                FROM {TABLE_PHOTOS}
                WHERE thumbnail_path IS NULL
                ORDER BY id
                LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def update_thumbnail_path(self, photo_id: int, thumbnail_path: str) -> None:
        """更新照片的缩略图路径."""
        conn = self.get_connection()
        conn.execute(
            f"UPDATE {TABLE_PHOTOS} SET thumbnail_path = ?, updated_at = datetime('now') WHERE id = ?",
            (thumbnail_path, photo_id),
        )
        conn.commit()

    # ------------------------------------------------------------------
    # 扫描目录管理
    # ------------------------------------------------------------------

    def add_directory(self, path: str) -> None:
        """添加扫描目录 (如不存在则创建)."""
        conn = self.get_connection()
        conn.execute(
            f"INSERT OR IGNORE INTO {TABLE_DIRECTORIES} (path) VALUES (?)",
            (path,),
        )
        conn.commit()

    def update_directory_scan(self, path: str, photo_count: int) -> None:
        """更新目录的扫描时间和照片数量."""
        conn = self.get_connection()
        conn.execute(
            f"""UPDATE {TABLE_DIRECTORIES} SET
                photo_count = ?, last_scan_at = datetime('now')
                WHERE path = ?""",
            (photo_count, path),
        )
        conn.commit()

    def get_directories(self) -> list[dict]:
        """获取所有已注册的扫描目录."""
        conn = self.get_connection()
        rows = conn.execute(
            f"SELECT * FROM {TABLE_DIRECTORIES} WHERE enabled = 1 ORDER BY path",
        ).fetchall()
        return [dict(r) for r in rows]

    def remove_directory(self, path: str) -> None:
        """移除扫描目录及其关联照片."""
        conn = self.get_connection()
        conn.execute(f"DELETE FROM {TABLE_PHOTOS} WHERE file_path LIKE ?", (f"{path}%",))
        conn.execute(f"DELETE FROM {TABLE_DIRECTORIES} WHERE path = ?", (path,))
        conn.commit()

    # ------------------------------------------------------------------
    # 设置管理
    # ------------------------------------------------------------------

    def get_setting(self, key: str, default: str = "") -> str:
        """获取应用设置."""
        conn = self.get_connection()
        row = conn.execute(
            f"SELECT value FROM {TABLE_SETTINGS} WHERE key = ?", (key,),
        ).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        """写入应用设置."""
        conn = self.get_connection()
        conn.execute(
            f"INSERT OR REPLACE INTO {TABLE_SETTINGS} (key, value) VALUES (?, ?)",
            (key, value),
        )
        conn.commit()
