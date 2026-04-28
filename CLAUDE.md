# GeoTrace (迹点) — 桌面端离线照片地理聚合与检索应用

## 项目概览

读取本地照片目录的 EXIF GPS 数据，通过离线逆地理编码映射至中国各省份，
使用原生 QPainter 手绘中国地图可视化交互，按省份检索浏览照片。

- **语言**: Python 3.11+
- **GUI**: PySide6 (无 QWebEngine 依赖)
- **可视化**: 原生 QPainter 手绘 + Shapely 几何 → QPainterPath 渲染
- **空间计算**: Shapely + R-Tree (本地 GeoJSON)
- **存储**: SQLite3 (WAL 模式)
- **运行环境**: `.venv` 虚拟环境, `python -m geotrace` 启动

## 目录结构

```
GeoTrace/
├── requirements.txt
├── 启动GeoTrace.bat               # 当前启动方式 (待替换为 exe)
├── data/                          # 运行时数据
│   ├── china_provinces.geojson    # 中国 34 省行政区划 (DataV.GeoAtlas, WGS84)
│   └── geotrace_index.db         # 自动生成
├── geotrace/
│   ├── __init__.py                # v0.1.0
│   ├── __main__.py                # python -m geotrace 入口
│   ├── app.py                     # QApplication, 全局 QSS, 窗口图标
│   ├── core/
│   │   ├── __init__.py
│   │   ├── models.py              # PhotoMetadata, ProvinceStat dataclass
│   │   ├── extractor.py           # EXIF GPS 提取 (Pillow + exifread)
│   │   └── spatial.py             # R-Tree + Shapely 离线逆地理编码
│   ├── database/
│   │   ├── __init__.py
│   │   ├── schema.py              # DDL (4 表 + 索引 + 3 触发器)
│   │   └── manager.py             # DatabaseManager: 线程安全 CRUD
│   ├── ui/
│   │   ├── __init__.py
│   │   ├── main_window.py         # QMainWindow, QStackedWidget 视图路由
│   │   ├── map_widget.py          # 【主地图】QPainter 手绘 + 色阶图例 + 悬停提示
│   │   ├── map_view.py            # 【已废弃】旧 QWebEngineView + ECharts 实现
│   │   ├── bridge.py              # MapBridge: QObject Signal 集线器
│   │   ├── theme.py               # 主题系统: Colors/Fonts/Metrics/GLOBAL_QSS/投影
│   │   ├── photo_grid.py          # QListView 卡片式缩略图 + 分页 + 空状态
│   │   ├── photo_viewer.py        # QDialog 大图查看 (EXIF 自动旋转 + 暖色背景)
│   │   ├── province_list.py       # 浮动省份列表面板
│   │   ├── settings_panel.py      # 浮动设置面板 (目录管理 + 扫描)
│   │   └── resources/
│   │       ├── __init__.py
│   │       ├── map.html           # 【已废弃】ECharts HTML 模板
│   │       ├── echarts.min.js     # 【已废弃】ECharts 5.5
│   │       └── qwebchannel.js     # 【已废弃】QWebChannel JS API
│   └── workers/
│       ├── __init__.py            # Worker 基类 (QObject)
│       ├── scan.py                # ScanWorker: 磁盘扫描 + EXIF + 入库
│       └── thumbnail.py           # ThumbnailWorker: 320px JPEG 缓存
```

## 核心数据流

```
用户选择目录 → ScanWorker(QThread) os.walk
  → EXIFExtractor.extract() → GPS 坐标
  → SpatialIndex.locate(lng, lat) → 省份
  → DatabaseManager.batch_upsert_photos() → SQLite
  → province_stats 触发器自动更新
  → MainWindow._refresh_stats() → map_widget.update_stats() → QPainter 热力图重绘

用户点击地图省份 → _MapCanvas.provinceClicked(name) → MapWidget._on_province_clicked
  → bridge.provinceClicked.emit(name) → MainWindow._on_province_clicked
  → PhotoGrid.load_province(name) → DatabaseManager.query_by_province(分页)
  → QStackedWidget 切换到 PhotoGrid

浮动面板:
  地图左上角 ☰ → ProvinceListPanel (按照片数排序的省份列表)
  地图右上角 ⚙ → SettingsPanel (目录添加/移除 + 重新扫描 + 进度条)
```

## 关键设计决策

### 地图渲染: 原生 QPainter 手绘 (map_widget.py)

- **_MapCanvas (QWidget)**: 直接使用 QPainter 绘制省份多边形，无 GPU 依赖
- **Shapely → QPainterPath**: `_geom_to_painter_path()` 将 Shapely Polygon/MultiPolygon 转为 QPainterPath
- **暖色热力图色阶**: 4 级渐变 (米白 → 浅橙 → 深橙 → 暗橙)
- **坐标变换**: `QTransform(lng→X, lat→Y, 支持缩放拖拽)`
- **缩放**: 鼠标滚轮缩放 (scale 5.0~800.0)，以鼠标位置为中心
- **拖拽**: 鼠标左键拖拽平移视图
- **标签动画**: 缩放 ≥ 110% 时省份简称淡入显示，悬停省份 + 邻居省份不透明放大
- **省份邻接**: 通过 Shapely `touches()` + 距离回退计算邻接关系，悬停时高亮省 + 邻居
- **GeoJSON 简化**: 加载时 `shapely.simplify(tolerance=0.02)` 减少多边形顶点
- **色阶图例**: 右下角半透明叠加层，4 段渐变色条 + 最小/最大值标签
- **悬停提示**: `_MapCanvas.hoveredChanged(str)` 信号 → MapWidget 浮动 QLabel
- **为什么不用 QWebEngineView**: QGraphicsView Item 事件分发在复杂几何场景下不可靠，QWebEngine 打包体积大 (~200MB+) 且启动慢，改用最底层手绘保证交互可靠性

