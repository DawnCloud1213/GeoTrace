"""GeoTrace 主题系统 — 集中管理颜色、字体、全局样式表.

设计原则:
  - 暖土色系 (warm earthy), 灵感来自手绘地图的 #FEF9F0 底色
  - 所有颜色通过本模块公开, 禁止在其他 UI 文件中硬编码颜色字面量
  - 按钮色与地图热力色阶协调 (避免 Material Design 绿/红)
"""

from PySide6.QtGui import QColor, QFont


# ==========================================================================
# Color Palette
# ==========================================================================

class Colors:
    """暖土色系调色板."""

    # Surface
    WINDOW_BG = "#F5F0E8"
    MAP_BG = "#FEF9F0"
    CARD_BG = "#FFFFFF"
    PANEL_BG = "rgba(255,255,255,0.96)"
    INPUT_BG = "#FAFAF5"

    # Text
    TEXT_PRIMARY = "#503214"
    TEXT_SECONDARY = "#8B7355"
    TEXT_DISABLED = "#B8A88E"
    TEXT_ON_ACCENT = "#FFFFFF"

    # Borders
    BORDER_LIGHT = "#E8E0D0"
    BORDER_MEDIUM = "#C8B898"
    BORDER_STRONG = "#A89070"
    BORDER_FOCUS = "#E67E22"

    # Accent (暖橙系, 与地图 HOVER_BORDER #FF7043 协调)
    ACCENT_PRIMARY = "#E67E22"
    ACCENT_HOVER = "#D35400"
    ACCENT_PRESSED = "#BF4700"
    ACCENT_SELECTED = "#FFCC80"
    ACCENT_HOVER_LIGHT = "#FFF3E0"

    # Semantic (暖土色系语义色)
    DANGER = "#B5432E"
    DANGER_HOVER = "#962D1C"
    SUCCESS = "#5D7A3A"
    SUCCESS_HOVER = "#4A632E"
    WARNING = "#E67E22"
    WARNING_HOVER = "#D35400"

    # Map (保持现有 QPainter 渲染管道不变)
    MAP_HEAT_1 = "#FFF3E0"
    MAP_HEAT_2 = "#FFCC80"
    MAP_HEAT_3 = "#FF9800"
    MAP_HEAT_4 = "#E65100"
    MAP_BORDER = "#C8B898"
    MAP_HOVER = "#FF7043"

    # Progress Bar
    PROGRESS_TRACK = "#EDE5D8"
    PROGRESS_CHUNK = "#E67E22"

    # Scrollbar
    SCROLL_HANDLE = "#C8B898"
    SCROLL_HANDLE_HOVER = "#A89070"
    SCROLL_TRACK = "#F5F0E8"


# ==========================================================================
# Typography
# ==========================================================================

class Fonts:
    """字体栈."""

    FAMILY = '"Microsoft YaHei UI", "Segoe UI", "SimSun"'
    FAMILY_MONO = '"Cascadia Code", "Consolas", "Courier New"'

    @staticmethod
    def ui(size: int = 9, bold: bool = False) -> QFont:
        f = QFont(Fonts.FAMILY, size)
        f.setBold(bold)
        return f

    @staticmethod
    def title(size: int = 14) -> QFont:
        f = QFont(Fonts.FAMILY, size)
        f.setBold(True)
        return f

    @staticmethod
    def caption(size: int = 8) -> QFont:
        return QFont(Fonts.FAMILY, size)


# ==========================================================================
# Dimensions & Metrics
# ==========================================================================

class Metrics:
    """统一尺寸."""

    BORDER_RADIUS_SM = 4
    BORDER_RADIUS_MD = 8
    BORDER_RADIUS_LG = 12
    PADDING_XS = 4
    PADDING_SM = 8
    PADDING_MD = 12
    PADDING_LG = 16
    SHADOW_BLUR = 12
    SHADOW_OFFSET = (0, 2)
    PANEL_WIDTH_MIN = 180
    PANEL_WIDTH_MAX = 220
    MAP_BTN_SIZE = 36


# ==========================================================================
# Global QSS Stylesheet
# ==========================================================================

