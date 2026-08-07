"""
PixelFlow 处理器基类
所有图像处理功能都应继承此基类，实现插件化架构。

P6：处理器只负责 process / process_batch 纯处理逻辑；
参数面板、参数收集/应用等 UI 职责已迁至 ui/routes/process/features/*
（FeatureRoute）与 services/features/*（FeatureService）。
功能的唯一权威注册入口是 services.features.catalog（FeatureDescriptor），
不再有装饰器注册表。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from PIL import Image


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
    处理器基类（纯处理，无 UI）。

    每个图像处理功能模块都应继承此类；功能注册、菜单顺序、预设与
    编排统一由 services.features.catalog 的 FeatureDescriptor 描述。
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
    def process(self, img: Image.Image, options: dict) -> tuple[Image.Image, dict]:
        """处理单张图片"""
        ...

    @abstractmethod
    def default_options(self) -> dict:
        """返回该处理器的出厂默认参数"""
        ...
