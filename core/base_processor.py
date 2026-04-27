"""
PixelFlow 处理器基类
所有图像处理功能都应继承此基类，实现插件化架构。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from PIL import Image
from PySide6.QtWidgets import QWidget


@dataclass
class ProcessResult:
    """通用处理结果"""
    input_path: str = ""
    output_path: str = ""
    success: bool = True
    error: str = ""
    details: dict = None

    def __post_init__(self):
        if self.details is None:
            self.details = {}


class BaseProcessor(ABC):
    """
    处理器基类。
    每个图像处理功能模块都应继承此类，注册后自动出现在功能菜单中。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """处理器显示名称"""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """处理器简要说明"""
        ...

    @property
    def icon(self) -> str:
        """图标字符（可选覆盖）"""
        return "🔧"

    @property
    @abstractmethod
    def preset_id(self) -> str:
        """预设目录名（英文，唯一标识），用作 presets/<preset_id>/ 文件夹名"""
        ...

    @property
    def is_batch_processor(self) -> bool:
        """
        是否为批量合并处理器。
        如果为 True，Worker 将调用 process_batch(file_list, options, output_dir)
        而不是逐个调用 process(img, options)。
        """
        return False

    def process_batch(self, file_list: list[str], options: dict, output_dir: str, progress_callback=None) -> list[ProcessResult]:
        """
        批量合并处理接口（当 is_batch_processor 为 True 时调用）。
        :param file_list:        输入文件路径列表
        :param options:          用户参数
        :param output_dir:       输出目录
        :param progress_callback: 可选的回调函数 progress_callback(current, total, msg)
        :return:                 处理结果列表
        """
        raise NotImplementedError("Batch processor must implement process_batch()")

    @abstractmethod
    def create_panel(self, parent: QWidget = None) -> QWidget:
        """创建该处理器的参数设置面板"""
        ...

    @abstractmethod
    def process(self, img: Image.Image, options: dict) -> tuple[Image.Image, dict]:
        """处理单张图片"""
        ...

    @abstractmethod
    def gather_options(self) -> dict:
        """从 UI 面板收集当前参数，返回字典"""
        ...

    @abstractmethod
    def apply_options(self, options: dict):
        """将参数字典应用到 UI 面板（用于加载预设）"""
        ...

    @abstractmethod
    def default_options(self) -> dict:
        """返回该处理器的出厂默认参数"""
        ...

    @abstractmethod
    def get_output_format(self) -> str:
        """返回输出格式: png / jpg / webp"""
        ...


# ── 处理器注册表 ──
_registry: list[type[BaseProcessor]] = []


def register_processor(cls: type[BaseProcessor]):
    """装饰器：注册一个处理器"""
    _registry.append(cls)
    return cls


def get_all_processors() -> list[type[BaseProcessor]]:
    """获取所有已注册的处理器类"""
    return list(_registry)
