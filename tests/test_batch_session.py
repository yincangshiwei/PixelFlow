"""P-1 基线测试：BatchSession 状态、取消、续跑、失败重试与原序号保持。

固化 core/batch_session.py 的既有行为，P3 演进为 BatchSnapshot 时不得回归。
"""
import unittest

from core.batch_session import (
    BatchSession,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCESS,
)

ENTRIES = [
    (r"C:\img\a.png", None),
    (r"C:\img\sub\b.png", "sub/b.png"),
    (r"C:\img\c.jpg", None),
]
PATH_A, PATH_B, PATH_C = (p for p, _ in ENTRIES)


def make_session(**overrides) -> BatchSession:
    kwargs = dict(
        kind="image",
        processor_preset_id="basic_process",
        processor_name="基础处理",
        supports_resume=True,
        options={"enable_format": True, "_output_format": "jpg"},
        output_dir=r"C:\out",
        auto_subfolder=True,
        overwrite=False,
        keep_structure=True,
        path_mode_id=0,
        entries=list(ENTRIES),
    )
    kwargs.update(overrides)
    return BatchSession.create(**kwargs)


class TestBatchSessionCreate(unittest.TestCase):
    def test_orders_are_1_based_and_stable(self):
        sess = make_session()
        self.assertEqual([f.order for f in sess.files], [1, 2, 3])
        self.assertEqual(
            sess.order_map(), {PATH_A: 1, PATH_B: 2, PATH_C: 3}
        )

    def test_rel_path_map_only_keeps_relative_entries(self):
        sess = make_session()
        self.assertEqual(sess.rel_path_map, {PATH_B: "sub/b.png"})
        self.assertIsNone(sess.get(PATH_A).rel_path)
        self.assertEqual(sess.get(PATH_B).rel_path, "sub/b.png")

    def test_file_overwrite_defaults_to_overwrite_when_omitted(self):
        # 未显式传入时：原图覆盖模式等同允许覆盖文件
        self.assertTrue(make_session(overwrite=True).file_overwrite)
        self.assertFalse(make_session(overwrite=False).file_overwrite)
        # 显式传入时以显式值为准（桌面/自定义的「覆盖同名」独立于原图覆盖）
        self.assertTrue(
            make_session(overwrite=False, file_overwrite=True).file_overwrite
        )
        self.assertFalse(
            make_session(overwrite=True, file_overwrite=False).file_overwrite
        )

    def test_options_snapshot_is_copied(self):
        opts = {"enable_format": True}
        sess = make_session(options=opts)
        opts["mutated"] = True
        self.assertNotIn("mutated", sess.options)
        sess.options["mutated2"] = True
        self.assertNotIn("mutated2", opts)

    def test_initial_status_all_pending(self):
        sess = make_session()
        self.assertEqual(sess.count(STATUS_PENDING), 3)
        self.assertEqual(sess.summary()["total"], 3)


class TestBatchSessionTransitions(unittest.TestCase):
    def test_mark_running_from_pending(self):
        sess = make_session()
        sess.mark_running(PATH_A)
        self.assertEqual(sess.get(PATH_A).status, STATUS_RUNNING)

    def test_mark_running_does_not_override_terminal_states(self):
        sess = make_session()
        sess.mark_success(PATH_A, output_path=r"C:\out\a.jpg")
        sess.mark_running(PATH_A)
        self.assertEqual(sess.get(PATH_A).status, STATUS_SUCCESS)

        sess.mark_failed(PATH_B, error="boom")
        sess.mark_running(PATH_B)
        self.assertEqual(sess.get(PATH_B).status, STATUS_FAILED)

    def test_mark_success_clears_error_and_sets_output(self):
        sess = make_session()
        sess.mark_failed(PATH_A, error="old")
        sess.mark_success(PATH_A, output_path=r"C:\out\a.png")
        job = sess.get(PATH_A)
        self.assertEqual(job.status, STATUS_SUCCESS)
        self.assertEqual(job.error, "")
        self.assertEqual(job.output_path, r"C:\out\a.png")

    def test_mark_failed_clears_output(self):
        sess = make_session()
        sess.mark_success(PATH_A, output_path=r"C:\out\a.png")
        sess.mark_failed(PATH_A, error="decode error")
        job = sess.get(PATH_A)
        self.assertEqual(job.status, STATUS_FAILED)
        self.assertEqual(job.error, "decode error")
        self.assertEqual(job.output_path, "")

    def test_mark_unknown_path_is_noop(self):
        sess = make_session()
        sess.mark_success(r"C:\not\exist.png")
        sess.mark_failed(r"C:\not\exist.png")
        sess.mark_running(r"C:\not\exist.png")
        self.assertEqual(sess.count(), 3)

    def test_cancel_marks_running_items_cancelled(self):
        sess = make_session()
        sess.mark_running(PATH_A)
        sess.mark_success(PATH_B)
        sess.mark_cancelled_running()
        self.assertTrue(sess.user_cancelled)
        self.assertEqual(sess.get(PATH_A).status, STATUS_CANCELLED)
        self.assertEqual(sess.get(PATH_A).error, "用户取消")
        self.assertEqual(sess.get(PATH_B).status, STATUS_SUCCESS)
        self.assertEqual(sess.get(PATH_C).status, STATUS_PENDING)


