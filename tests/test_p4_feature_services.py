"""P4：各 FeatureService 校验 / 默认值 / Processor 纯化（无 QWidget）。"""
import unittest

from services.features.metadata_service import MetadataService, default_metadata_options
from services.features.overlay_service import OverlayService, default_overlay_options
from services.features.img2doc_service import Img2DocService, default_img2doc_options
from services.features.transparent_service import (
    TransparentService,
    default_transparent_options,
)
from core.processors.metadata_processor import MetadataProcessor
from core.processors.overlay_processor import OverlayProcessor
from core.processors.img2doc_processor import Img2DocProcessor
from core.processors.transparent_processor import TransparentImageProcessor
from core.image_processor import place_subject_on_canvas


class TestMetadataService(unittest.TestCase):
    def setUp(self):
        self.svc = MetadataService()

    def test_default_matches_processor(self):
        self.assertEqual(self.svc.default_state(), MetadataProcessor().default_options())
        self.assertEqual(self.svc.default_state(), default_metadata_options())
        self.assertTrue(self.svc.default_state()["enable_convert"])
        self.assertEqual(self.svc.default_state()["target_format"], "jpg")

    def test_normalize_fields(self):
        data = self.svc.normalize_preset({
            "enable_convert": True,
            "target_format": "JPEG",
            "fields": {
                "title": {"enabled": True, "source": "fixed", "value": "T"},
            },
        })
        self.assertTrue(data["enable_convert"])
        self.assertEqual(data["target_format"], "jpg")
        self.assertTrue(data["fields"]["title"]["enabled"])
        self.assertIn("keywords", data["fields"])

    def test_build_run_options(self):
        opts = self.svc.build_run_options(self.svc.default_state())
        self.assertEqual(opts["_output_format"], "")
        self.assertFalse(opts["keep_matting"])

    def test_processor_no_ui_api(self):
        for attr in ("create_panel", "gather_options", "apply_options"):
            self.assertFalse(hasattr(MetadataProcessor(), attr), attr)

    def test_load_selected_missing(self):
        r = self.svc.load_selected(None)
        self.assertFalse(r["_load_ok"])


class TestOverlayService(unittest.TestCase):
    def setUp(self):
        self.svc = OverlayService()

    def test_default(self):
        self.assertEqual(self.svc.default_state(), default_overlay_options())
        self.assertEqual(self.svc.default_state(), OverlayProcessor().default_options())

    def test_format_and_elements(self):
        r = self.svc.validate_and_normalize({
            "output_format": "JPEG",
            "elements": [
                {"type": "text", "source": "fixed", "content": "hi", "font_size": 30},
                {"type": "image", "width": 10, "height": 20, "x": 1, "y": 2},
            ],
        })
        self.assertTrue(r.ok)
        self.assertEqual(r.value["output_format"], "jpg")
        self.assertEqual(len(r.value["elements"]), 2)

    def test_build_run_options(self):
        opts = self.svc.build_run_options({"output_format": "png"})
        self.assertEqual(opts["_output_format"], "png")

    def test_processor_no_ui_api(self):
        for attr in ("create_panel", "gather_options", "apply_options"):
            self.assertFalse(hasattr(OverlayProcessor(), attr), attr)

    def test_process_empty_elements(self):
        from PIL import Image
        img = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        out, details = OverlayProcessor().process(img, {"elements": [], "custom_fonts": {}})
        self.assertEqual(out.size, (8, 8))
        self.assertIn("summary", details)


class TestImg2DocService(unittest.TestCase):
    def setUp(self):
        self.svc = Img2DocService()

    def test_default(self):
        self.assertEqual(self.svc.default_state(), default_img2doc_options())

    def test_format_clamp(self):
        r = self.svc.validate_and_normalize({
            "format": "pdf",
            "width_cm": 10,
            "count_per_page": 4,
        }, strict=False)
        self.assertTrue(r.ok)
        self.assertEqual(r.value["format"], "PDF")
        self.assertEqual(r.value["count_per_page"], 4)

    def test_build_run_options(self):
        opts = self.svc.build_run_options(self.svc.default_state())
        self.assertEqual(opts["_output_format"], "")

    def test_processor_no_ui_api(self):
        for attr in ("create_panel", "gather_options", "apply_options"):
            self.assertFalse(hasattr(Img2DocProcessor(), attr), attr)


