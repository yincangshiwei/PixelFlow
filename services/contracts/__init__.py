"""跨层稳定数据契约（DTO）。

本包只允许使用 Python 标准库：
- 不得 import PySide6 / PIL / core / ui 中任何模块
- 不得携带 QWidget、QModelIndex、QListWidgetItem 等 UI 对象
- 必须可在无 QApplication 的环境下导入

契约清单（对应 TECHNICAL.md §1 跨层契约）：
- ImportEntry        文件列表唯一业务真相
- FeatureDescriptor  功能统一描述（图片 / 文件 / 批量合并）
- OutputPolicy       输出路径策略（四种模式 + 覆盖/保留结构语义）
- RunRequest         一次批处理请求的不可变快照
- JobEvent/JobResult 携带 job_id 的任务事件与终态
- JobStateMachine    任务状态机（IDLE→STARTING→RUNNING→CANCELLING→FINISHED/FAILED）
"""
from .import_entry import ImportEntry, ImportSource, new_entry_id
from .feature_descriptor import FeatureDescriptor, InputKind
from .output_policy import (
    AUTO_SUBFOLDER_NAME,
    OutputPolicy,
    PathMode,
    default_desktop_path,
)
from .run_request import RunRequest, new_job_id
from .job_event import (
    InvalidJobTransition,
    JobEvent,
    JobEventKind,
    JobResult,
    JobState,
    JobStateMachine,
)

__all__ = [
    "ImportEntry",
    "ImportSource",
    "new_entry_id",
    "FeatureDescriptor",
    "InputKind",
    "OutputPolicy",
    "PathMode",
    "AUTO_SUBFOLDER_NAME",
    "default_desktop_path",
    "RunRequest",
    "new_job_id",
    "JobState",
    "JobStateMachine",
    "InvalidJobTransition",
    "JobEvent",
    "JobEventKind",
    "JobResult",
]
