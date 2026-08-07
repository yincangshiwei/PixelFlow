"""ImportCollection —— 无 QWidget 的文件列表状态模型测试。

覆盖：
- 增删 / 去重
- 相对路径保持（不依赖 item role 即可恢复）
- 无 UI 模型：增删、按范围生成输入快照
- 排序回写（reorder）与任务状态（批处理投影）语义
"""
import unittest

from core.batch_session import (
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCESS,
    STATUS_FAILED,
)
from services.common.file_list import ImportCollection, compute_display_name
from services.contracts.import_entry import ImportSource


class TestAddAndDedupe(unittest.TestCase):
    def test_add_files_basic(self):
        col = ImportCollection()
        added = col.add_files(["C:/img/a.png", "C:/img/b.jpg"])
        self.assertEqual(len(added), 2)
        self.assertEqual(len(col), 2)
        self.assertEqual(col.paths(), ["C:/img/a.png", "C:/img/b.jpg"])

    def test_dedupe_by_path(self):
        col = ImportCollection()
        col.add_files(["C:/img/a.png"])
        added = col.add_files(["C:/img/a.png", "C:/img/b.jpg"])
        self.assertEqual([e.path for e in added], ["C:/img/b.jpg"])
        self.assertEqual(len(col), 2)

    def test_dedupe_within_same_batch(self):
        col = ImportCollection()
        added = col.add_files(["C:/img/a.png", "C:/img/a.png"])
        self.assertEqual(len(added), 1)
        self.assertEqual(len(col), 1)

    def test_add_empty_path_ignored(self):
        col = ImportCollection()
        self.assertEqual(col.add_files(["", None]), [])
        self.assertIsNone(col.add(""))
        self.assertEqual(len(col), 0)

    def test_contains_and_lookup(self):
        col = ImportCollection()
        entry = col.add("C:/img/a.png")
        self.assertIn("C:/img/a.png", col)
        self.assertTrue(col.has_id(entry.entry_id))
        self.assertIs(col.entry_by_path("C:/img/a.png"), entry)
        self.assertIs(col.entry_by_id(entry.entry_id), entry)
        self.assertIsNone(col.entry_by_path("C:/img/none.png"))


class TestRelativePathAndDisplayName(unittest.TestCase):
    def test_folder_import_keeps_relative_path(self):
        col = ImportCollection()
        base = "C:/data/root"
        col.add_files(
            ["C:/data/root/sub/c.png", "C:/data/root/a.png"], base_dir=base
        )
        e_sub = col.entry_by_path("C:/data/root/sub/c.png")
        e_root = col.entry_by_path("C:/data/root/a.png")
        self.assertEqual(e_sub.relative_path, "sub/c.png")
        self.assertEqual(e_root.relative_path, "a.png")

    def test_single_file_has_no_relative_path(self):
        col = ImportCollection()
        col.add_files(["C:/anywhere/x.png"])
        self.assertIsNone(col.entry_by_path("C:/anywhere/x.png").relative_path)

    def test_display_name_rules(self):
        # 有 base_dir：相对路径
        self.assertEqual(
            compute_display_name("C:/r/sub/a.png", "C:/r"), "sub/a.png"
        )
        # 有 base_dir 但无法求相对路径：仅文件名
        self.assertEqual(
            compute_display_name("C:/other/a.png", "C:/r"), "a.png"
        )
        # 无 base_dir：父目录/文件名
        self.assertEqual(
            compute_display_name("C:/photos/a.png", None), "photos/a.png"
        )

    def test_display_name_stored_in_state(self):
        col = ImportCollection()
        col.add_files(["C:/r/sub/a.png"], base_dir="C:/r")
        col.add_files(["C:/photos/b.png"])
        self.assertEqual(col.display_name_of("C:/r/sub/a.png"), "sub/a.png")
        self.assertEqual(col.display_name_of("C:/photos/b.png"), "photos/b.png")


class TestRemoveAndClear(unittest.TestCase):
    def test_remove_paths(self):
        col = ImportCollection()
        col.add_files(["C:/img/a.png", "C:/img/b.jpg", "C:/img/c.webp"])
        removed = col.remove_paths(["C:/img/b.jpg", "C:/img/none.png"])
        self.assertEqual(removed, ["C:/img/b.jpg"])
        self.assertEqual(col.paths(), ["C:/img/a.png", "C:/img/c.webp"])
        self.assertNotIn("C:/img/b.jpg", col)

    def test_clear(self):
        col = ImportCollection()
        col.add_files(["C:/img/a.png", "C:/img/b.jpg"])
        col.clear()
        self.assertEqual(len(col), 0)
        self.assertEqual(col.paths(), [])
        # 清空后可重新添加同路径
        self.assertIsNotNone(col.add("C:/img/a.png"))


