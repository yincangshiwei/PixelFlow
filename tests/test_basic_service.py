"""P2：BasicService 校验 / 联动 / 运行参数（无 QWidget）。"""
import unittest

from services.features.basic_service import (
    BasicService,
    default_basic_options,
    output_format_from_options,
)
from core.processors.basic_processor import BasicProcessor


class TestBasicServiceDefaults(unittest.TestCase):
    def test_default_matches_processor(self):
        svc = BasicService()
        self.assertEqual(svc.default_state(), BasicProcessor().default_options())
        self.assertEqual(svc.default_state(), default_basic_options())


class TestBasicServiceNormalize(unittest.TestCase):
    def setUp(self):
        self.svc = BasicService()

    def test_compress_forces_format_and_jpg_webp(self):
        raw = {
            "enable_compress": True,
            "compress_mode": "quality",
            "quality": 80,
            "enable_format": False,
            "output_format": "png",
        }
        r = self.svc.validate_and_normalize(raw)
        self.assertTrue(r.ok)
        self.assertTrue(r.value["enable_format"])
        self.assertEqual(r.value["output_format"], "jpg")

    def test_size_mode_keeps_webp(self):
        raw = {
            "enable_compress": True,
            "compress_mode": "size",
            "target_size_kb": 100,
            "output_format": "webp",
        }
        r = self.svc.validate_and_normalize(raw, strict=False)
        self.assertTrue(r.ok)
        self.assertEqual(r.value["output_format"], "webp")
        self.assertTrue(r.value["enable_format"])

    def test_quality_clamped(self):
        r = self.svc.validate_and_normalize({"quality": 999}, strict=True)
        self.assertFalse(r.ok)
        r2 = self.svc.validate_and_normalize({"quality": 999}, strict=False)
        self.assertTrue(r2.ok)
        self.assertEqual(r2.value["quality"], 100)

    def test_jpeg_alias(self):
        r = self.svc.validate_and_normalize(
            {"enable_format": True, "output_format": "jpeg"}, strict=False
        )
        self.assertEqual(r.value["output_format"], "jpg")

    def test_preset_normalize_merges_defaults(self):
        data = self.svc.normalize_preset({"enable_dpi": True, "dpi": 150})
        self.assertTrue(data["enable_dpi"])
        self.assertEqual(data["dpi"], 150)
        self.assertIn("enable_compress", data)
        self.assertFalse(data["enable_compress"])

    def test_build_run_options_output_format(self):
        opts = self.svc.build_run_options({
            "enable_format": True,
            "output_format": "png",
        })
        self.assertEqual(opts["_output_format"], "png")
        opts2 = self.svc.build_run_options({"enable_format": False})
        self.assertEqual(opts2["_output_format"], "")

    def test_output_format_helper(self):
        self.assertEqual(
            output_format_from_options({"enable_format": True, "output_format": "WEBP"}),
            "webp",
        )
        self.assertEqual(output_format_from_options({"enable_format": False}), "")


class TestBasicProcessorPure(unittest.TestCase):
    def test_no_ui_api(self):
        """P6：处理器不再暴露任何面板 UI 方法。"""
        p = BasicProcessor()
        for attr in (
            "create_panel", "gather_options", "apply_options",
            "get_output_format", "on_selected_image",
        ):
            self.assertFalse(hasattr(p, attr), attr)

    def test_process_jpg_flattens_rgba(self):
        from PIL import Image
        img = Image.new("RGBA", (4, 4), (255, 0, 0, 128))
        p = BasicProcessor()
        out, details = p.process(img, {"enable_format": True, "output_format": "jpg"})
        self.assertEqual(out.mode, "RGB")
        self.assertIn("mode_converted", details)

    def test_process_no_format_keeps_mode(self):
        from PIL import Image
        img = Image.new("RGBA", (2, 2), (0, 0, 0, 0))
        p = BasicProcessor()
        out, details = p.process(img, {"enable_format": False})
        self.assertEqual(out.mode, "RGBA")
        self.assertEqual(details["output_format"], "原格式")


if __name__ == "__main__":
    unittest.main()
