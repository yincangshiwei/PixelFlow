"""统一比例排版（img2doc）纯函数与 Service 校验测试。"""
from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from core.processors.img2doc_processor import (
    _image_size_exif,
    compute_uniform_ratio,
    default_img2doc_options,
    image_has_transparency,
    normalize_image_to_ratio,
)
from services.features.img2doc_service import Img2DocService


def _make_png(tmp: Path, size, mode="RGB", color=(200, 30, 30), alpha=None) -> str:
    img = Image.new(mode, size, color)
    if alpha is not None:
        img.putalpha(alpha)
    p = tmp / f"t_{size[0]}x{size[1]}_{mode}_{alpha is not None}.png"
    img.save(p, format="PNG")
    return str(p)


class ComputeUniformRatioTests(unittest.TestCase):
    def test_auto_majority(self):
        sizes = [(1600, 900), (1920, 1080), (800, 600), (400, 300)]
        # 16:9 出现两次（1.78），4:3 两次（1.33）→ 众数并列取先出现者
        self.assertEqual(compute_uniform_ratio(sizes, mode="auto"), 1.78)

    def test_auto_all_unique_falls_back_to_first(self):
        sizes = [(100, 50), (30, 20), (7, 3)]
        self.assertEqual(compute_uniform_ratio(sizes, mode="auto"), 2.0)

    def test_custom(self):
        self.assertAlmostEqual(compute_uniform_ratio([], mode="custom", custom_w=3, custom_h=2), 1.5)

    def test_preset_string(self):
        self.assertAlmostEqual(compute_uniform_ratio([], mode="16:9"), 16 / 9)
        self.assertAlmostEqual(compute_uniform_ratio([], mode="1:1"), 1.0)

    def test_empty_defaults_4_3(self):
        self.assertAlmostEqual(compute_uniform_ratio([], mode="auto"), 4 / 3)

    def test_invalid_custom_falls_back(self):
        self.assertAlmostEqual(
            compute_uniform_ratio([(100, 100)], mode="custom", custom_w=0, custom_h=0), 1.0
        )


class NormalizeImageTests(unittest.TestCase):
    def test_already_target_ratio_unchanged(self):
        img = Image.new("RGB", (160, 90))
        out = normalize_image_to_ratio(img, 16 / 9)
        self.assertEqual(out.size, (160, 90))

    def test_solid_fill_size_and_content(self):
        img = Image.new("RGB", (20, 10), (200, 30, 30))
        out = normalize_image_to_ratio(img, 1.0, fill_mode="solid", fill_color="#FFFFFF")
        self.assertEqual(out.size, (20, 20))
        self.assertEqual(out.getpixel((10, 2)), (255, 255, 255))   # 顶部留白
        self.assertEqual(out.getpixel((10, 10)), (200, 30, 30))    # 中间原图

    def test_auto_opaque_uses_solid(self):
        img = Image.new("RGB", (10, 20), (10, 200, 30))
        out = normalize_image_to_ratio(img, 1.0, fill_mode="auto", fill_color="#FFFFFF")
        self.assertEqual(out.mode, "RGB")
        self.assertEqual(out.size, (20, 20))
        self.assertEqual(out.getpixel((2, 10)), (255, 255, 255))   # 左侧留白

    def test_auto_transparent_pads_transparent(self):
        alpha = Image.new("L", (10, 20), 0)  # 全透明底
        img = Image.new("RGBA", (10, 20), (50, 100, 150))
        img.putalpha(alpha)
        out = normalize_image_to_ratio(img, 1.0, fill_mode="auto")
        self.assertEqual(out.mode, "RGBA")
        self.assertEqual(out.size, (20, 20))
        self.assertEqual(out.getpixel((2, 10))[3], 0)              # 左侧补透明
        self.assertEqual(out.getpixel((10, 10))[3], 0)             # 原图区域仍透明

    def test_transparent_mode_keeps_alpha(self):
        img = Image.new("RGB", (20, 10), (200, 30, 30))
        out = normalize_image_to_ratio(img, 1.0, fill_mode="transparent")
        self.assertEqual(out.mode, "RGBA")
        self.assertEqual(out.getpixel((10, 2))[3], 0)

    def test_blur_mode_returns_rgb_canvas(self):
        img = Image.new("RGB", (20, 10), (90, 90, 90))
        out = normalize_image_to_ratio(img, 1.0, fill_mode="blur")
        self.assertEqual(out.mode, "RGB")
        self.assertEqual(out.size, (20, 20))

    def test_invalid_ratio_returns_original(self):
        img = Image.new("RGB", (30, 10))
        self.assertIs(normalize_image_to_ratio(img, 0), img)


