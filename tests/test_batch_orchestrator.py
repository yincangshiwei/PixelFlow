"""P3 测试：BatchOrchestrator 编排与纯逻辑辅助。

覆盖：
- policy_from_session / build_resume_entries / record_result_to_session /
  summarize_job_results 纯函数（与既有 _begin_process / _on_all_done 口径一致）
- 编排器：校验、会话创建、job_id 事件、取消、结算、续跑/重试请求构建
  （FakeWorker 替身，不启动真实 QThread）
"""
import unittest

from PySide6.QtCore import QCoreApplication, QObject, Signal

from core.batch_session import (
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCESS,
)
from services.common.batch_orchestrator import (
    BatchOrchestrator,
    build_resume_entries,
    policy_from_session,
    record_result_to_session,
    summarize_job_results,
)
from services.contracts.job_event import JobEventKind
from services.contracts.output_policy import OutputPolicy, PathMode
from services.contracts.run_request import RunRequest, new_job_id

PATH_A = r"C:\img\a.png"
PATH_B = r"C:\img\sub\b.png"
PATH_C = r"C:\img\c.jpg"
ENTRIES = ((PATH_A, None), (PATH_B, "sub/b.png"), (PATH_C, None))


class FakeProcessor:
    preset_id = "basic_process"
    name = "基础处理"
    icon = "🛠"


class FakeResult:
    def __init__(self, input_path, success=True, error="", output_path="", details=None):
        self.input_path = input_path
        self.success = success
        self.error = error
        self.output_path = output_path
        self.details = details or {}


class FakeWorker(QObject):
    """ProcessWorker 替身：同接口信号 + start/cancel/isRunning/wait。"""

    progress = Signal(int, int, str)
    image_done = Signal(object)
    file_done = Signal(object)
    finished = Signal()
    debug = Signal(str)

    def __init__(self):
        super().__init__()
        self.started = False
        self._running = False
        self.cancelled = False
        self.results = []
        self.current_path = None

    def start(self):
        self.started = True
        self._running = True

    def isRunning(self):
        return self._running

    def cancel(self):
        self.cancelled = True

    def wait(self, timeout_ms=None):
        self._running = False
        return True

    def stop_running(self):
        self._running = False


def make_policy(**overrides) -> OutputPolicy:
    kwargs = dict(
        path_mode=PathMode.DESKTOP,
        root_dir=r"C:\Users\me\Desktop",
        auto_subfolder=True,
        keep_structure=True,
        file_overwrite=False,
    )
    kwargs.update(overrides)
    return OutputPolicy(**kwargs)


def make_request(policy=None, entries=ENTRIES, feature_id="basic_process", kind="image"):
    return RunRequest(
        job_id=new_job_id(),
        feature_id=feature_id,
        kind=kind,
        entries=entries,
        options={"enable_format": True},
        output=policy or make_policy(),
    )


