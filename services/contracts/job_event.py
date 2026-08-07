"""任务生命周期契约：job_id、任务状态机、事件载体。

状态机（见 TECHNICAL.md §1 Qt 线程与生命周期约定）：

    IDLE → STARTING → RUNNING → CANCELLING → FINISHED
                             ├─────────────→ FAILED
                             └──────────────→ FINISHED

生命周期规则（Orchestrator / Route 必须遵守）：

1. 每次批处理生成唯一 job_id；progress / item_done / log / finished 均携带它。
2. Orchestrator 强持有 Worker，直到 QThread.finished；
   cancel() 只表示「请求取消」，不表示已结束。
3. UI 只接受当前 active job_id 的状态事件；迟到事件只允许落日志，
   不得覆盖当前进度 / 列表状态（见 JobEvent.is_for）。
4. 完成结算必须在 Worker finished 后执行，
   确保 AI 子进程已在 Worker finally 清理。
5. 窗口关闭时：禁止新任务 → 请求取消 → 等待后台任务有界退出 → 再释放窗口；
   禁止直接 terminate 正在清理的 Worker。
6. 导入抽取、缩略图、配置扫描、环境创建和模型下载等后台任务同样
   需要 owner；owner 销毁后回调通过 token / weak guard 丢弃。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class JobState(Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    CANCELLING = "cancelling"
    FINISHED = "finished"
    FAILED = "failed"


class InvalidJobTransition(Exception):
    """非法的任务状态迁移。"""


_ALLOWED_TRANSITIONS = {
    JobState.IDLE: frozenset({JobState.STARTING}),
    JobState.STARTING: frozenset({JobState.RUNNING, JobState.FAILED}),
    JobState.RUNNING: frozenset(
        {JobState.CANCELLING, JobState.FINISHED, JobState.FAILED}
    ),
    JobState.CANCELLING: frozenset({JobState.FINISHED, JobState.FAILED}),
    # 终态：新任务必须使用新的 job_id / 新的状态机实例
    JobState.FINISHED: frozenset(),
    JobState.FAILED: frozenset(),
}


class JobStateMachine:
    """单个任务的状态机（一个 job_id 一个实例）。"""

    def __init__(self, job_id: str):
        if not job_id:
            raise ValueError("job_id 不能为空")
        self.job_id = job_id
        self._state = JobState.IDLE
        self.user_cancelled = False

    @property
    def state(self) -> JobState:
        return self._state

    @property
    def is_active(self) -> bool:
        return self._state in (
            JobState.STARTING,
            JobState.RUNNING,
            JobState.CANCELLING,
        )

    @property
    def is_terminal(self) -> bool:
        return self._state in (JobState.FINISHED, JobState.FAILED)

    def can_transition(self, new_state: JobState) -> bool:
        return new_state in _ALLOWED_TRANSITIONS[self._state]

    def transition(self, new_state: JobState) -> None:
        if not self.can_transition(new_state):
            raise InvalidJobTransition(
                f"job {self.job_id}: 非法状态迁移 {self._state.value} → {new_state.value}"
            )
        self._state = new_state

    def request_cancel(self) -> bool:
        """请求取消（仅 RUNNING 时有效）。

        返回是否进入 CANCELLING。注意：返回 True 不代表任务已结束，
        结算必须等待 Worker finished。
        """
        if self._state is not JobState.RUNNING:
            return False
        self.user_cancelled = True
        self._state = JobState.CANCELLING
        return True


class JobEventKind(Enum):
    PROGRESS = "progress"      # 进度 (current, total, message)
    ITEM_DONE = "item_done"    # 单项结果（payload=ProcessResult/FileProcessResult）
    LOG = "log"                # 面向用户的日志行
    DEBUG = "debug"            # 调试/异常详情
    FINISHED = "finished"      # 终态（payload=JobResult）


@dataclass(frozen=True)
class JobEvent:
    """携带 job_id 的任务事件（由 Orchestrator 包装 Worker 信号产生）。"""

    job_id: str
    kind: JobEventKind
    current: int = 0
    total: int = 0
    message: str = ""
    payload: object = None

    def is_for(self, active_job_id: str | None) -> bool:
        """迟到事件隔离：仅当事件属于当前 active 任务时才可更新 UI 状态。

        不满足时只允许落日志，不得覆盖当前进度 / 列表状态。
        """
        return bool(active_job_id) and self.job_id == active_job_id


@dataclass(frozen=True)
class JobResult:
    """任务终态结算（Worker finished 后由 Orchestrator 汇总）。

    字段语义（与既有 _on_all_done 结算口径一致）：
    - supports_resume 会话：success/failed/unfinished 为 **累计** 值，
      run_success/run_failed 为 **本轮** 值
    - 非续跑会话：success/failed 即本轮结果，unfinished 恒 0
    """

    job_id: str
    user_cancelled: bool = False
    total: int = 0
    success: int = 0
    failed: int = 0
    cancelled: int = 0
    elapsed_seconds: float = 0.0
    # P3 扩展：本轮与累计口径（默认值保持向后兼容）
    unfinished: int = 0        # pending + cancelled（续跑会话的未完成数）
    run_success: int = 0       # 本轮成功数
    run_failed: int = 0        # 本轮失败数
    supports_resume: bool = False
