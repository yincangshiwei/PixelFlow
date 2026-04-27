"""
PixelFlow 文件处理器基类
用于处理非图片文件（如文档格式转换），与 BaseProcessor（图片处理）并存。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from PySide6.QtWidgets import QWidget


@dataclass
class FileProcessResult:
    """文件处理结果"""
    input_path: str = ""
    output_path: str = ""
    success: bool = True
    error: str = ""
    details: dict = field(default_factory=dict)


class BaseFileProcessor(ABC):
    """
    文件处理器基类。
    适用于输入/输出不是 PIL Image 的功能（如文档格式转换）。
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
        return "📄"

    @property
    @abstractmethod
    def preset_id(self) -> str:
        """预设目录名（英文唯一标识）"""
        ...

    @property
    @abstractmethod
    def supported_extensions(self) -> set[str]:
        """该处理器支持的输入文件扩展名集合，如 {'.docx', '.pdf'}"""
        ...

    @abstractmethod
    def create_panel(self, parent: QWidget = None) -> QWidget:
        """创建参数设置面板"""
        ...

    @abstractmethod
    def gather_options(self) -> dict:
        """从 UI 面板收集当前参数"""
        ...

    @abstractmethod
    def apply_options(self, options: dict):
        """将参数字典应用到 UI 面板（加载预设用）"""
        ...

    @abstractmethod
    def default_options(self) -> dict:
        """返回出厂默认参数"""
        ...

    @abstractmethod
    def process_file(self, input_path: str, output_dir: str,
                     options: dict, index: int) -> FileProcessResult:
        """
        处理单个文件。
        :param input_path:  输入文件完整路径
        :param output_dir:  输出目录（已创建）
        :param options:     参数字典
        :param index:       当前文件在批次中的序号（从 1 开始，供重命名使用）
        :return:            FileProcessResult
        """
        ...


# ── 文件处理器注册表 ──
_file_registry: list[type[BaseFileProcessor]] = []


def register_file_processor(cls: type[BaseFileProcessor]):
    """装饰器：注册一个文件处理器"""
    _file_registry.append(cls)
    return cls


def get_all_file_processors() -> list[type[BaseFileProcessor]]:
    """获取所有已注册的文件处理器类"""
    return list(_file_registry)