class TestPureHelpers(unittest.TestCase):
    def _make_session(self, **overrides):
        from core.batch_session import BatchSession

        kwargs = dict(
            kind="image",
            processor_preset_id="basic_process",
            processor_name="基础处理",
            supports_resume=True,
            options={"enable_format": True},
            output_dir=r"C:\Users\me\Desktop",
            auto_subfolder=True,
            overwrite=False,
            file_overwrite=False,
            keep_structure=True,
            path_mode_id=0,
            entries=list(ENTRIES),
        )
        kwargs.update(overrides)
        return BatchSession.create(**kwargs)

    def test_policy_from_session_roundtrip_desktop(self):
        sess = self._make_session()
        policy = policy_from_session(sess)
        self.assertIs(policy.path_mode, PathMode.DESKTOP)
        self.assertEqual(policy.root_dir, sess.output_dir)
        self.assertTrue(policy.effective_auto_subfolder())
        self.assertTrue(policy.effective_keep_structure())
        self.assertFalse(policy.resolve_file_overwrite())

    def test_policy_from_session_src_overwrite(self):
        sess = self._make_session(
            path_mode_id=2, output_dir=r"C:\src",
            auto_subfolder=False, overwrite=True,
            file_overwrite=True, keep_structure=False,
        )
        policy = policy_from_session(sess)
        self.assertTrue(policy.src_overwrite)
        self.assertFalse(policy.effective_auto_subfolder())
        self.assertFalse(policy.effective_keep_structure())
        self.assertTrue(policy.resolve_file_overwrite())

    def test_policy_from_session_src_copy(self):
        sess = self._make_session(
            path_mode_id=3, output_dir=r"C:\src",
            auto_subfolder=False, overwrite=False,
            file_overwrite=False, keep_structure=False,
        )
        policy = policy_from_session(sess)
        self.assertFalse(policy.src_overwrite)
        self.assertFalse(policy.resolve_file_overwrite())

    def test_build_resume_entries_keeps_rel_paths(self):
        sess = self._make_session()
        entries = build_resume_entries(sess, [PATH_A, PATH_B])
        self.assertEqual(entries, ((PATH_A, None), (PATH_B, "sub/b.png")))

    def test_record_result_to_session_success_and_failed(self):
        sess = self._make_session()
        record_result_to_session(sess, FakeResult(PATH_A, True, output_path="o.png"))
        record_result_to_session(sess, FakeResult(PATH_B, False, error="boom"))
        self.assertEqual(sess.get(PATH_A).status, STATUS_SUCCESS)
        self.assertEqual(sess.get(PATH_B).status, STATUS_FAILED)
        self.assertEqual(sess.get(PATH_B).error, "boom")

    def test_record_result_skips_group_and_batch_placeholders(self):
        sess = self._make_session()
        record_result_to_session(sess, FakeResult("分组: 排版导出", True))
        record_result_to_session(sess, FakeResult("批量处理", False, error="x"))
        record_result_to_session(sess, FakeResult("", True))
        self.assertEqual(sess.count(), 3)
        self.assertEqual(sess.count(STATUS_PENDING), 3)

    def test_record_result_noop_without_resume_support(self):
        sess = self._make_session(supports_resume=False)
        record_result_to_session(sess, FakeResult(PATH_A, True))
        self.assertEqual(sess.get(PATH_A).status, STATUS_PENDING)

    def test_summarize_with_resume_session(self):
        sess = self._make_session()
        sess.mark_success(PATH_A)
        sess.mark_failed(PATH_B, "e")
        results = [FakeResult(PATH_A, True), FakeResult(PATH_B, False)]
        res = summarize_job_results("job-1", sess, results, 1.5)
        self.assertTrue(res.supports_resume)
        self.assertEqual(res.total, 3)
        self.assertEqual(res.success, 1)
        self.assertEqual(res.failed, 1)
        self.assertEqual(res.unfinished, 1)  # PATH_C pending
        self.assertEqual(res.run_success, 1)
        self.assertEqual(res.run_failed, 1)
        self.assertFalse(res.user_cancelled)

    def test_summarize_without_session(self):
        results = [FakeResult(PATH_A, True), FakeResult(PATH_C, False, error="x")]
        res = summarize_job_results("job-2", None, results, 0.5)
        self.assertFalse(res.supports_resume)
        self.assertEqual(res.total, 2)
        self.assertEqual(res.success, 1)
        self.assertEqual(res.failed, 1)
        self.assertEqual(res.unfinished, 0)


