"""P-1 基线测试：输出路径矩阵（四种模式 × 覆盖/保留结构）、命名与子目录解析。

固化两部分既有行为：
1. core/worker.py 的 _build_stem / resolve_file_out_dir / unique_out_path
2. MainWindow._resolve_output_dir / _resolve_file_overwrite / auto_folder /
   keep_structure 的语义（现由 services.contracts.OutputPolicy 承载）
"""
import tempfile
import unittest
from pathlib import Path

from core.worker import _build_stem, resolve_file_out_dir, unique_out_path
from services.contracts import (
    AUTO_SUBFOLDER_NAME,
    OutputPolicy,
    PathMode,
    default_desktop_path,
)

SRC_FILE = r"C:\photos\trip\IMG_001.png"


class TestBuildStem(unittest.TestCase):
    def test_rename_disabled_keeps_original_stem(self):
        opts = {"enable_rename": False}
        self.assertEqual(_build_stem("photo", opts, 5), "photo")

    def test_keep_mode_appends_sequence(self):
        opts = {"enable_rename": True, "prefix_mode": "keep",
                "digits": 3, "start_index": 1}
        self.assertEqual(_build_stem("photo", opts, 1), "photo_001")
        self.assertEqual(_build_stem("photo", opts, 12), "photo_012")

    def test_custom_prefix(self):
        opts = {"enable_rename": True, "prefix_mode": "custom",
                "prefix": "product", "digits": 4, "start_index": 1}
        self.assertEqual(_build_stem("photo", opts, 3), "product_0003")

    def test_custom_prefix_empty_uses_sequence_only(self):
        opts = {"enable_rename": True, "prefix_mode": "custom",
                "prefix": "  ", "digits": 2, "start_index": 1}
        self.assertEqual(_build_stem("photo", opts, 7), "07")

    def test_start_index_offsets_sequence(self):
        opts = {"enable_rename": True, "prefix_mode": "custom",
                "prefix": "p", "digits": 3, "start_index": 10}
        self.assertEqual(_build_stem("x", opts, 1), "p_010")

    def test_resume_order_is_passed_through(self):
        """续跑时 Worker 传入原批次 order，序号不重排。"""
        opts = {"enable_rename": True, "prefix_mode": "keep",
                "digits": 3, "start_index": 1}
        # file_index_map 命中后 order=42（模拟续跑第 42 张）
        self.assertEqual(_build_stem("photo", opts, 42), "photo_042")


class TestResolveFileOutDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name) / "out"

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_rel_map_uses_base(self):
        d = resolve_file_out_dir(self.base, r"C:\x\a.png", None)
        self.assertEqual(d, self.base)
        self.assertTrue(self.base.is_dir())

    def test_rel_path_builds_subdir(self):
        d = resolve_file_out_dir(self.base, r"C:\x\a.png",
                                 {r"C:\x\a.png": "sub/dir/a.png"})
        self.assertEqual(d, self.base / "sub" / "dir")
        self.assertTrue(d.is_dir())

    def test_rel_filename_only_stays_in_base(self):
        d = resolve_file_out_dir(self.base, r"C:\x\a.png",
                                 {r"C:\x\a.png": "a.png"})
        self.assertEqual(d, self.base)

    def test_windows_backslash_rel_path(self):
        d = resolve_file_out_dir(self.base, r"C:\x\a.png",
                                 {r"C:\x\a.png": "sub\\a.png"})
        self.assertEqual(d, self.base / "sub")

    def test_parent_traversal_is_stripped(self):
        d = resolve_file_out_dir(self.base, r"C:\x\a.png",
                                 {r"C:\x\a.png": "../../evil/a.png"})
        self.assertEqual(d, self.base / "evil")

    def test_absolute_rel_is_stripped_to_relative_parts(self):
        d = resolve_file_out_dir(self.base, r"C:\x\a.png",
                                 {r"C:\x\a.png": "C:/abs/sub/a.png"})
        self.assertEqual(d, self.base / "abs" / "sub")

    def test_unknown_path_falls_back_to_base(self):
        d = resolve_file_out_dir(self.base, r"C:\x\other.png",
                                 {r"C:\x\a.png": "sub/a.png"})
        self.assertEqual(d, self.base)


