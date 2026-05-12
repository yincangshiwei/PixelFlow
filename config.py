"""
PixelFlow 应用全局配置
所有应用元信息统一在此定义，其他模块和打包脚本统一引用此文件。
"""
import sys
from pathlib import Path

# ─── 应用基本信息 ───
APP_NAME = "PixelFlow"
APP_TITLE = "PixelFlow - 图像处理工作台"
APP_VERSION = "1.0.6"
APP_DESCRIPTION = "图像处理工作台"
APP_PUBLISHER = "PixelFlow"

# ─── 版权信息 ───
APP_COPYRIGHT = "SEQL"
APP_COPYRIGHT_URL = "https://github.com/yincangshiwei"

# ─── 路径 ───
# 项目/安装根目录
if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys._MEIPASS)           # 打包后：只读资源目录
    DATA_DIR = Path(sys.executable).parent  # 打包后：exe 所在目录（可读写）
else:
    BASE_DIR = Path(__file__).resolve().parent   # 开发时：项目根目录
    DATA_DIR = BASE_DIR                          # 开发时：同项目根目录

RESOURCES_DIR = BASE_DIR / "resources"
ICON_PATH = RESOURCES_DIR / "app.ico"
PRESETS_DIR = DATA_DIR / "presets"
LOGS_DIR = DATA_DIR / "logs"

# ─── UI 样式常量 ───
# 全局下拉框样式（QComboBox），确保背景色不透明，字体清晰可见
COMBOBOX_STYLE = """
    QComboBox {
        padding: 5px 10px;
        border: 1px solid rgba(90, 100, 160, 0.25);
        border-radius: 7px;
        background-color: rgba(22, 22, 40, 160);
        color: #e0e0e0;
    }
    QComboBox:focus {
        border-color: rgba(100, 150, 255, 0.6);
    }
    QComboBox::drop-down {
        border: none;
        background-color: rgba(55, 55, 85, 140);
        width: 24px;
        border-radius: 0 7px 7px 0;
    }
    QComboBox::down-arrow {
        border-left:5px solid transparent;
        border-right:5px solid transparent;
        border-top:5px solid #aaa;
    }
    QComboBox QAbstractItemView {
        background-color: rgba(30, 30, 55, 230);
        color: #e0e0e0;
        border: 1px solid rgba(100, 110, 170, 0.3);
        selection-background-color: rgba(91, 138, 245, 180);
        selection-color: #fff;
        border-radius: 6px;
        outline: none;
    }
    QComboBox QAbstractItemView::item {
        padding: 5px 10px;
        background-color: transparent;
    }
    QComboBox QAbstractItemView::item:hover {
        background-color: rgba(60, 65, 110, 100);
    }
    QComboBox QAbstractItemView::item:selected {
        background-color: rgba(91, 138, 245, 180);
        color: #fff;
    }
"""

# emoji图标按钮样式，用于元素列表操作按钮
ICON_BTN_STYLE = """
    QPushButton {
        border: 1px solid rgba(90, 100, 160, 0.25);
        border-radius: 7px;
        background-color: rgba(30, 30, 55, 160);
        padding: 2px;
        font-size: 16px;
        min-width: 34px;
        max-width: 38px;
        min-height: 34px;
        max-height: 38px;
    }
    QPushButton:hover {
        border-color: rgba(100, 150, 255, 0.6);
        background-color: rgba(40, 40, 70, 200);
    }
    QPushButton:pressed {
        background-color: rgba(20, 20, 45, 220);
    }
"""