class TestBatchOrchestrator(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.created_workers = []

        def factory(request, processor, *, output_dir, rel_path_map, file_index_map):
            w = FakeWorker()
            self.created_workers.append({
                "worker": w,
                "request": request,
                "output_dir": output_dir,
                "rel_path_map": rel_path_map,
                "file_index_map": file_index_map,
            })
            return w

        self.orch = BatchOrchestrator(worker_factory=factory)
        self.events = []
        self.orch.job_event.connect(self.events.append)
        self.proc = FakeProcessor()

    # ── begin ──

    def test_begin_rejects_empty_entries(self):
        req = make_request(entries=())
        ok, err = self.orch.begin(req, self.proc)
        self.assertFalse(ok)
        self.assertIn("输入为空", err)
        self.assertIsNone(self.orch.session)

    def test_begin_creates_session_and_starts_worker(self):
        req = make_request()
        ok, err = self.orch.begin(
            req, self.proc, processor_name="基础处理", supports_resume=True
        )
        self.assertTrue(ok, err)
        sess = self.orch.session
        self.assertIsNotNone(sess)
        self.assertEqual(sess.processor_preset_id, "basic_process")
        self.assertEqual(sess.count(), 3)
        self.assertEqual(sess.order_map()[PATH_B], 2)
        self.assertTrue(sess.auto_subfolder)
        self.assertTrue(sess.keep_structure)
        self.assertFalse(sess.overwrite)
        # worker 参数
        info = self.created_workers[-1]
        self.assertTrue(info["worker"].started)
        self.assertEqual(info["file_index_map"], sess.order_map())
        self.assertEqual(info["rel_path_map"], {PATH_B: "sub/b.png"})
        self.assertTrue(self.orch.is_busy())
        self.assertEqual(self.orch.active_job_id, req.job_id)

    def test_begin_rejects_when_busy(self):
        ok, _ = self.orch.begin(make_request(), self.proc)
        self.assertTrue(ok)
        ok2, err2 = self.orch.begin(make_request(), self.proc)
        self.assertFalse(ok2)
        self.assertIn("已有任务正在处理", err2)

    # ── 事件流 ──

    def _begin(self):
        req = make_request()
        ok, err = self.orch.begin(req, self.proc, processor_name="基础处理")
        self.assertTrue(ok, err)
        return req, self.created_workers[-1]["worker"]

    def test_progress_marks_running_and_emits_event(self):
        req, worker = self._begin()
        worker.current_path = PATH_A
        worker.progress.emit(1, 3, "a.png")
        ev = self.events[-1]
        self.assertIs(ev.kind, JobEventKind.PROGRESS)
        self.assertEqual(ev.job_id, req.job_id)
        self.assertEqual((ev.current, ev.total), (1, 3))
        self.assertEqual(ev.payload, PATH_A)
        self.assertEqual(self.orch.session.get(PATH_A).status, STATUS_RUNNING)

    def test_item_done_records_session_and_emits_event(self):
        req, worker = self._begin()
        worker.image_done.emit(FakeResult(PATH_A, True, output_path="o.png"))
        worker.image_done.emit(FakeResult(PATH_B, False, error="boom"))
        self.assertEqual(self.orch.session.get(PATH_A).status, STATUS_SUCCESS)
        self.assertEqual(self.orch.session.get(PATH_B).status, STATUS_FAILED)
        kinds = [e.kind for e in self.events]
        self.assertEqual(kinds.count(JobEventKind.ITEM_DONE), 2)
        self.assertTrue(all(e.job_id == req.job_id for e in self.events))

    def test_cancel_marks_session_and_worker(self):
        req, worker = self._begin()
        worker.current_path = PATH_A
        self.orch.session.mark_running(PATH_A)
        self.assertTrue(self.orch.cancel())
        self.assertTrue(worker.cancelled)
        self.assertTrue(self.orch.session.user_cancelled)
        self.assertEqual(self.orch.session.get(PATH_A).status, STATUS_CANCELLED)

    def test_finished_settles_and_releases_worker(self):
        req, worker = self._begin()
        # 真实事件流：item_done 先行（会话状态由此累积），finished 结算
        worker.image_done.emit(FakeResult(PATH_A, True, output_path="o.png"))
        worker.image_done.emit(FakeResult(PATH_B, False, error="e"))
        worker.results = [FakeResult(PATH_A, True), FakeResult(PATH_B, False, error="e")]
        worker.stop_running()
        worker.finished.emit()
        finished_events = [e for e in self.events if e.kind is JobEventKind.FINISHED]
        self.assertEqual(len(finished_events), 1)
        res = finished_events[0].payload
        self.assertEqual(res.job_id, req.job_id)
        self.assertTrue(res.supports_resume)
        self.assertEqual(res.run_success, 1)
        self.assertEqual(res.run_failed, 1)
        self.assertEqual(res.success, 1)
        self.assertEqual(res.failed, 1)
        self.assertEqual(res.unfinished, 1)
        self.assertFalse(self.orch.is_busy())
        self.assertIsNotNone(self.orch.start_wall)

    def test_finished_after_cancel_reports_user_cancelled(self):
        req, worker = self._begin()
        worker.current_path = PATH_A
        self.orch.session.mark_running(PATH_A)
        self.orch.cancel()
        worker.results = [FakeResult(PATH_A, True)]
        worker.stop_running()
        worker.finished.emit()
        res = [e for e in self.events if e.kind is JobEventKind.FINISHED][0].payload
        self.assertTrue(res.user_cancelled)
        self.assertEqual(self.orch.session.get(PATH_A).status, STATUS_CANCELLED)

    # ── resume / retry ──

    def _finish_with(self, worker, results):
        worker.results = results
        worker.stop_running()
        worker.finished.emit()

    def test_resume_continue_uses_session_snapshot(self):
        req, worker = self._begin()
        sess = self.orch.session
        sess.mark_success(PATH_A, output_path="o")
        sess.mark_cancelled_running()  # B/C pending
        self._finish_with(worker, [FakeResult(PATH_A, True)])

        ok, err = self.orch.resume("continue", self.proc)
        self.assertTrue(ok, err)
        info = self.created_workers[-1]
        req2 = info["request"]
        self.assertTrue(req2.resume)
        self.assertEqual(req2.file_list(), [PATH_B, PATH_C])
        # options / 输出设置沿用会话快照
        self.assertEqual(req2.options, sess.options)
        self.assertEqual(info["output_dir"], sess.output_dir)
        self.assertEqual(info["file_index_map"], sess.order_map())
        self.assertEqual(self.orch.run_mode, "continue")
        # 会话状态已重置为 pending
        self.assertEqual(sess.get(PATH_B).status, STATUS_PENDING)
        self.assertEqual(sess.get(PATH_A).status, STATUS_SUCCESS)

    def test_resume_retry_failed_only_failed_paths(self):
        req, worker = self._begin()
        sess = self.orch.session
        sess.mark_success(PATH_A, output_path="o")
        sess.mark_failed(PATH_B, "e")
        self._finish_with(worker, [
            FakeResult(PATH_A, True), FakeResult(PATH_B, False, error="e"),
        ])
        ok, err = self.orch.resume("retry_failed", self.proc)
        self.assertTrue(ok, err)
        req2 = self.created_workers[-1]["request"]
        self.assertEqual(req2.file_list(), [PATH_B])

    def test_resume_without_session_fails(self):
        ok, err = self.orch.resume("continue", self.proc)
        self.assertFalse(ok)
        self.assertIn("没有可续跑", err)

    def test_resume_no_pending_fails(self):
        req, worker = self._begin()
        sess = self.orch.session
        for p in (PATH_A, PATH_B, PATH_C):
            sess.mark_success(p, output_path="o")
        self._finish_with(worker, [FakeResult(p, True) for p in (PATH_A, PATH_B, PATH_C)])
        ok, err = self.orch.resume("continue", self.proc)
        self.assertFalse(ok)
        self.assertIn("没有未完成", err)

    # ── 会话维护 / 关闭 ──

    def test_clear_session(self):
        self._begin()
        w = self.created_workers[-1]["worker"]
        self._finish_with(w, [])
        self.orch.clear_session()
        self.assertIsNone(self.orch.session)

    def test_remove_session_paths(self):
        self._begin()
        w = self.created_workers[-1]["worker"]
        self._finish_with(w, [])
        self.orch.remove_session_paths([PATH_B])
        self.assertIsNone(self.orch.session.get(PATH_B))
        self.assertEqual(self.orch.session.count(), 2)

    def test_request_shutdown_idle(self):
        self.assertTrue(self.orch.request_shutdown())

    def test_request_shutdown_cancels_running_worker(self):
        self._begin()
        worker = self.created_workers[-1]["worker"]
        self.assertTrue(self.orch.request_shutdown(wait_ms=100))
        self.assertTrue(worker.cancelled)
        self.assertFalse(worker.isRunning())


if __name__ == "__main__":
    unittest.main()