class TestReorder(unittest.TestCase):
    def test_reorder_matches_widget_sort(self):
        col = ImportCollection()
        entries = col.add_files(["C:/img/b.png", "C:/img/a.png"])
        ids = {e.entry_id: e.path for e in entries}
        # 模拟 sortItems 后的控件顺序（按显示名）
        order = sorted(entries, key=lambda e: col.display_name_of(e.path))
        col.reorder([e.entry_id for e in order])
        self.assertEqual(col.paths(), ["C:/img/a.png", "C:/img/b.png"])
        # 未知 id 被忽略
        col.reorder(["not-exist"] + [e.entry_id for e in order])
        self.assertEqual(col.paths(), ["C:/img/a.png", "C:/img/b.png"])


class TestSnapshot(unittest.TestCase):
    def setUp(self):
        self.col = ImportCollection()
        self.col.add_files(
            ["C:/r/sub/a.png", "C:/r/b.jpg"], base_dir="C:/r"
        )
        self.col.add_files(["C:/solo/c.webp"])

    def test_full_snapshot_keeps_order_and_rel(self):
        snap = self.col.snapshot()
        self.assertEqual(
            snap,
            [
                ("C:/r/sub/a.png", "sub/a.png"),
                ("C:/r/b.jpg", "b.jpg"),
                ("C:/solo/c.webp", None),
            ],
        )

    def test_subset_snapshot(self):
        snap = self.col.snapshot(["C:/solo/c.webp", "C:/r/sub/a.png"])
        # 子集按集合顺序输出
        self.assertEqual(
            snap, [("C:/r/sub/a.png", "sub/a.png"), ("C:/solo/c.webp", None)]
        )

    def test_subset_snapshot_ignores_unknown(self):
        self.assertEqual(self.col.snapshot(["C:/none.png"]), [])


class TestJobStatus(unittest.TestCase):
    def test_default_pending(self):
        col = ImportCollection()
        col.add("C:/img/a.png")
        self.assertEqual(col.job_status("C:/img/a.png"), STATUS_PENDING)

    def test_set_and_reset(self):
        col = ImportCollection()
        col.add("C:/img/a.png")
        col.add("C:/img/b.jpg")
        self.assertTrue(col.set_job_status("C:/img/a.png", STATUS_RUNNING))
        self.assertTrue(
            col.set_job_status("C:/img/b.jpg", STATUS_FAILED, error="boom\nx")
        )
        self.assertEqual(col.job_status("C:/img/a.png"), STATUS_RUNNING)
        st = col.state_by_path("C:/img/b.jpg")
        self.assertEqual(st.job_status, STATUS_FAILED)
        self.assertEqual(st.job_error, "boom\nx")

        col.reset_job_status()
        self.assertEqual(col.job_status("C:/img/a.png"), STATUS_PENDING)
        self.assertEqual(col.job_status("C:/img/b.jpg"), STATUS_PENDING)
        self.assertEqual(col.state_by_path("C:/img/b.jpg").job_error, "")

    def test_set_status_unknown_path(self):
        col = ImportCollection()
        self.assertFalse(col.set_job_status("C:/none.png", STATUS_SUCCESS))
        self.assertEqual(col.job_status("C:/none.png"), STATUS_PENDING)

    def test_remove_entry_drops_status(self):
        col = ImportCollection()
        col.add("C:/img/a.png")
        col.set_job_status("C:/img/a.png", STATUS_SUCCESS)
        col.remove_paths(["C:/img/a.png"])
        self.assertEqual(col.job_status("C:/img/a.png"), STATUS_PENDING)
        # 重新导入后状态回到 pending（新条目）
        col.add("C:/img/a.png")
        self.assertEqual(col.job_status("C:/img/a.png"), STATUS_PENDING)


class TestSource(unittest.TestCase):
    def test_source_recorded(self):
        col = ImportCollection()
        col.add_files(["C:/img/a.png"], source=ImportSource.DRAG_DROP)
        self.assertEqual(
            col.entry_by_path("C:/img/a.png").source, ImportSource.DRAG_DROP
        )


if __name__ == "__main__":
    unittest.main()