class HelperTests(unittest.TestCase):
    def test_image_has_transparency(self):
        opaque = Image.new("RGB", (5, 5))
        self.assertFalse(image_has_transparency(opaque))
        rgba = Image.new("RGBA", (5, 5), (0, 0, 0, 255))
        self.assertFalse(image_has_transparency(rgba))
        rgba.putpixel((0, 0), (0, 0, 0, 0))
        self.assertTrue(image_has_transparency(rgba))

    def test_image_size_exif(self):
        with tempfile.TemporaryDirectory() as td:
            p = _make_png(Path(td), (32, 16))
            self.assertEqual(_image_size_exif(p), (32, 16))
            self.assertEqual(_image_size_exif(str(Path(td) / "missing.png")), (1, 1))

    def test_default_options_has_uniform_keys(self):
        opts = default_img2doc_options()
        for key in (
            "uniform_ratio_enabled", "uniform_ratio_mode", "uniform_ratio_w",
            "uniform_ratio_h", "uniform_fill_mode", "uniform_fill_color",
        ):
            self.assertIn(key, opts)
        self.assertFalse(opts["uniform_ratio_enabled"])


class ServiceValidationTests(unittest.TestCase):
    def setUp(self):
        self.svc = Img2DocService()

    def test_roundtrip(self):
        r = self.svc.validate_and_normalize({
            "uniform_ratio_enabled": True,
            "uniform_ratio_mode": "16:9",
            "uniform_fill_mode": "blur",
            "uniform_fill_color": "#123456",
        }, strict=True)
        self.assertTrue(r.ok)
        v = r.value
        self.assertTrue(v["uniform_ratio_enabled"])
        self.assertEqual(v["uniform_ratio_mode"], "16:9")
        self.assertEqual(v["uniform_fill_mode"], "blur")
        self.assertEqual(v["uniform_fill_color"], "#123456")

    def test_invalid_values_fallback(self):
        r = self.svc.validate_and_normalize({
            "uniform_ratio_mode": "bogus",
            "uniform_fill_mode": "bogus",
            "uniform_fill_color": "red",
            "uniform_ratio_w": -5,
        }, strict=False)
        self.assertTrue(r.ok)
        v = r.value
        self.assertEqual(v["uniform_ratio_mode"], "auto")
        self.assertEqual(v["uniform_fill_mode"], "auto")
        self.assertEqual(v["uniform_fill_color"], "#FFFFFF")
        self.assertEqual(v["uniform_ratio_w"], 1)

    def test_old_preset_without_keys_gets_defaults(self):
        r = self.svc.validate_and_normalize({"format": "PDF"}, strict=True)
        self.assertTrue(r.ok)
        self.assertFalse(r.value["uniform_ratio_enabled"])
        self.assertEqual(r.value["uniform_fill_mode"], "auto")


class CacheStreamSafetyTests(unittest.TestCase):
    """预处理缓存流不得被导出流程意外关闭（回归：I/O operation on closed file）。"""

    def setUp(self):
        from core.processors.img2doc_processor import Img2DocProcessor
        self.proc = Img2DocProcessor()
        buf = io.BytesIO()
        Image.new("RGB", (8, 8), (255, 0, 0)).save(buf, format="PNG")
        self.cache = {"a.png": buf}

    def test_close_image_does_not_close_cache(self):
        img = self.proc._open_image_for_export("a.png", self.cache)
        img.close()  # PIL close 会关闭其底层流
        # 缓存本体必须仍然可用
        stream = self.proc._cache_stream("a.png", self.cache)
        self.assertEqual(Image.open(stream).size, (8, 8))

    def test_cache_stream_independent_copies(self):
        s1 = self.proc._cache_stream("a.png", self.cache)
        s2 = self.proc._cache_stream("a.png", self.cache)
        s1.close()
        # 关闭一个副本不影响缓存与其他副本
        self.assertEqual(Image.open(s2).size, (8, 8))
        self.assertEqual(
            Image.open(self.proc._cache_stream("a.png", self.cache)).size, (8, 8)
        )


if __name__ == "__main__":
    unittest.main()
