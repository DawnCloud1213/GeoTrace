"""数据库 DDL 与常量定义."""

# 数据库文件名
DB_FILENAME = "geotrace_index.db"

# 表名常量
TABLE_PHOTOS = "photos"
TABLE_PROVINCE_STATS = "province_stats"
TABLE_DIRECTORIES = "directories"
TABLE_SETTINGS = "settings"

# pragma 初始化
PRAGMA_INIT = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA synchronous=NORMAL;
PRAGMA cache_size=-64000;
"""

# 照片主表
CREATE_PHOTOS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {TABLE_PHOTOS} (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path       TEXT NOT NULL UNIQUE,
    file_name       TEXT NOT NULL,
    file_size       INTEGER NOT NULL,
    file_mtime      REAL NOT NULL,
    md5_hash        TEXT,
    width           INTEGER,
    height          INTEGER,
    latitude        REAL,
    longitude       REAL,
    altitude        REAL,
    province_code   TEXT,
    province_name   TEXT,
    date_taken      TEXT,
    camera_model    TEXT,
    orientation     INTEGER DEFAULT 1,
    thumbnail_path  TEXT,
    indexed_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# 索引
CREATE_INDEXES = [
    f"CREATE INDEX IF NOT EXISTS idx_photos_province ON {TABLE_PHOTOS}(province_name);",
    f"CREATE INDEX IF NOT EXISTS idx_photos_date ON {TABLE_PHOTOS}(date_taken);",
    f"CREATE INDEX IF NOT EXISTS idx_photos_lat_lng ON {TABLE_PHOTOS}(latitude, longitude);",
    f"CREATE INDEX IF NOT EXISTS idx_photos_file_path ON {TABLE_PHOTOS}(file_path);",
    f"CREATE INDEX IF NOT EXISTS idx_photos_mtime ON {TABLE_PHOTOS}(file_path, file_mtime);",
]

# 扫描目录表
CREATE_DIRECTORIES_TABLE = f"""
CREATE TABLE IF NOT EXISTS {TABLE_DIRECTORIES} (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    path          TEXT NOT NULL UNIQUE,
    photo_count   INTEGER DEFAULT 0,
    last_scan_at  TEXT,
    enabled       INTEGER DEFAULT 1
);
"""

# 省份统计缓存表 (触发器自动维护)
CREATE_PROVINCE_STATS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {TABLE_PROVINCE_STATS} (
    province_code TEXT PRIMARY KEY,
    province_name TEXT NOT NULL,
    photo_count   INTEGER DEFAULT 0,
    last_updated  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# 设置表
CREATE_SETTINGS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {TABLE_SETTINGS} (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# --- 触发器: 自动维护 province_stats ---

TRIGGER_PHOTOS_INSERT = f"""
CREATE TRIGGER IF NOT EXISTS trg_photos_insert
    AFTER INSERT ON {TABLE_PHOTOS}
BEGIN
    INSERT INTO {TABLE_PROVINCE_STATS} (province_code, province_name, photo_count)
    VALUES (COALESCE(NEW.province_code, ''), COALESCE(NEW.province_name, 'Unclassified'), 1)
    ON CONFLICT(province_code) DO UPDATE SET
        photo_count = photo_count + 1,
        last_updated = datetime('now');
END;
"""

TRIGGER_PHOTOS_DELETE = f"""
CREATE TRIGGER IF NOT EXISTS trg_photos_delete
    AFTER DELETE ON {TABLE_PHOTOS}
BEGIN
    UPDATE {TABLE_PROVINCE_STATS} SET
        photo_count = MAX(0, photo_count - 1),
        last_updated = datetime('now')
    WHERE province_code = COALESCE(OLD.province_code, '');
END;
"""

TRIGGER_PHOTOS_UPDATE = f"""
CREATE TRIGGER IF NOT EXISTS trg_photos_update
    AFTER UPDATE ON {TABLE_PHOTOS}
    WHEN OLD.province_code IS NOT NEW.province_code
BEGIN
    UPDATE {TABLE_PROVINCE_STATS} SET
        photo_count = MAX(0, photo_count - 1),
        last_updated = datetime('now')
    WHERE province_code = COALESCE(OLD.province_code, '');
    INSERT INTO {TABLE_PROVINCE_STATS} (province_code, province_name, photo_count)
    VALUES (COALESCE(NEW.province_code, ''), COALESCE(NEW.province_name, 'Unclassified'), 1)
    ON CONFLICT(province_code) DO UPDATE SET
        photo_count = photo_count + 1,
        last_updated = datetime('now');
END;
"""

ALL_DDL = [
    CREATE_PHOTOS_TABLE,
    *CREATE_INDEXES,
    CREATE_DIRECTORIES_TABLE,
    CREATE_PROVINCE_STATS_TABLE,
    CREATE_SETTINGS_TABLE,
    TRIGGER_PHOTOS_INSERT,
    TRIGGER_PHOTOS_DELETE,
    TRIGGER_PHOTOS_UPDATE,
]
