"""ActionBarRoute —— 底部操作栏 + 批处理编排 UI 入口（P3）。

职责：
- 进度条 + 开始 / 取消 / 继续 / 重试失败 按钮
- 开始 / 续跑 / 重试：UI 层校验弹窗 → 收集 OutputPolicy / entries /
  options → 构建 RunRequest → 交给 BatchOrchestrator
- 订阅 orchestrator.job_event：进度 / 单项结果 / 结算投影到
  进度条、日志与文件列表状态；按 job_id 隔离迟到事件
- 不持有 Worker / 会话（所有权在 Orchestrator）；不读取编排器内部状态机
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from PySide6.QtWidgets import (
    QHBoxLayout, QMessageBox, QProgressBar, QPushButton, QWidget,
)

from core.batch_session import (
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCESS,
)
from services.common.batch_log_format import (
    format_duration,
    format_file_result_line,
    format_image_result_line,
    format_matting_start_log,
    format_multiline_error_debug,
)
from services.common.output_path_service import PATH_MODE_NAMES
from services.contracts.job_event import JobEvent, JobEventKind
from services.contracts.run_request import RunRequest, new_job_id


class ActionBarRoute(QWidget):
    """底部操作栏（进度条 + 处理按钮）。"""

    def __init__(
        self,
        *,
        orchestrator,
        process_tab,
        output_route,
        file_list_route,
        log_manager,
        log_writer: Callable[[str], None],
        log_clear: Callable[[], None],
        debug_writer: Callable[[str], None],
        switch_to_log_tab: Callable[[], None],
        parent=None,
    ):
        """
        :param orchestrator: BatchOrchestrator（任务与 Worker 所有权）
        :param process_tab: ProcessTabRoute（功能 / 参数 / processor 实例）
        :param output_route: OutputSettingsRoute（输出策略 / 处理范围）
        :param file_list_route: FileListRoute（输入快照 / 状态投影）
        :param log_manager: AppLogManager（功能日志分区）
        :param log_writer: 追加界面日志（同时落日志文件）
        :param log_clear: 清空界面日志文本
        :param debug_writer: 仅写后台日志文件（不进界面）
        :param switch_to_log_tab: 切换到后台日志 Tab
        """
        super().__init__(parent)
        self._orchestrator = orchestrator
        self._process_tab = process_tab
        self._output_route = output_route
        self._file_list_route = file_list_route
        self._log_manager = log_manager
        self._log = log_writer
        self._log_clear = log_clear
        self._debug = debug_writer
        self._switch_to_log_tab = switch_to_log_tab

        self._build_widget()
        self._wire_signals()

    # ── UI 构建 ──

    def _build_widget(self) -> None:
        bottom = QHBoxLayout(self)
        bottom.setContentsMargins(0, 0, 0, 0)
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%v / %m")
        bottom.addWidget(self.progress_bar, 1)
        self.btn_continue = QPushButton("继续")
        self.btn_continue.setObjectName("btn_resume")
        self.btn_continue.setMinimumHeight(38)
        self.btn_continue.setVisible(False)
        self.btn_continue.setToolTip(
            "从取消后剩余的未完成文件继续处理（含取消时中断的那张）。\n"
            "使用该批次保存的参数与输出设置。"
        )
        bottom.addWidget(self.btn_continue)
        self.btn_retry_failed = QPushButton("重试失败")
        self.btn_retry_failed.setObjectName("btn_resume")
        self.btn_retry_failed.setMinimumHeight(38)
        self.btn_retry_failed.setVisible(False)
        self.btn_retry_failed.setToolTip(
            "仅重新处理上一批失败的文件。\n"
            "使用该批次保存的参数与输出设置。"
        )
        bottom.addWidget(self.btn_retry_failed)
        self.btn_start = QPushButton("  开始处理  ")
        self.btn_start.setObjectName("btn_start")
        self.btn_start.setMinimumHeight(38)
        bottom.addWidget(self.btn_start)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.setVisible(False)
        self.btn_cancel.setMinimumHeight(38)
        bottom.addWidget(self.btn_cancel)

    def _wire_signals(self) -> None:
        self.btn_start.clicked.connect(self._start_clicked)
        self.btn_continue.clicked.connect(self._continue_clicked)
        self.btn_retry_failed.clicked.connect(self._retry_failed_clicked)
        self.btn_cancel.clicked.connect(self._cancel_clicked)
        self._orchestrator.job_event.connect(self._on_job_event)

    # ── 开始 / 续跑 / 重试 / 取消 ──

    def _start_clicked(self) -> None:
        orch = self._orchestrator
        pt = self._process_tab
        flr = self._file_list_route
        oroute = self._output_route

        if orch.is_busy():
            QMessageBox.warning(self, "提示", "已有任务正在处理，请等待完成或取消后再试")
            return
        if flr.count() == 0:
            QMessageBox.warning(self, "提示", "请先添加要处理的文件")
            return
        proc = pt.current_processor()
        if proc is None:
            QMessageBox.warning(self, "提示", "请选择处理功能")
            return

        # 若有未完成/失败会话，确认是否开新批次
        old = orch.session
        if old is not None and old.supports_resume and (
            old.can_continue() or old.can_retry_failed()
        ):
            s = old.summary()
            unfinished = s["pending"] + s["cancelled"]
            ret = QMessageBox.question(
                self,
                "开始新任务",
                (
                    f"上一批仍有未完成 {unfinished} 个、失败 {s['failed']} 个。\n"
                    f"开始新任务将清除续跑记录（已成功输出的文件不受影响）。\n\n"
                    f"是否继续开始新任务？"
                ),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if ret != QMessageBox.Yes:
                return

        # 验证输出路径（自定义模式非空）
        err = oroute.validate_for_start()
        if err:
            QMessageBox.warning(self, "提示", err)
            return

        scope_selected = oroute.scope_is_selected()
        entries = (
            flr.selected_entries_snapshot()
            if scope_selected
            else flr.all_entries_snapshot()
        )
        if not entries:
            if scope_selected:
                QMessageBox.warning(
                    self, "提示", "「仅选中」模式下请先在左侧列表选中要处理的文件"
                )
            else:
                QMessageBox.warning(self, "提示", "请先添加要处理的文件")
            return

        policy = oroute.collect_policy()
        feature_id = pt.current_feature_id or ""
        kind = pt.current_kind()
        # 经 ProcessTabRoute / FeatureService 收集并规范化
        options = pt.build_run_options(policy)
        # 输出设置：保留抠图（仅透明图 + 已开 AI 抠图时生效）
        options["keep_matting"] = (
            bool(options.get("enable_matting")) and oroute.keep_matting_active()
        )

        desc = pt.registry.get(feature_id) if feature_id else None
        supports = bool(desc.supports_resume) if desc is not None else True

        request = RunRequest(
            job_id=new_job_id(),
            feature_id=feature_id,
            kind=kind,
            entries=tuple(entries),
            options=dict(options),
            output=policy,
        )

        # 日志分区 + 清空 + 切到后台日志页（与重构前一致）
        self._log_manager.switch_feature(feature_id, clear_current=True)
        self._log_clear()
        self._switch_to_log_tab()

        ok, err = orch.begin(
            request, proc,
            processor_name=str(getattr(proc, "name", feature_id) or feature_id),
            supports_resume=supports,
        )
        if not ok:
            QMessageBox.warning(self, "提示", err)
            return

        # UI 状态与列表投影
        count = len(entries)
        self.progress_bar.setMaximum(count)
        self.progress_bar.setValue(0)
        self.btn_start.setEnabled(False)
        self.btn_continue.setVisible(False)
        self.btn_retry_failed.setVisible(False)
        flr.set_select_failed_button(False)
        self.btn_cancel.setVisible(True)
        self.btn_cancel.setEnabled(True)
        flr.reset_job_status()
        for p, _ in entries:
            flr.set_job_status(p, STATUS_PENDING)

        # 启动摘要日志
        mode_id = int(policy.path_mode)
        output_dir = policy.resolve_output_dir(entries[0][0])
        allow_file_overwrite = policy.resolve_file_overwrite()
        keep_structure = policy.effective_keep_structure()
        rel_map = policy.build_rel_path_map(entries)
        scope_name = "仅选中" if scope_selected else "全部文件"

        self._log(f"功能: {getattr(proc, 'icon', '')}  {getattr(proc, 'name', feature_id)}")
        self._log(f"处理范围: {scope_name}  共 {count} 个文件")
        self._log(f"输出模式: {PATH_MODE_NAMES[mode_id]}  →  {output_dir}")
        if mode_id in (0, 1):
            self._log(
                f"同名文件: {'覆盖' if allow_file_overwrite else '自动加后缀 (_1/_2…)'}"
            )
        if keep_structure:
            self._log(f"保留目录结构: 是（{len(rel_map)} 个文件含相对路径）")
        if options.get("keep_matting"):
            self._log("保留抠图结果: 是（文件名追加 _matted）")
        if not supports:
            self._log("说明: 当前为批量合并功能，不支持中途续跑/按文件重试")
        # AI 抠图开启时，启动摘要先写一版（Worker 内会再写设备/batch 实测值）
        if options.get("enable_matting"):
            self._log(format_matting_start_log(options))
        self._log(f"开始时间: {self._format_start_time()}")
        self._log("─" * 50)

    def _continue_clicked(self) -> None:
        self._resume_clicked("continue")

    def _retry_failed_clicked(self) -> None:
        self._resume_clicked("retry_failed")

    def _resume_clicked(self, mode: str) -> None:
        orch = self._orchestrator
        pt = self._process_tab

        if orch.is_busy():
            QMessageBox.warning(self, "提示", "已有任务正在处理，请等待完成或取消后再试")
            return
        sess = orch.session
        if sess is None or not sess.supports_resume:
            QMessageBox.information(self, "提示", "当前没有可续跑的批处理任务")
            return
        paths = sess.pending_paths() if mode == "continue" else sess.failed_paths()
        if not paths:
            tip = "没有未完成的文件" if mode == "continue" else "没有失败的文件"
            QMessageBox.information(self, "提示", tip)
            return
        proc = pt.get_processor_by_preset(sess.processor_preset_id)
        if proc is None:
            QMessageBox.warning(
                self, "提示",
                f"找不到原功能「{sess.processor_name}」，无法续跑。\n请重新选择功能后点「开始处理」。"
            )
            return

        # 切回对应功能（不改用户面板参数；Worker 用会话 options）
        pt.activate_feature(sess.processor_preset_id, clear_log_feature=False)

        self._log_manager.switch_feature(proc.preset_id, clear_current=False)
        self._switch_to_log_tab()

        ok, err = orch.resume(mode, proc)
        if not ok:
            QMessageBox.warning(self, "提示", err)
            return

        count = len(paths)
        self._sync_list_from_session()
        self.progress_bar.setMaximum(count)
        self.progress_bar.setValue(0)
        self.btn_start.setEnabled(False)
        self.btn_continue.setVisible(False)
        self.btn_retry_failed.setVisible(False)
        self.btn_cancel.setVisible(True)
        self.btn_cancel.setEnabled(True)

        action = "继续未完成" if mode == "continue" else "重试失败"
        icon = getattr(proc, "icon", "")
        options = sess.options
        self._log("─" * 50)
        self._log(f"▶ {action}: {icon}  {proc.name}  共 {count} 个文件")
        self._log(f"输出: {PATH_MODE_NAMES[sess.path_mode_id]}  →  {sess.output_dir}")
        self._log("参数: 沿用该批次快照")
        if options.get("enable_matting"):
            self._log(format_matting_start_log(options))
        if options.get("keep_matting"):
            self._log("保留抠图结果: 是（文件名追加 _matted）")
        self._log(f"开始时间: {self._format_start_time()}")
        self._log("─" * 50)

    def _cancel_clicked(self) -> None:
        if self._orchestrator.cancel():
            self._sync_list_from_session()
            self._log("⚠ 用户取消处理（已成功的文件保留；可用「继续」处理剩余）")

    def _format_start_time(self) -> str:
        start_wall = self._orchestrator.start_wall
        return start_wall.strftime("%Y-%m-%d %H:%M:%S") if start_wall else "—"

    # ── 编排事件 ──

    def _on_job_event(self, event: JobEvent) -> None:
        if not event.is_for(self._orchestrator.active_job_id):
            # 迟到事件：只允许落日志，不得覆盖当前进度 / 列表状态
            if event.kind is JobEventKind.DEBUG:
                self._debug(event.message)
            elif event.kind is JobEventKind.LOG:
                self._log(event.message)
            return
        kind = event.kind
        if kind is JobEventKind.PROGRESS:
            self._on_progress_event(event)
        elif kind is JobEventKind.ITEM_DONE:
            self._on_item_done_event(event)
        elif kind is JobEventKind.DEBUG:
            self._debug(event.message)
        elif kind is JobEventKind.LOG:
            self._log(event.message)
        elif kind is JobEventKind.FINISHED:
            self._on_finished_event(event)

    def _on_progress_event(self, event: JobEvent) -> None:
        # 动态同步最大值（批量处理器上报的 total 是文件总数，可能与初始 count 不同）
        if event.total > 0 and self.progress_bar.maximum() != event.total:
            self.progress_bar.setMaximum(event.total)
        self.progress_bar.setValue(event.current)
        if event.message and event.message != "完成":
            self._log(f"  ▶ {event.message}")
        cur = event.payload
        if cur:
            self._file_list_route.set_job_status(cur, STATUS_RUNNING)

    def _on_item_done_event(self, event: JobEvent) -> None:
        result = event.payload
        sess = self._orchestrator.session
        kind = sess.kind if sess is not None else "image"

        # 列表状态投影（会话状态已由编排器更新）
        path = getattr(result, "input_path", "") or ""
        if path and not path.startswith("分组:") and path != "批量处理":
            if result.success:
                self._file_list_route.set_job_status(path, STATUS_SUCCESS)
            else:
                self._file_list_route.set_job_status(
                    path, STATUS_FAILED,
                    error=str(getattr(result, "error", "") or ""),
                )

        line = (
            format_image_result_line(result)
            if kind == "image"
            else format_file_result_line(result)
        )
        self._log(line)
        extra = format_multiline_error_debug(result)
        if extra:
            self._debug(extra)

    def _on_finished_event(self, event: JobEvent) -> None:
        res = event.payload
        # 取消 / 终态后的列表状态同步
        self._sync_list_from_session()

        end_wall = datetime.now()
        start_text = self._format_start_time()
        end_text = end_wall.strftime("%Y-%m-%d %H:%M:%S")
        elapsed = float(res.elapsed_seconds)

        self._log("─" * 50)
        if res.supports_resume:
            self._log(
                f"处理结束!  本轮成功: {res.run_success}  本轮失败: {res.run_failed}"
                f"  |  累计成功: {res.success}  失败: {res.failed}  未完成: {res.unfinished}"
            )
            if res.user_cancelled:
                self._log("状态: 用户已取消")
        else:
            self._log(f"处理完成!  成功: {res.success}  失败: {res.failed}")
        self._log(f"开始时间: {start_text}")
        self._log(f"结束时间: {end_text}")
        self._log(f"处理耗时: {format_duration(elapsed)}")

        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.setVisible(False)
        self.update_resume_buttons()

        # 全部成功且无未完成
        if res.failed == 0 and res.unfinished == 0:
            QMessageBox.information(
                self, "完成",
                f"全部 {res.success} 个文件处理成功!\n耗时: {format_duration(elapsed)}"
            )
            return

        # 取消：仅提示结果，不在弹窗里放继续/重试（需要时用底部按钮）
        lines = []
        if res.supports_resume:
            lines.append(
                f"累计成功: {res.success}    失败: {res.failed}    未完成: {res.unfinished}"
            )
        else:
            lines.append(f"成功: {res.success}    失败: {res.failed}")
        lines.append(f"耗时: {format_duration(elapsed)}")

        if res.user_cancelled:
            lines.append("任务已取消。如需接着处理，可使用底部「继续」。")
            QMessageBox.information(self, "已取消", "\n".join(lines))
            return

        # 自然结束且有失败：提示可用底部「重试失败」
        lines.append("可查看后台日志；失败项可用底部「重试失败」重新处理。")
        QMessageBox.warning(self, "完成", "\n".join(lines))

    # ── 会话状态投影 / 按钮 ──

    def _sync_list_from_session(self) -> None:
        sess = self._orchestrator.session
        if sess is None:
            return
        for job in sess.files:
            self._file_list_route.set_job_status(job.path, job.status, error=job.error)

    def update_resume_buttons(self) -> None:
        """
        底部按钮按需显示，避免常态冗余：
        - 「继续」：仅用户取消后且仍有未完成时显示
        - 「重试失败」：仅存在失败项时显示
        """
        busy = self._orchestrator.is_busy()
        sess = self._orchestrator.session
        # 继续：必须是取消过的会话，且还有 pending/cancelled
        show_cont = (
            (not busy)
            and sess is not None
            and sess.supports_resume
            and sess.user_cancelled
            and sess.can_continue()
        )
        # 重试失败：有失败即可（取消后若也有失败可一并显示）
        show_retry = (
            (not busy)
            and sess is not None
            and sess.supports_resume
            and sess.can_retry_failed()
        )
        self.btn_continue.setVisible(show_cont)
        self.btn_continue.setEnabled(show_cont)
        self.btn_retry_failed.setVisible(show_retry)
        self.btn_retry_failed.setEnabled(show_retry)

        has_failed = sess is not None and bool(sess.failed_paths())
        self._file_list_route.set_select_failed_button(has_failed, has_failed and not busy)

        if sess and sess.supports_resume:
            s = sess.summary()
            unfinished = s["pending"] + s["cancelled"]
            self.btn_continue.setText(
                f"继续({unfinished})" if unfinished else "继续"
            )
            self.btn_retry_failed.setText(
                f"重试失败({s['failed']})" if s["failed"] else "重试失败"
            )
        else:
            self.btn_continue.setText("继续")
            self.btn_retry_failed.setText("重试失败")

    def select_failed_files(self) -> None:
        """「选中失败项」：选中失败文件并切换处理范围为仅选中。"""
        sess = self._orchestrator.session
        if sess is None:
            return
        failed = set(sess.failed_paths())
        if not failed:
            return
        self._file_list_route.select_paths(failed)
        self._output_route.set_scope_selected(True)
        self._log(f"已选中 {len(failed)} 个失败文件，处理范围已切换为「仅选中」")

    # ── 文件列表联动 ──

    def on_file_list_cleared(self) -> None:
        """清空列表后同步清除批处理会话。"""
        self._orchestrator.clear_session()
        self.update_resume_buttons()

    def on_files_removed(self, paths: list) -> None:
        """移除条目后同步会话：移除的文件不再参与续跑。"""
        self._orchestrator.remove_session_paths(paths)
        self.update_resume_buttons()

    # ── 窗口关闭 ──

    def request_shutdown(self, wait_ms: int = 15000) -> bool:
        """关闭窗口协调：禁止新任务 → 请求取消 → 有界等待。"""
        return self._orchestrator.request_shutdown(wait_ms)