class TestBatchSessionResumeRetry(unittest.TestCase):
    def test_pending_paths_include_cancelled(self):
        sess = make_session()
        sess.mark_running(PATH_A)
        sess.mark_success(PATH_B)
        sess.mark_cancelled_running()
        # 续跑 = 未跑完 + 取消中断的当前张
        self.assertEqual(sess.pending_paths(), [PATH_A, PATH_C])

    def test_failed_paths(self):
        sess = make_session()
        sess.mark_failed(PATH_B, "x")
        self.assertEqual(sess.failed_paths(), [PATH_B])

    def test_reset_for_retry_only_resets_given_paths(self):
        sess = make_session()
        sess.mark_failed(PATH_A, "e1")
        sess.mark_success(PATH_B, output_path="out")
        sess.reset_for_retry([PATH_A])
        self.assertEqual(sess.get(PATH_A).status, STATUS_PENDING)
        self.assertEqual(sess.get(PATH_A).error, "")
        self.assertEqual(sess.get(PATH_B).status, STATUS_SUCCESS)

    def test_resume_keeps_original_orders(self):
        """续跑/重试必须沿用原批次序号（重命名、_image_index 依赖）。"""
        sess = make_session()
        sess.mark_success(PATH_A)
        sess.mark_failed(PATH_B, "err")
        sess.mark_cancelled_running()
        orders_before = sess.order_map()
        sess.reset_for_retry(sess.failed_paths())
        self.assertEqual(sess.order_map(), orders_before)
        self.assertEqual(sess.order_map()[PATH_B], 2)

    def test_can_continue_requires_supports_resume(self):
        sess = make_session(supports_resume=False)
        self.assertTrue(sess.pending_paths())
        self.assertFalse(sess.can_continue())
        self.assertFalse(sess.can_retry_failed())

    def test_can_continue_with_resume_enabled(self):
        sess = make_session()
        self.assertTrue(sess.can_continue())
        self.assertFalse(sess.can_retry_failed())
        sess.mark_failed(PATH_C, "e")
        self.assertTrue(sess.can_retry_failed())

    def test_summary_counts(self):
        sess = make_session()
        sess.mark_success(PATH_A)
        sess.mark_failed(PATH_B, "e")
        s = sess.summary()
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["success"], 1)
        self.assertEqual(s["failed"], 1)
        self.assertEqual(s["pending"], 1)
        self.assertEqual(s["cancelled"], 0)


class TestBatchSessionRemovePaths(unittest.TestCase):
    """P3：列表移除条目后会话同步（remove_paths）。"""

    def test_remove_paths_drops_entries_and_index(self):
        sess = make_session()
        n = sess.remove_paths([PATH_B])
        self.assertEqual(n, 1)
        self.assertIsNone(sess.get(PATH_B))
        self.assertEqual([f.path for f in sess.files], [PATH_A, PATH_C])
        # 原序号保持（续跑依赖）
        self.assertEqual(sess.order_map(), {PATH_A: 1, PATH_C: 3})

    def test_remove_paths_unknown_is_noop(self):
        sess = make_session()
        self.assertEqual(sess.remove_paths([r"C:\nope.png"]), 0)
        self.assertEqual(sess.count(), 3)

    def test_remove_paths_empty(self):
        sess = make_session()
        self.assertEqual(sess.remove_paths([]), 0)
        self.assertEqual(sess.count(), 3)


if __name__ == "__main__":
    unittest.main()
