"""功能注册基线（P6）：注册入口统一为 services.features.catalog。

不再存在 @register_processor 装饰器注册表；功能的唯一权威注册是
FeatureDescriptor（catalog.build_feature_registry）。这里校验：
  1. 六个功能全部在册、preset_id 与处理器类一致；
  2. 菜单顺序稳定；
  3. id / preset_id 唯一；
  4. 处理器类元数据（name/description/icon/preset_id）完整；
  5. 批量合并 / 续跑 / 选中回读基线（来自 FeatureDescriptor）。
"""
import unittest

from services.contracts.feature_descriptor import InputKind
from services.features.catalog import EXPECTED_FEATURE_ORDER, build_feature_registry


class TestFeatureRegistry(unittest.TestCase):
    def setUp(self):
        self.reg, self.services = build_feature_registry()

    def test_all_features_registered(self):
        self.assertEqual(self.reg.ids(), EXPECTED_FEATURE_ORDER)
        self.assertEqual(len(self.reg.ids()), 6)
        self.assertIn("upscale", self.reg.ids())

    def test_preset_id_matches_processor(self):
        # FeatureDescriptor.id 即 preset_id（全局唯一）
        for desc in self.reg:
            proc = desc.create_processor()
            self.assertEqual(desc.id, proc.preset_id, desc.id)

    def test_ids_unique(self):
        ids = self.reg.ids()
        self.assertEqual(len(ids), len(set(ids)))

    def test_class_metadata(self):
        for desc in self.reg:
            proc = desc.create_processor()
            self.assertIsInstance(proc.name, str)
            self.assertGreater(len(proc.name), 0, desc.id)
            self.assertIsInstance(proc.description, str)
            self.assertIsInstance(proc.icon, str)
            self.assertIsInstance(proc.preset_id, str)

    def test_batch_merged_and_resume(self):
        for desc in self.reg:
            if desc.input_kind is InputKind.BATCH_MERGED:
                self.assertFalse(desc.supports_resume, desc.id)
            else:
                self.assertTrue(desc.supports_resume, desc.id)

    def test_selected_load_flag(self):
        md = self.reg.require("metadata_edit")
        self.assertTrue(md.supports_selected_load)
        basic = self.reg.require("basic_process")
        self.assertFalse(basic.supports_selected_load)

    def test_processors_pure_no_ui(self):
        """P6：处理器不暴露任何面板 UI 方法。"""
        ui_attrs = (
            "create_panel", "gather_options", "apply_options",
            "get_output_format", "on_selected_image",
        )
        for desc in self.reg:
            proc = desc.create_processor()
            for attr in ui_attrs:
                self.assertFalse(hasattr(proc, attr), f"{desc.id}.{attr}")


if __name__ == "__main__":
    unittest.main()