class TestTransparentService(unittest.TestCase):
    def setUp(self):
        self.svc = TransparentService()

    def test_default(self):
        self.assertEqual(self.svc.default_state(), default_transparent_options())
        self.assertEqual(
            self.svc.default_state(),
            TransparentImageProcessor().default_options(),
        )

    def test_legacy_canvas_migration(self):
        data = self.svc.normalize_preset({
            "enable_canvas": True,
            "canvas_w": 1000,
            "canvas_h": 800,
            "enable_resize": True,
            "resize_w": 500,
            "resize_h": 400,
        })
        self.assertTrue(data["enable_layout"])
        self.assertEqual(data["canvas_w"], 1000)
        self.assertEqual(data["subject_percent"], 50)

    def test_keep_matting_requires_enable(self):
        r = self.svc.validate_and_normalize({
            "enable_matting": False,
            "keep_matting": True,
        }, strict=False)
        self.assertFalse(r.value["keep_matting"])

    def test_reserve_and_position_normalize(self):
        r = self.svc.validate_and_normalize({
            "reserve_left_percent": 10,
            "reserve_right_percent": 20,
            "reserve_top_percent": 5,
            "reserve_bottom_percent": 25,
            "subject_position": "bottom_left",
        })
        self.assertTrue(r.ok)
        self.assertEqual(r.value["reserve_bottom_percent"], 25)
        self.assertEqual(r.value["subject_position"], "bottom_left")

    def test_invalid_reserve_sum(self):
        r = self.svc.validate_and_normalize({
            "reserve_left_percent": 60,
            "reserve_right_percent": 40,
        })
        self.assertFalse(r.ok)

    def test_build_run_options(self):
        opts = self.svc.build_run_options({
            "enable_matting": True,
            "output_format": "webp",
            "keep_matting": True,
        })
        self.assertEqual(opts["_output_format"], "webp")
        self.assertTrue(opts["keep_matting"])

    def test_processor_no_ui_api(self):
        for attr in ("create_panel", "gather_options", "apply_options"):
            self.assertFalse(hasattr(TransparentImageProcessor(), attr), attr)

    def test_process_trim_only(self):
        from PIL import Image
        img = Image.new("RGBA", (20, 20), (0, 0, 0, 0))
        # opaque center
        for x in range(5, 15):
            for y in range(5, 15):
                img.putpixel((x, y), (255, 0, 0, 255))
        out, details = TransparentImageProcessor().process(img, {
            "enable_matting": False,
            "enable_trim": True,
            "alpha_threshold": 0,
            "enable_layout": False,
        })
        self.assertEqual(out.size, (10, 10))
        self.assertIn("trim_bbox", details)

    def test_layout_reserve_and_bottom_left_position(self):
        from PIL import Image
        asset = Image.new("RGBA", (100, 100), (255, 0, 0, 255))
        out, details = place_subject_on_canvas(
            asset,
            (1000, 1000),
            subject_percent=100,
            reserve_top_percent=10,
            reserve_bottom_percent=20,
            subject_position="bottom_left",
            detail_restore="off",
        )
        self.assertEqual(out.size, (1000, 1000))
        self.assertEqual(details["available_box"], (0, 100, 1000, 800))
        self.assertEqual(details["layout_display_size"], (700, 700))
        self.assertEqual(details["paste_pos"], (0, 100))

    def test_layout_subject_size_is_relative_to_available_area(self):
        from PIL import Image
        asset = Image.new("RGBA", (100, 100), (255, 0, 0, 255))
        _, details = place_subject_on_canvas(
            asset,
            (1000, 1000),
            subject_percent=50,
            reserve_bottom_percent=20,
            subject_position="bottom_center",
            detail_restore="off",
        )
        self.assertEqual(details["layout_display_size"], (400, 400))
        self.assertEqual(details["paste_pos"], (300, 400))


if __name__ == "__main__":
    unittest.main()
