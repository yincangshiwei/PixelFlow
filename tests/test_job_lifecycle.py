"""P-1 契约测试：DTO 纯度、任务状态机、job_id 迟到事件隔离。

对应验收项：
- P-1-1 contracts 无 QApplication 可导入、不携带 Qt UI 对象
- P-1-2 FeatureDescriptor 覆盖 image / file / batch_merged 且 id 唯一
- P-1-6 job_id、状态机、关闭窗口与迟到事件规则进入代码接口
"""
import unittest

from services.contracts import (
    FeatureDescriptor,
    ImportEntry,
    ImportSource,
    InputKind,
    InvalidJobTransition,
    JobEvent,
    JobEventKind,
    JobResult,
    JobState,
    JobStateMachine,
    OutputPolicy,
    PathMode,
    RunRequest,
    new_entry_id,
    new_job_id,
)


class TestContractsPurity(unittest.TestCase):
    def test_no_qt_modules_loaded_by_contracts(self):
        """contracts 自身不得引入 Qt（本测试进程未创建 QApplication，
        若 contracts 依赖 Qt，导入即失败）。"""
        import services.contracts as c

        for name in ("QWidget", "QListWidgetItem", "QModelIndex"):
            self.assertFalse(hasattr(c, name))

    def test_import_entry_is_immutable_and_validated(self):
        e = ImportEntry(path=r"C:\img\a.png")
        with self.assertRaises(AttributeError):
            e.path = r"C:\other.png"
        with self.assertRaises(ValueError):
            ImportEntry(path="")

    def test_import_entry_dedupe_key_is_path(self):
        e = ImportEntry(path=r"C:\img\a.png")
        self.assertEqual(e.dedupe_key, r"C:\img\a.png")

    def test_import_entry_entry_ids_unique(self):
        ids = {new_entry_id() for _ in range(200)}
        self.assertEqual(len(ids), 200)

    def test_import_entry_relative_path_normalized(self):
        e = ImportEntry(path=r"C:\img\a.png", relative_path="sub\\a.png")
        self.assertEqual(e.relative_path, "sub/a.png")
        e2 = ImportEntry(path=r"C:\img\a.png", relative_path="a.png")
        self.assertEqual(e2.relative_path, "a.png")

    def test_job_ids_unique(self):
        ids = {new_job_id() for _ in range(200)}
        self.assertEqual(len(ids), 200)


class TestFeatureDescriptor(unittest.TestCase):
    def test_image_file_batch_kinds_all_describable(self):
        d_img = FeatureDescriptor(
            id="basic_process", name="基础处理", input_kind=InputKind.IMAGE
        )
        d_file = FeatureDescriptor(
            id="doc_convert", name="文档转换", input_kind=InputKind.FILE,
            supports_resume=True,
        )
        d_batch = FeatureDescriptor(
            id="img2doc", name="图片排版导出",
            input_kind=InputKind.BATCH_MERGED, supports_resume=False,
        )
        self.assertEqual(d_img.input_kind, InputKind.IMAGE)
        self.assertEqual(d_file.input_kind, InputKind.FILE)
        self.assertEqual(d_batch.input_kind, InputKind.BATCH_MERGED)

    def test_batch_merged_cannot_support_resume(self):
        with self.assertRaises(ValueError):
            FeatureDescriptor(
                id="x", name="X",
                input_kind=InputKind.BATCH_MERGED, supports_resume=True,
            )

    def test_id_and_name_required(self):
        with self.assertRaises(ValueError):
            FeatureDescriptor(id="", name="X")
        with self.assertRaises(ValueError):
            FeatureDescriptor(id="x", name="  ")

    def test_ids_unique_across_current_features(self):
        """现有六功能 + 描述符 id 唯一性（P2 注册表的基线约束）。"""
        feats = [
            FeatureDescriptor(id="transparent_image", name="透明图处理",
                              input_kind=InputKind.IMAGE),
            FeatureDescriptor(id="basic_process", name="基础处理",
                              input_kind=InputKind.IMAGE),
            FeatureDescriptor(id="upscale", name="高清放大",
                              input_kind=InputKind.IMAGE),
            FeatureDescriptor(id="img2doc", name="图片排版导出",
                              input_kind=InputKind.BATCH_MERGED,
                              supports_resume=False),
            FeatureDescriptor(id="image_overlay", name="图片叠加",
                              input_kind=InputKind.IMAGE),
            FeatureDescriptor(id="metadata_edit", name="元数据编辑",
                              input_kind=InputKind.BATCH_MERGED,
                              supports_resume=False,
                              supports_selected_load=True),
        ]
        ids = [f.id for f in feats]
        self.assertEqual(len(ids), len(set(ids)))

    def test_processor_factory(self):
        calls = []

        def factory():
            calls.append(1)
            return object()

        d = FeatureDescriptor(id="x", name="X", processor_factory=factory)
        d.create_processor()
        self.assertEqual(len(calls), 1)

        d2 = FeatureDescriptor(id="y", name="Y")
        with self.assertRaises(RuntimeError):
            d2.create_processor()


