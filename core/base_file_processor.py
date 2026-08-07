"""
PixelFlow 文件处理器基类
用于处理非图片文件（如文档格式转换），与 BaseProcessor（图片处理）并存。

P6：文件处理器同样只负责处理逻辑；参数面板与参数收集/应用等 UI 职责
由 FeatureRoute / FeatureService 承担，功能注册统一走
services.features.catalog 的 FeatureDescriptor（InputKind.FILE）。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


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
    文件处理器基类（纯处理，无 UI）。
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
