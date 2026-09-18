"""
PixelFlow 应用全局配置
所有应用元信息统一在此定义，其他模块和打包脚本统一引用此文件。
"""
import sys
from pathlib import Path

# ─── 应用基本信息 ───
APP_NAME = "PixelFlow"
APP_TITLE = "PixelFlow - 图像处理工作台"
APP_VERSION = "1.1.7"
APP_DESCRIPTION = "图像处理工作台"
APP_PUBLISHER = "PixelFlow"

# ─── 版权信息 ───
APP_COPYRIGHT = "SEQL"
APP_COPYRIGHT_URL = "https://github.com/yincangshiwei"

# 本项目 GitHub Releases 页（版本发布与 DLSS5 运行时包下载源）
APP_RELEASES_URL = APP_COPYRIGHT_URL + "/PixelFlow/releases"

# DLSS5 高清放大运行时包 —— 本项目 Releases 固定直链（由维护者上传，
# 不随源码仓库与安装包分发任何 NVIDIA / ReShade / RenoDX 二进制）。
# 一键安装直接用此地址下载（自动走 GitHub 代理），无需 API 查询；
# 将来更新运行时包：同 tag 替换 asset 并改文件名即可。
DLSS5_BUNDLE_TAG = "DISS5"
DLSS5_BUNDLE_ASSET_NAME = "DLSS5.Runtime.v5.0.zip"
DLSS5_BUNDLE_DOWNLOAD_URL = (
    f"https://github.com/yincangshiwei/PixelFlow/releases/download/"
    f"{DLSS5_BUNDLE_TAG}/{DLSS5_BUNDLE_ASSET_NAME}"
)
# 备用：按 tag 查询 Release 元信息（固定直链不可用时枚举该 tag 的 assets 兜底）
DLSS5_BUNDLE_API_TAG = (
    "https://api.github.com/repos/yincangshiwei/PixelFlow/releases/tags/"
    + DLSS5_BUNDLE_TAG
)

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
MODELS_DIR = DATA_DIR / "models" / "matting"  # AI 抠图模型权重目录
# AI 运行时：uv 工具 + 每模型独立虚拟环境（与主程序/打包 exe 隔离）
RUNTIME_DIR = DATA_DIR / "runtime"
RUNTIME_ENVS_DIR = RUNTIME_DIR / "envs"
RUNTIME_UV_DIR = RUNTIME_DIR / "uv"

# 高清放大引擎运行时（外挂式：由用户下载/指定，**不随安装包分发**）
# DLSS5 依赖 NVIDIA 专有运行时（受 NVIDIA RTX SDK License 约束，禁止独立再分发），
# 因此只在此目录存放用户自行获取的文件，源码仓库与打包产物均不包含任何二进制。
UPSCALE_RUNTIME_DIR = RUNTIME_DIR / "upscale"
DLSS5_RUNTIME_DIR = UPSCALE_RUNTIME_DIR / "dlss5"
UPSCALE_SETTINGS_PATH = RUNTIME_DIR / "upscale_settings.json"

# 预设选中状态记忆（记录每个功能上次选中的预设名，启动时恢复）
PRESET_STATE_PATH = RUNTIME_DIR / "preset_state.json"

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
