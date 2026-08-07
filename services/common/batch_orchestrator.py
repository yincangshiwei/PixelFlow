"""BatchOrchestrator —— 通用批处理编排。

职责（见 TECHNICAL.md §3.3 批处理数据流）：
- 校验：无并发任务、请求输入非空、任务状态允许启动
- 会话：创建 / 恢复 BatchSession（续跑快照），维护 job_id 与状态机
- 启动：经 WorkerFactory 选择 ProcessWorker / FileProcessWorker
- 转发：Worker progress / item_done / finished / debug → 带 job_id 的 JobEvent
- 生命周期：取消请求、Worker 强引用、QThread.finished 后结算、窗口关闭协调

不负责：
- 不读取或操作 QListWidget / QWidget（UI 投影由 ActionBarRoute 完成）
- 不收集功能参数、不实现 process() 算法
- 不包含功能 id 特判；keep_matting 等由 FeatureService / Route 构建

线程契约（见 TECHNICAL.md §1 Qt 线程与生命周期约定）：本类为主线程 QObject；
Worker 信号经队列连接投递到主线程槽函数，结算只在 QThread.finished 后执行。
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal

from core.batch_session import BatchSession
from services.contracts.job_event import (
    InvalidJobTransition,
    JobEvent,
    JobEventKind,
    JobResult,
    JobState,
    JobStateMachine,
)
from services.contracts.output_policy import OutputPolicy, PathMode
from services.contracts.run_request import RunRequest, new_job_id
from .worker_factory import create_worker_for_request


# ── 纯逻辑辅助（可脱离 Qt 单测）──


def policy_from_session(sess: BatchSession) -> OutputPolicy:
    """由会话快照重建输出策略（续跑/重试沿用原批次输出设置）。

    会话中 auto_subfolder / keep_structure 存的是开始时的 **生效值**，
    OutputPolicy 的派生语义对其幂等（再次计算结果不变）。
    """
    return OutputPolicy(
        path_mode=PathMode(sess.path_mode_id),
        root_dir=sess.output_dir,
        auto_subfolder=sess.auto_subfolder,
        keep_structure=sess.keep_structure,
        file_overwrite=sess.file_overwrite,
    )


def build_resume_entries(
    sess: BatchSession, paths: list[str]
) -> tuple[tuple[str, Optional[str]], ...]:
    """续跑/重试输入快照：(路径, 相对路径|None)，相对路径取自会话 rel_path_map。"""
    return tuple((p, sess.rel_path_map.get(p)) for p in paths)


def record_result_to_session(sess: Optional[BatchSession], result) -> None:
    """单项结果写入会话（与既有 _record_result_to_session 语义一致）。

    批量合并占位路径（分组:* / 批量处理）不参与按文件状态跟踪。
    """
    if sess is None or not sess.supports_resume:
        return
    path = getattr(result, "input_path", "") or ""
    if not path or path.startswith("分组:") or path == "批量处理":
        return
    if result.success:
        sess.mark_success(path, getattr(result, "output_path", "") or "")
    else:
        sess.mark_failed(path, str(getattr(result, "error", "") or ""))


def summarize_job_results(
    job_id: str,
    sess: Optional[BatchSession],
    results: list,
    elapsed_seconds: float,
) -> JobResult:
    """Worker finished 后的终态结算（口径与既有 _on_all_done 一致）。"""
    run_success = sum(1 for r in results if getattr(r, "success", False))
    run_failed = len(results) - run_success
    if sess is not None and sess.supports_resume:
        s = sess.summary()
        return JobResult(
            job_id=job_id,
            user_cancelled=bool(sess.user_cancelled),
            total=s["total"],
            success=s["success"],
            failed=s["failed"],
            cancelled=s["cancelled"],
            unfinished=s["pending"] + s["cancelled"],
            elapsed_seconds=elapsed_seconds,
            run_success=run_success,
            run_failed=run_failed,
            supports_resume=True,
        )
    return JobResult(
        job_id=job_id,
        user_cancelled=False,
        total=len(results),
        success=run_success,
        failed=run_failed,
        cancelled=0,
        unfinished=0,
        elapsed_seconds=elapsed_seconds,
        run_success=run_success,
        run_failed=run_failed,
        supports_resume=False,
    )


# ── 编排器 ──


class BatchOrchestrator(QObject):
    """批处理任务编排器（主线程 QObject，强持有 Worker 直至 finished）。"""

    # JobEvent 载体（kind: PROGRESS / ITEM_DONE / LOG / DEBUG / FINISHED）
    job_event = Signal(object)

    def __init__(
        self,
        worker_factory: Optional[Callable] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._worker_factory = worker_factory or create_worker_for_request
        self._worker = None
        self._session: Optional[BatchSession] = None
        self._machine: Optional[JobStateMachine] = None
        self._active_job_id: Optional[str] = None
        self._run_mode: str = "full"  # full | continue | retry_failed
        self._t0_mono: Optional[float] = None
        self._t0_wall: Optional[datetime] = None
        self._shutting_down: bool = False

    # ── 对外查询 ──

    @property
    def session(self) -> Optional[BatchSession]:
        return self._session

    @property
    def active_job_id(self) -> Optional[str]:
        return self._active_job_id

    @property
    def run_mode(self) -> str:
        return self._run_mode

    @property
    def start_wall(self) -> Optional[datetime]:
        """当前/最近一次任务的开始墙钟时间（结算日志展示用）。"""
        return self._t0_wall

    def is_busy(self) -> bool:
        worker = self._worker
        return bool(worker is not None and worker.isRunning())

    # ── 启动 ──

    def begin(
        self,
        request: RunRequest,
        processor,
        *,
        processor_name: str = "",
        supports_resume: bool = True,
    ) -> tuple[bool, str]:
        """全新整批。返回 (是否启动, 错误文案)。"""
        if self._shutting_down:
            return False, "程序正在关闭，无法开始新任务"
        if self.is_busy():
            return False, "已有任务正在处理，请等待完成或取消后再试"
        if not request.entries:
            return False, "输入为空，无法开始处理"

        policy = request.output
        first_src = request.entries[0][0]
        output_dir = policy.resolve_output_dir(first_src)

        session = BatchSession.create(
            kind=request.kind,
            processor_preset_id=request.feature_id,
            processor_name=processor_name or request.feature_id,
            supports_resume=supports_resume,
            options=request.options,
            output_dir=output_dir,
            auto_subfolder=policy.effective_auto_subfolder(),
            overwrite=policy.src_overwrite,
            file_overwrite=policy.resolve_file_overwrite(),
            keep_structure=policy.effective_keep_structure(),
            path_mode_id=int(policy.path_mode),
            entries=list(request.entries),
        )
        self._session = session
        self._run_mode = "full"

        rel_map = policy.build_rel_path_map(request.entries)
        worker = self._worker_factory(
            request, processor,
            output_dir=output_dir,
            rel_path_map=rel_map,
            file_index_map=session.order_map(),
        )
        self._launch(worker, request)
        return True, ""

    def resume(self, mode: str, processor) -> tuple[bool, str]:
        """续跑 / 重试失败：复用会话快照（options / 输出设置 / 原序号）。

        :param mode: "continue" | "retry_failed"
        """
        if self._shutting_down:
            return False, "程序正在关闭，无法开始新任务"
        if self.is_busy():
            return False, "已有任务正在处理，请等待完成或取消后再试"
        sess = self._session
        if sess is None or not sess.supports_resume:
            return False, "当前没有可续跑的批处理任务"
        paths = sess.pending_paths() if mode == "continue" else sess.failed_paths()
        if not paths:
            tip = "没有未完成的文件" if mode == "continue" else "没有失败的文件"
            return False, tip

        sess.reset_for_retry(paths)
        self._run_mode = mode

        policy = policy_from_session(sess)
        entries = build_resume_entries(sess, paths)
        request = RunRequest(
            job_id=new_job_id(),
            feature_id=sess.processor_preset_id,
            kind=sess.kind,
            entries=entries,
            options=dict(sess.options),
            output=policy,
            order_map=sess.order_map(),
            resume=True,
        )
        rel_map = policy.build_rel_path_map(entries)
        worker = self._worker_factory(
            request, processor,
            output_dir=sess.output_dir,
            rel_path_map=rel_map,
            file_index_map=sess.order_map(),
        )
        self._launch(worker, request)
        return True, ""

    def _launch(self, worker, request: RunRequest) -> None:
        machine = JobStateMachine(request.job_id)
        machine.transition(JobState.STARTING)
        self._machine = machine
        self._active_job_id = request.job_id
        self._worker = worker
        self._t0_mono = time.monotonic()
        self._t0_wall = datetime.now()

        if request.kind == "image":
            worker.image_done.connect(self._on_worker_item_done)
        else:
            worker.file_done.connect(self._on_worker_item_done)
        worker.progress.connect(self._on_worker_progress)
        worker.finished.connect(self._on_worker_finished)
        worker.debug.connect(self._on_worker_debug)

        machine.transition(JobState.RUNNING)
        worker.start()

    # ── 取消 / 关闭 ──

    def cancel(self) -> bool:
        """请求取消（不代表已结束；结算等待 Worker finished）。"""
        worker = self._worker
        if worker is None:
            return False
        # 先标记当前 running，避免 cancel 抢在 item_done 前
        cur = getattr(worker, "current_path", None)
        if self._session is not None and cur:
            self._session.mark_running(cur)
        worker.cancel()
        if self._machine is not None:
            self._machine.request_cancel()
        if self._session is not None:
            self._session.mark_cancelled_running()
        return True

    def request_shutdown(self, wait_ms: int = 15000) -> bool:
        """窗口关闭协调：禁止新任务 → 请求取消 → 有界等待（§5.4 规则 5）。

        返回 True 表示可以安全关闭；False 表示超时仍未退出，
        调用方应忽略关闭事件，禁止 terminate 正在清理的 Worker。
        """
        self._shutting_down = True
        worker = self._worker
        if worker is None:
            return True
        if worker.isRunning():
            self.cancel()
            return bool(worker.wait(wait_ms))
        return True

    # ── 会话维护（列表增删同步）──

    def clear_session(self) -> None:
        """清空列表时同步清除会话（与既有 _clear_batch_session 一致）。"""
        self._session = None
        self._run_mode = "full"

    def remove_session_paths(self, paths) -> None:
        """移除条目后同步会话：移除的文件不再参与续跑。"""
        if self._session is not None:
            self._session.remove_paths(paths)

    # ── Worker 信号槽（队列连接投递到主线程）──

    def _on_worker_progress(self, current, total, filename):
        cur = getattr(self._worker, "current_path", None)
        if self._session is not None and cur:
            self._session.mark_running(cur)
        self.job_event.emit(JobEvent(
            job_id=self._active_job_id or "",
            kind=JobEventKind.PROGRESS,
            current=int(current),
            total=int(total),
            message=filename or "",
            payload=cur,
        ))

    def _on_worker_item_done(self, result):
        record_result_to_session(self._session, result)
        self.job_event.emit(JobEvent(
            job_id=self._active_job_id or "",
            kind=JobEventKind.ITEM_DONE,
            payload=result,
        ))

    def _on_worker_debug(self, text: str):
        self.job_event.emit(JobEvent(
            job_id=self._active_job_id or "",
            kind=JobEventKind.DEBUG,
            message=text,
        ))

    def _on_worker_finished(self):
        """仅在 QThread 已完全退出后结算，避免销毁仍处于清理阶段的线程对象。"""
        worker = self._worker
        if worker is None:
            return
        results = list(getattr(worker, "results", []) or [])
        sess = self._session
        # 取消时可能仍有 running 未落到 cancelled
        if sess is not None and sess.user_cancelled:
            sess.mark_cancelled_running()

        elapsed = (
            time.monotonic() - self._t0_mono
            if self._t0_mono is not None
            else 0.0
        )
        job_result = summarize_job_results(
            self._active_job_id or "", sess, results, elapsed
        )

        if self._machine is not None and not self._machine.is_terminal:
            try:
                self._machine.transition(JobState.FINISHED)
            except InvalidJobTransition:
                pass

        # 释放 Worker 强引用（finished 已发出，AI 子进程已在 Worker finally 清理）
        self._worker = None
        self._t0_mono = None
        self._shutting_down = False

        self.job_event.emit(JobEvent(
            job_id=job_result.job_id,
            kind=JobEventKind.FINISHED,
            total=job_result.total,
            payload=job_result,
        ))
