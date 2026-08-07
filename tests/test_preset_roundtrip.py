"""P-1 基线测试：PresetManager 预设往返（保存/加载/默认/删除/重命名）。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import core.preset_manager as pm
from core.preset_manager import PresetManager


class PresetRoundtripTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._patcher = mock.patch.object(pm, "PRESETS_DIR", self.root)
        self._patcher.start()
        self.mgr = PresetManager("test_feature")

    def tearDown(self):
        self._patcher.stop()
        self._tmp.cleanup()

    def test_dir_created_under_presets_root(self):
        self.assertEqual(self.mgr.preset_dir, self.root / "test_feature")
        self.assertTrue(self.mgr.preset_dir.is_dir())

    def test_save_load_roundtrip(self):
        data = {"enable_format": True, "quality": 85, "名称": "中文预设"}
        self.mgr.save_preset("我的预设", data)
        self.assertEqual(self.mgr.load_preset("我的预设"), data)

    def test_default_lifecycle(self):
        self.assertFalse(self.mgr.has_default())
        self.mgr.ensure_default({"a": 1})
        self.assertTrue(self.mgr.has_default())
        self.assertEqual(self.mgr.load_default(), {"a": 1})
        # ensure_default 不覆盖已有 default
        self.mgr.ensure_default({"a": 2})
        self.assertEqual(self.mgr.load_default(), {"a": 1})
        # save_default 强制覆盖（恢复默认用）
        self.mgr.save_default({"a": 3})
        self.assertEqual(self.mgr.load_default(), {"a": 3})

    def test_list_presets_sorted_with_default(self):
        self.mgr.save_default({})
        self.mgr.save_preset("b预设", {})
        self.mgr.save_preset("a预设", {})
        self.assertEqual(self.mgr.list_presets(), ["a预设", "b预设", "default"])
        self.assertEqual(self.mgr.list_user_presets(), ["a预设", "b预设"])

    def test_delete_default_refused(self):
        self.mgr.save_default({})
        self.assertFalse(self.mgr.delete_preset("default"))
        self.assertTrue(self.mgr.has_default())

    def test_delete_user_preset(self):
        self.mgr.save_preset("tmp", {"x": 1})
        self.assertTrue(self.mgr.delete_preset("tmp"))
        self.assertIsNone(self.mgr.load_preset("tmp"))
        self.assertFalse(self.mgr.delete_preset("tmp"))

    def test_rename_rules(self):
        self.mgr.save_preset("old", {"v": 1})
        self.assertTrue(self.mgr.rename_preset("old", "new"))
        self.assertEqual(self.mgr.load_preset("new"), {"v": 1})
        self.assertIsNone(self.mgr.load_preset("old"))
        # default 不可参与重命名；目标已存在时拒绝
        self.mgr.save_preset("other", {})
        self.assertFalse(self.mgr.rename_preset("default", "x"))
        self.assertFalse(self.mgr.rename_preset("new", "default"))
        self.assertFalse(self.mgr.rename_preset("new", "other"))

    def test_load_missing_returns_none(self):
        self.assertIsNone(self.mgr.load_preset("不存在"))

    def test_load_corrupted_json_returns_none(self):
        p = self.mgr.preset_dir / "bad.json"
        p.write_text("{not json", encoding="utf-8")
        self.assertIsNone(self.mgr.load_preset("bad"))

    def test_preset_file_is_utf8_json(self):
        self.mgr.save_preset("中文", {"标题": "电商白底图"})
        raw = (self.mgr.preset_dir / "中文.json").read_text(encoding="utf-8")
        self.assertIn("电商白底图", raw)
        self.assertEqual(json.loads(raw), {"标题": "电商白底图"})


if __name__ == "__main__":
    unittest.main()
