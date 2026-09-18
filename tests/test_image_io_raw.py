"""RAW 加载与可写格式收敛测试。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from core.image_io import (
    RAW_EXTS,
    coerce_writable_format,
    is_raw_ext,
    is_raw_path,
    load_image,
)
from services.common.importing import IMAGE_EXTS, image_dialog_filter, image_only_dialog_filter


class TestRawExts(unittest.TestCase):
    def test_common_raw_exts_present(self):
        for e in (".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".rw2", ".orf"):
            self.assertIn(e, RAW_EXTS)
            self.assertIn(e, IMAGE_EXTS)
            self.assertTrue(is_raw_ext(e))
            self.assertTrue(is_raw_ext(e.lstrip(".")))
            self.assertTrue(is_raw_path(f"C:/photos/a{e}"))

    def test_standard_not_raw(self):
        self.assertFalse(is_raw_ext(".png"))
        self.assertFalse(is_raw_path("a.jpg"))
        self.assertIn(".png", IMAGE_EXTS)

    def test_dialog_filter_includes_raw(self):
        f = image_dialog_filter()
        self.assertIn("*.cr2", f)
        self.assertIn("*.dng", f)
        self.assertIn("*.docx", f)
        f2 = image_only_dialog_filter()
        self.assertIn("*.arw", f2)
        self.assertNotIn("*.docx", f2)


class TestCoerceWritableFormat(unittest.TestCase):
    def test_explicit_png(self):
        self.assertEqual(coerce_writable_format("png", "a.cr2"), "png")
        self.assertEqual(coerce_writable_format("JPG", "a.png"), "jpg")

    def test_empty_keeps_standard_ext(self):
        self.assertEqual(coerce_writable_format("", "photo.jpg"), "jpg")
        self.assertEqual(coerce_writable_format("", "photo.jpeg"), "jpg")
        self.assertEqual(coerce_writable_format("", "photo.PNG"), "png")
        self.assertEqual(coerce_writable_format("", "photo.tif"), "tiff")

    def test_raw_forces_png(self):
        self.assertEqual(coerce_writable_format("", "shot.CR2"), "png")
        self.assertEqual(coerce_writable_format("", "shot.nef"), "png")
        self.assertEqual(coerce_writable_format("cr2", "shot.cr2"), "png")
        self.assertEqual(coerce_writable_format("raw", Path("x.dng")), "png")


class TestLoadImageStandard(unittest.TestCase):
    def test_load_png(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.png"
            Image.new("RGB", (4, 3), (10, 20, 30)).save(p)
            img = load_image(p)
            self.assertEqual(img.size, (4, 3))
            img.close()

    def test_raw_without_rawpy_raises_clear_error(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "fake.cr2"
            p.write_bytes(b"not-a-real-raw")
            with mock.patch.dict("sys.modules", {"rawpy": None}):
                # force ImportError path inside _load_with_rawpy
                import core.image_io as m

                real_import = __import__

                def _block_rawpy(name, *a, **k):
                    if name == "rawpy" or name.startswith("rawpy."):
                        raise ImportError("blocked")
                    return real_import(name, *a, **k)

                with mock.patch("builtins.__import__", side_effect=_block_rawpy):
                    with self.assertRaises(RuntimeError) as cm:
                        m.load_image(p)
                    self.assertIn("rawpy", str(cm.exception).lower())


if __name__ == "__main__":
    unittest.main()