GLOBAL_QSS = f"""
/* GeoTrace 全局样式表 */

/* ── 全局默认 ──────────────────────────────────────────────────── */
QMainWindow {{
    background-color: {Colors.WINDOW_BG};
}}
QWidget {{
    font-family: {Fonts.FAMILY};
    font-size: 13px;
    color: {Colors.TEXT_PRIMARY};
}}

/* ── QMenuBar ───────────────────────────────────────────────────── */
QMenuBar {{
    background-color: {Colors.WINDOW_BG};
    border-bottom: 1px solid {Colors.BORDER_LIGHT};
    padding: 2px 4px;
    font-size: 13px;
}}
QMenuBar::item {{
    padding: 4px 12px;
    border-radius: {Metrics.BORDER_RADIUS_SM}px;
    background: transparent;
}}
QMenuBar::item:selected {{
    background-color: {Colors.ACCENT_HOVER_LIGHT};
}}

/* ── QMenu ──────────────────────────────────────────────────────── */
QMenu {{
    background-color: {Colors.CARD_BG};
    border: 1px solid {Colors.BORDER_LIGHT};
    border-radius: {Metrics.BORDER_RADIUS_MD}px;
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 28px 6px 12px;
    border-radius: {Metrics.BORDER_RADIUS_SM}px;
}}
QMenu::item:selected {{
    background-color: {Colors.ACCENT_HOVER_LIGHT};
    color: {Colors.TEXT_PRIMARY};
}}
QMenu::separator {{
    height: 1px;
    background: {Colors.BORDER_LIGHT};
    margin: 4px 8px;
}}

/* ── QStatusBar ─────────────────────────────────────────────────── */
QStatusBar {{
    background-color: {Colors.WINDOW_BG};
    border-top: 1px solid {Colors.BORDER_LIGHT};
    padding: 2px 8px;
    font-size: 12px;
    color: {Colors.TEXT_SECONDARY};
}}

/* ── QPushButton 全局基准 ───────────────────────────────────────── */
QPushButton {{
    border: 1px solid {Colors.BORDER_MEDIUM};
    border-radius: {Metrics.BORDER_RADIUS_SM}px;
    padding: 5px 14px;
    background-color: {Colors.CARD_BG};
    color: {Colors.TEXT_PRIMARY};
}}
QPushButton:hover {{
    background-color: {Colors.ACCENT_HOVER_LIGHT};
    border-color: {Colors.ACCENT_PRIMARY};
}}
QPushButton:pressed {{
    background-color: {Colors.ACCENT_SELECTED};
}}
QPushButton:disabled {{
    color: {Colors.TEXT_DISABLED};
    background-color: {Colors.INPUT_BG};
    border-color: {Colors.BORDER_LIGHT};
}}

/* QPushButton 语义变体 (通过 cssClass 属性选择) */
QPushButton[cssClass="primary"] {{
    background-color: {Colors.ACCENT_PRIMARY};
    color: {Colors.TEXT_ON_ACCENT};
    border: none;
    font-weight: bold;
}}
QPushButton[cssClass="primary"]:hover {{
    background-color: {Colors.ACCENT_HOVER};
}}
QPushButton[cssClass="danger"] {{
    background-color: {Colors.DANGER};
    color: {Colors.TEXT_ON_ACCENT};
    border: none;
}}
QPushButton[cssClass="danger"]:hover {{
    background-color: {Colors.DANGER_HOVER};
}}
QPushButton[cssClass="success"] {{
    background-color: {Colors.SUCCESS};
    color: {Colors.TEXT_ON_ACCENT};
    border: none;
}}
QPushButton[cssClass="success"]:hover {{
    background-color: {Colors.SUCCESS_HOVER};
}}

/* QPushButton ghost (透明背景) */
QPushButton[cssClass="ghost"] {{
    background: transparent;
    border: none;
    color: {Colors.TEXT_SECONDARY};
    font-size: 14px;
}}
QPushButton[cssClass="ghost"]:hover {{
    color: {Colors.TEXT_PRIMARY};
}}

/* QPushButton 地图叠加按钮 */
QPushButton[cssClass="mapOverlay"] {{
    background: rgba(255,255,255,0.85);
    border: 1px solid {Colors.BORDER_MEDIUM};
    border-radius: {Metrics.BORDER_RADIUS_SM}px;
    font-size: 16px;
    color: {Colors.TEXT_SECONDARY};
    padding: 0px;
}}
QPushButton[cssClass="mapOverlay"]:hover {{
    background: rgba(255,255,255,0.95);
    border-color: {Colors.ACCENT_PRIMARY};
    color: {Colors.ACCENT_PRIMARY};
}}

/* ── QListView / QListWidget ────────────────────────────────────── */
QListView, QListWidget {{
    border: 1px solid {Colors.BORDER_LIGHT};
    border-radius: {Metrics.BORDER_RADIUS_SM}px;
    background-color: {Colors.INPUT_BG};
    outline: none;
}}
QListView::item, QListWidget::item {{
    padding: 5px 10px;
    border-bottom: 1px solid {Colors.BORDER_LIGHT};
}}
QListView::item:hover, QListWidget::item:hover {{
    background: {Colors.ACCENT_HOVER_LIGHT};
}}
QListView::item:selected, QListWidget::item:selected {{
    background: {Colors.ACCENT_SELECTED};
    color: {Colors.TEXT_PRIMARY};
}}

/* ── QScrollBar:vertical ───────────────────────────────────────── */
QScrollBar:vertical {{
    background: {Colors.SCROLL_TRACK};
    width: 8px;
    margin: 0;
    border-radius: 4px;
}}
QScrollBar::handle:vertical {{
    background: {Colors.SCROLL_HANDLE};
    min-height: 30px;
    border-radius: 4px;
}}
QScrollBar::handle:vertical:hover {{
    background: {Colors.SCROLL_HANDLE_HOVER};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: none;
}}

/* ── QScrollBar:horizontal ─────────────────────────────────────── */
QScrollBar:horizontal {{
    background: {Colors.SCROLL_TRACK};
    height: 8px;
    margin: 0;
    border-radius: 4px;
}}
QScrollBar::handle:horizontal {{
    background: {Colors.SCROLL_HANDLE};
    min-width: 30px;
    border-radius: 4px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {Colors.SCROLL_HANDLE_HOVER};
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0px;
}}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: none;
}}

/* ── QProgressBar ────────────────────────────────────────────────── */
QProgressBar {{
    border: 1px solid {Colors.BORDER_LIGHT};
    border-radius: 3px;
    background-color: {Colors.PROGRESS_TRACK};
    height: 14px;
    text-align: center;
    font-size: 11px;
}}
QProgressBar::chunk {{
    background-color: {Colors.PROGRESS_CHUNK};
    border-radius: 2px;
}}

/* ── QToolTip ────────────────────────────────────────────────────── */
QToolTip {{
    background-color: {Colors.CARD_BG};
    color: {Colors.TEXT_PRIMARY};
    border: 1px solid {Colors.BORDER_MEDIUM};
    border-radius: {Metrics.BORDER_RADIUS_SM}px;
    padding: 4px 8px;
    font-size: 12px;
}}

/* ── QScrollArea ─────────────────────────────────────────────────── */
QScrollArea {{
    border: none;
    background-color: transparent;
}}

/* ── FloatingPanel ──────────────────────────────────────────────── */
QFrame#floatingPanel {{
    background: {Colors.PANEL_BG};
    border: 1px solid {Colors.BORDER_LIGHT};
    border-radius: {Metrics.BORDER_RADIUS_MD}px;
}}

/* ── QTabWidget / QTabBar ─────────────────────────────────────── */
QTabWidget::pane {{
    border: none;
    background-color: {Colors.WINDOW_BG};
}}
QTabBar::tab {{
    background: {Colors.CARD_BG};
    border: 1px solid {Colors.BORDER_LIGHT};
    border-bottom: none;
    border-top-left-radius: {Metrics.BORDER_RADIUS_SM}px;
    border-top-right-radius: {Metrics.BORDER_RADIUS_SM}px;
    padding: 6px 16px;
    color: {Colors.TEXT_SECONDARY};
}}
QTabBar::tab:selected {{
    background: {Colors.WINDOW_BG};
    color: {Colors.TEXT_PRIMARY};
    font-weight: bold;
}}
QTabBar::tab:!selected {{
    margin-top: 2px;
}}
"""


# ==========================================================================
# Shadow effect factories
# ==========================================================================

def panel_shadow_effect():
    """浮动面板投影."""
    from PySide6.QtWidgets import QGraphicsDropShadowEffect
    shadow = QGraphicsDropShadowEffect()
    shadow.setBlurRadius(Metrics.SHADOW_BLUR)
    shadow.setXOffset(Metrics.SHADOW_OFFSET[0])
    shadow.setYOffset(Metrics.SHADOW_OFFSET[1])
    shadow.setColor(QColor(80, 50, 20, 40))
    return shadow


def card_shadow_effect():
    """卡片投影 (比面板投影轻)."""
    from PySide6.QtWidgets import QGraphicsDropShadowEffect
    shadow = QGraphicsDropShadowEffect()
    shadow.setBlurRadius(8)
    shadow.setXOffset(0)
    shadow.setYOffset(1)
    shadow.setColor(QColor(80, 50, 20, 30))
    return shadow


def frosted_rgba(alpha: float) -> str:
    """根据 0.0~1.0 alpha 生成白色 rgba 字符串."""
    a = max(0.0, min(1.0, alpha))
    return f"rgba(255,255,255,{a:.2f})"
