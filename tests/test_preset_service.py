"""P2：PresetService 门面（基于临时目录）。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import core.preset_manager as pm
from services.common.preset_service import (
    PresetConflictAction,
    PresetService,
)
from services.features.basic_service import BasicService


class TestPresetService(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._patcher = mock.patch.object(pm, "PRESETS_DIR", self.root)
        self._patcher.start()
        self.basic = BasicService()
        self.svc = PresetService(
            service_resolver=lambda fid: self.basic if fid == "basic_process" else None
        )

    def tearDown(self):
        self._patcher.stop()
        self._tmp.cleanup()

    def test_ensure_and_load_default(self):
        self.svc.ensure_default("basic_process")
        r = self.svc.load_default("basic_process")
        self.assertTrue(r.ok)
        self.assertFalse(r.data["enable_compress"])

    def test_save_load_normalize(self):
        r = self.svc.save_preset(
            "basic_process",
            "电商",
            {"enable_compress": True, "output_format": "png", "quality": 70},
        )
        self.assertTrue(r.ok)
        # 压缩联动：format 强制 + png→jpg
        self.assertTrue(r.data["enable_format"])
        self.assertEqual(r.data["output_format"], "jpg")
        loaded = self.svc.load_preset("basic_process", "电商")
        self.assertEqual(loaded.data["quality"], 70)

    def test_delete_default_refused(self):
        self.svc.ensure_default("basic_process")
        r = self.svc.delete_preset("basic_process", "default")
        self.assertFalse(r.ok)

    def test_restore_factory(self):
        self.svc.ensure_default("basic_process", {"enable_dpi": True, "dpi": 72})
        r = self.svc.restore_factory_default("basic_process")
        self.assertTrue(r.ok)
        self.assertEqual(r.data["dpi"], 300)
        self.assertFalse(r.data["enable_dpi"])

    def test_import_external(self):
        ext = self.root / "ext.json"
        ext.write_text(
            json.dumps({"enable_format": True, "output_format": "webp"}),
            encoding="utf-8",
        )
        plan, err = self.svc.plan_import("basic_process", ext)
        self.assertIsNone(err)
        self.assertEqual(plan.suggested_name, "ext")
        self.assertFalse(plan.needs_conflict_resolution)
        r = self.svc.commit_import("basic_process", plan)
        self.assertTrue(r.ok)
        self.assertIn("ext", self.svc.list_user_presets("basic_process"))

    def test_import_conflict_overwrite(self):
        self.svc.save_preset("basic_process", "ext", {"quality": 10})
        ext = self.root / "ext.json"
        ext.write_text(json.dumps({"quality": 99}), encoding="utf-8")
        plan, _ = self.svc.plan_import("basic_process", ext)
        self.assertTrue(plan.needs_conflict_resolution)
        r = self.svc.commit_import(
            "basic_process", plan, action=PresetConflictAction.OVERWRITE
        )
        self.assertTrue(r.ok)
        self.assertEqual(self.svc.load_preset("basic_process", "ext").data["quality"], 99)


if __name__ == "__main__":
    unittest.main()