### 主题系统 (theme.py)

- **Colors 类**: 暖土色系调色板 (Surface / Text / Border / Accent / Semantic / Map / Progress)
- **Fonts 类**: 字体栈 `"Microsoft YaHei UI" → "Segoe UI" → "SimSun"` + `ui()`/`title()`/`caption()` 工厂
- **Metrics 类**: 统一圆角/内边距/阴影/按钮尺寸
- **GLOBAL_QSS**: 一次性 `app.setStyleSheet()` 应用，覆盖所有 Qt 组件 (QPushButton 含 `cssClass` 属性选择器)
- **投影工厂**: `panel_shadow_effect()` (blur=12, offset 0,2) 和 `card_shadow_effect()` (blur=8, offset 0,1)
- 所有 UI 文件通过 `from geotrace.ui.theme import Colors, Fonts` 引用，禁止硬编码颜色

### UI 动画

- **面板动画**: 省份列表面板 / 设置面板 滑入滑出 (200ms/150ms, OutCubic/InCubic, geometry 动画)
- **视图切换**: QStackedWidget 交叉淡入淡出 (150ms, QGraphicsOpacityEffect)
- **面板投影**: QGraphicsDropShadowEffect (与 geometry 动画不冲突)

### Bridge 模式 (bridge.py)

- MapBridge 是纯 QObject Signal 集线器，不再依赖 QWebChannel
- `@Slot` 装饰器保留但仅在外部直接调用时有用（当前无 JS 调用路径）
- 所有跨组件通信通过 Signal/Slot：MapWidget → bridge → MainWindow → PhotoGrid

### 数据库

- WAL 模式支持多线程并发读写
- `province_stats` 表由 3 个触发器自动维护 (INSERT/UPDATE/DELETE)
- 每个 Worker 线程持有独立 `sqlite3.Connection` (通过 `threading.local()`)
- 增量扫描：通过 `(file_path, file_mtime)` 幂等检查

### 线程安全

- QThread + moveToThread 模式：Worker 创建后移入工作线程
- 所有 GUI 更新通过 Signal/Slot 回到主线程
- DatabaseManager 使用 `threading.local()` 管理连接

## 启动方式

```bash
cd a:/JUST_DO_IT/GeoTrace
.venv/Scripts/activate
python -m geotrace
```

或双击 `启动GeoTrace.bat` (使用 pythonw.exe 无控制台窗口启动)

## 依赖

```
PySide6>=6.5.0,<7.0
Pillow>=10.0.0
exifread>=3.0.0
shapely>=2.0.0
rtree>=1.0.0
```

## 已知注意事项

1. **libpng iCCP 警告** — Qt 内置 PNG 资源的色彩配置文件问题，无害，可忽略
2. **GeoJSON 编码** — DataV 中文省份名在终端可能显示乱码，实际数据正确
3. **已废弃文件** — `map_view.py`, `echarts.min.js`, `map.html`, `qwebchannel.js` 不再被主代码引用，保留供参考
4. **缩略图缓存** — 缩略图生成到系统临时目录 (`tempfile.gettempdir()/geotrace_thumbnails/`)
5. **GitHub CLI** — 通过 winget 安装 `C:\Program Files\GitHub CLI\gh.exe`，需 VPN/代理才能认证和推送

## 封装方案A: PyInstaller exe 打包 (待实施)

将 `启动GeoTrace.bat` 替换为原生 exe，双击启动无控制台窗口。

### 方案选型

**PyInstaller `--onedir`（单目录）模式**：
- 相比旧方案（QWebEngine），原生 QPainter 渲染大幅简化打包 —— 无需 QtWebEngineProcess.exe
- `--onedir` 启动即时，比 `--onefile` 解压方案快 5-10 秒
- 打包体积预计 80-120MB (仅 Qt GUI 部分)

### 需修改的文件

1. **`geotrace/app.py`** — 添加 `_get_basedir()` 兼容 PyInstaller 的 `sys.frozen` 环境
2. **`geotrace/ui/map_widget.py`** — GeoJSON 路径从 main_window 传入，无需修改
3. **`geotrace/ui/main_window.py`** — `_DEFAULT_DATA_DIR` 使用兼容路径

### PyInstaller 打包命令

```bash
pyinstaller \
  --windowed \
  --name "GeoTrace" \
  --add-data "data/china_provinces.geojson;data" \
  -m geotrace
```

注意：已废弃的 web resources (echarts.min.js 等) 不需要打包。

### 输出

`dist/GeoTrace/GeoTrace.exe` — 双击启动，无控制台。