class TestJobStateMachine(unittest.TestCase):
    def test_happy_path(self):
        sm = JobStateMachine("j1")
        self.assertIs(sm.state, JobState.IDLE)
        sm.transition(JobState.STARTING)
        sm.transition(JobState.RUNNING)
        sm.transition(JobState.FINISHED)
        self.assertTrue(sm.is_terminal)
        self.assertFalse(sm.is_active)

    def test_cancel_path(self):
        sm = JobStateMachine("j1")
        sm.transition(JobState.STARTING)
        sm.transition(JobState.RUNNING)
        self.assertTrue(sm.request_cancel())
        self.assertIs(sm.state, JobState.CANCELLING)
        self.assertTrue(sm.user_cancelled)
        self.assertTrue(sm.is_active)  # 取消请求 ≠ 结束
        sm.transition(JobState.FINISHED)
        self.assertTrue(sm.is_terminal)

    def test_starting_can_fail(self):
        sm = JobStateMachine("j1")
        sm.transition(JobState.STARTING)
        sm.transition(JobState.FAILED)
        self.assertTrue(sm.is_terminal)

    def test_invalid_transitions_raise(self):
        sm = JobStateMachine("j1")
        with self.assertRaises(InvalidJobTransition):
            sm.transition(JobState.RUNNING)  # IDLE → RUNNING 非法
        sm.transition(JobState.STARTING)
        sm.transition(JobState.RUNNING)
        sm.transition(JobState.FINISHED)
        with self.assertRaises(InvalidJobTransition):
            sm.transition(JobState.RUNNING)  # 终态不可复活

    def test_request_cancel_only_from_running(self):
        sm = JobStateMachine("j1")
        self.assertFalse(sm.request_cancel())  # IDLE
        sm.transition(JobState.STARTING)
        self.assertFalse(sm.request_cancel())  # STARTING
        sm.transition(JobState.RUNNING)
        self.assertTrue(sm.request_cancel())
        self.assertFalse(sm.request_cancel())  # CANCELLING 重复请求

    def test_empty_job_id_rejected(self):
        with self.assertRaises(ValueError):
            JobStateMachine("")


class TestJobEvent(unittest.TestCase):
    def test_late_event_isolation(self):
        active = "job-2"
        late = JobEvent(job_id="job-1", kind=JobEventKind.PROGRESS, current=5, total=10)
        current = JobEvent(job_id="job-2", kind=JobEventKind.PROGRESS, current=1, total=3)
        self.assertFalse(late.is_for(active))
        self.assertTrue(current.is_for(active))
        self.assertFalse(current.is_for(None))

    def test_finished_event_carries_result(self):
        res = JobResult(job_id="j1", user_cancelled=True, total=3,
                        success=1, failed=1, cancelled=1)
        ev = JobEvent(job_id="j1", kind=JobEventKind.FINISHED, payload=res)
        self.assertTrue(ev.payload.user_cancelled)
        self.assertEqual(ev.payload.total, 3)


class TestRunRequest(unittest.TestCase):
    def _make(self, **overrides):
        kwargs = dict(
            job_id=new_job_id(),
            feature_id="basic_process",
            kind="image",
            entries=((r"C:\a.png", None), (r"C:\sub\b.png", "sub/b.png")),
            options={"enable_format": True},
            output=OutputPolicy(path_mode=PathMode.CUSTOM, root_dir=r"D:\out"),
        )
        kwargs.update(overrides)
        return RunRequest(**kwargs)

    def test_file_list_and_snapshot(self):
        req = self._make()
        self.assertEqual(req.file_list(), [r"C:\a.png", r"C:\sub\b.png"])
        self.assertIsInstance(req.entries, tuple)

    def test_entries_coerced_to_tuple(self):
        req = self._make(entries=[(r"C:\a.png", None)])
        self.assertIsInstance(req.entries, tuple)

    def test_order_map_semantics(self):
        """续跑序号：order_map 命中沿用原序号，否则按位置。"""
        req = self._make(order_map={r"C:\sub\b.png": 9})
        self.assertEqual(req.order_for(r"C:\sub\b.png", fallback_index=2), 9)
        self.assertEqual(req.order_for(r"C:\a.png", fallback_index=1), 1)

    def test_validation(self):
        with self.assertRaises(ValueError):
            self._make(job_id="")
        with self.assertRaises(ValueError):
            self._make(feature_id="")
        with self.assertRaises(ValueError):
            self._make(kind="doc")

    def test_request_is_frozen(self):
        req = self._make()
        with self.assertRaises(AttributeError):
            req.job_id = "other"


if __name__ == "__main__":
    unittest.main()