class TestUniqueOutPath(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_overwrite_on_returns_exact_path_even_if_exists(self):
        (self.dir / "a.png").write_bytes(b"x")
        p = unique_out_path(self.dir, "a", ".png", file_overwrite=True)
        self.assertEqual(p, self.dir / "a.png")

    def test_overwrite_off_appends_counter(self):
        (self.dir / "a.png").write_bytes(b"x")
        (self.dir / "a_1.png").write_bytes(b"x")
        p = unique_out_path(self.dir, "a", ".png", file_overwrite=False)
        self.assertEqual(p, self.dir / "a_2.png")

    def test_overwrite_off_no_conflict_keeps_name(self):
        p = unique_out_path(self.dir, "a", ".png", file_overwrite=False)
        self.assertEqual(p, self.dir / "a.png")


class TestOutputPolicyModes(unittest.TestCase):
    """四种输出模式 × 选项的矩阵（与现有 MainWindow 行为逐条对齐）。"""

    def test_desktop_mode_with_root(self):
        pol = OutputPolicy(path_mode=PathMode.DESKTOP, root_dir=r"D:\out")
        self.assertEqual(pol.resolve_output_dir(SRC_FILE), r"D:\out")
        self.assertFalse(pol.src_overwrite)

    def test_desktop_mode_empty_root_falls_back_to_desktop(self):
        pol = OutputPolicy(path_mode=PathMode.DESKTOP, root_dir="  ")
        self.assertEqual(pol.resolve_output_dir(SRC_FILE), default_desktop_path())

    def test_custom_mode(self):
        pol = OutputPolicy(path_mode=PathMode.CUSTOM, root_dir=r"E:\target")
        self.assertEqual(pol.resolve_output_dir(SRC_FILE), r"E:\target")
        self.assertFalse(pol.src_overwrite)

    def test_custom_mode_empty_root_falls_back_to_desktop(self):
        pol = OutputPolicy(path_mode=PathMode.CUSTOM, root_dir="")
        self.assertEqual(pol.resolve_output_dir(SRC_FILE), default_desktop_path())

    def test_src_overwrite_mode(self):
        pol = OutputPolicy(path_mode=PathMode.SRC_OVERWRITE)
        self.assertEqual(pol.resolve_output_dir(SRC_FILE), r"C:\photos\trip")
        self.assertTrue(pol.src_overwrite)
        # 原图覆盖：file_overwrite 恒 True、不建子文件夹、不保留结构
        self.assertTrue(pol.resolve_file_overwrite())
        self.assertFalse(pol.effective_auto_subfolder())
        self.assertFalse(pol.effective_keep_structure())
        self.assertEqual(pol.build_rel_path_map([(SRC_FILE, "a.png")]), {})

    def test_src_copy_mode(self):
        pol = OutputPolicy(path_mode=PathMode.SRC_COPY)
        self.assertEqual(pol.resolve_output_dir(SRC_FILE), r"C:\photos\trip")
        self.assertFalse(pol.src_overwrite)
        # 副本模式：同名不覆盖（_1/_2…），不保留结构（mode 不在 0/1）
        self.assertFalse(pol.resolve_file_overwrite())
        self.assertFalse(pol.effective_keep_structure())
        self.assertEqual(pol.build_rel_path_map([(SRC_FILE, "a.png")]), {})

    def test_src_modes_require_src_path(self):
        for mode in (PathMode.SRC_OVERWRITE, PathMode.SRC_COPY):
            pol = OutputPolicy(path_mode=mode)
            with self.assertRaises(ValueError):
                pol.resolve_output_dir("")

    def test_desktop_file_overwrite_follows_checkbox(self):
        self.assertTrue(
            OutputPolicy(path_mode=PathMode.DESKTOP, file_overwrite=True)
            .resolve_file_overwrite()
        )
        self.assertFalse(
            OutputPolicy(path_mode=PathMode.DESKTOP, file_overwrite=False)
            .resolve_file_overwrite()
        )

    def test_auto_subfolder_effective_only_outside_src_overwrite(self):
        pol = OutputPolicy(path_mode=PathMode.DESKTOP, auto_subfolder=True)
        self.assertTrue(pol.effective_auto_subfolder())
        pol2 = OutputPolicy(path_mode=PathMode.SRC_OVERWRITE, auto_subfolder=True)
        self.assertFalse(pol2.effective_auto_subfolder())
        # 副本模式：勾选框隐藏但取值保留（现状语义）
        pol3 = OutputPolicy(path_mode=PathMode.SRC_COPY, auto_subfolder=True)
        self.assertTrue(pol3.effective_auto_subfolder())

    def test_keep_structure_only_in_desktop_custom(self):
        entries = [(SRC_FILE, "trip/IMG_001.png")]
        for mode, expect in (
            (PathMode.DESKTOP, True),
            (PathMode.CUSTOM, True),
            (PathMode.SRC_OVERWRITE, False),
            (PathMode.SRC_COPY, False),
        ):
            pol = OutputPolicy(path_mode=mode, keep_structure=True)
            self.assertEqual(pol.effective_keep_structure(), expect, mode)
            rel_map = pol.build_rel_path_map(entries)
            if expect:
                self.assertEqual(rel_map, {SRC_FILE: "trip/IMG_001.png"})
            else:
                self.assertEqual(rel_map, {})

    def test_plan_output_root_applies_auto_subfolder(self):
        pol = OutputPolicy(path_mode=PathMode.CUSTOM, auto_subfolder=True)
        self.assertEqual(
            pol.plan_output_root(r"E:\target"),
            Path(r"E:\target") / AUTO_SUBFOLDER_NAME,
        )
        pol2 = OutputPolicy(path_mode=PathMode.CUSTOM, auto_subfolder=False)
        self.assertEqual(pol2.plan_output_root(r"E:\target"), Path(r"E:\target"))

    def test_path_mode_accepts_legacy_int(self):
        pol = OutputPolicy(path_mode=2)
        self.assertIs(pol.path_mode, PathMode.SRC_OVERWRITE)


if __name__ == "__main__":
    unittest.main()
