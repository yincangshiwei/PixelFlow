"""P-1 基线测试：HTML 引用提取 / 去重键 / 相对路径 / 导入分组。

固化导入逻辑的既有行为；P0 起实现位于 services/common/importing
（原 ui/main_window.py 模块级函数），本文件仅更新了 import 来源。
"""
import base64
import tempfile
import unittest
from pathlib import Path

from services.common.importing import (
    EXTRACT_EXTS,
    IMAGE_EXTS,
    _cf_html_source_url,
    _collect_extract_files,
    _collect_import_groups,
    _extract_image_refs_from_html,
    _guess_image_ext,
    _image_ref_dedupe_key,
    _looks_like_image_url,
    _normalize_ref_text,
    _resolve_html_ref,
    _scan_folder_extract_files,
    _scan_folder_images,
    _strip_cf_html_header,
    _write_image_bytes,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 40
JPG_BYTES = b"\xff\xd8\xff\xe0" + b"1" * 40
WEBP_BYTES = b"RIFF\x00\x00\x00\x00WEBP" + b"2" * 40


def b64_png(payload: bytes = PNG_BYTES) -> str:
    return "data:image/png;base64," + base64.b64encode(payload).decode("ascii")


class TestExtractImageRefsFromHtml(unittest.TestCase):
    def test_basic_img_src(self):
        html = '<html><body><img src="https://cdn.example.com/images/pic1.jpg"></body></html>'
        refs = _extract_image_refs_from_html(html)
        self.assertEqual(refs, ["https://cdn.example.com/images/pic1.jpg"])

    def test_lazy_load_attrs(self):
        html = '<img data-src="https://cdn.example.com/images/lazy.png">'
        refs = _extract_image_refs_from_html(html)
        self.assertIn("https://cdn.example.com/images/lazy.png", refs)

    def test_srcset_split(self):
        html = '<img srcset="https://cdn.example.com/a.jpg 1x, https://cdn.example.com/b.jpg 2x">'
        refs = _extract_image_refs_from_html(html)
        self.assertIn("https://cdn.example.com/a.jpg", refs)
        self.assertIn("https://cdn.example.com/b.jpg", refs)

    def test_same_url_different_query_deduped(self):
        html = (
            '<img src="https://cdn.example.com/p.png?sig=abc">'
            '<img src="https://cdn.example.com/p.png?sig=abcdef&full=1">'
        )
        refs = _extract_image_refs_from_html(html)
        self.assertEqual(len(refs), 1)
        # 同资源保留更长（query 更完整）的那条
        self.assertEqual(refs[0], "https://cdn.example.com/p.png?sig=abcdef&full=1")

    def test_data_uri_content_dedupe(self):
        uri1 = b64_png()
        # 同内容、base64 中插入空白仍合并
        raw = base64.b64encode(PNG_BYTES).decode("ascii")
        uri2 = "data:image/png;base64," + raw[:8] + " " + raw[8:]
        html = f'<img src="{uri1}"><img src="{uri2}">'
        refs = _extract_image_refs_from_html(html)
        self.assertEqual(len(refs), 1)

    def test_data_uri_not_split_by_srcset_rule(self):
        uri = b64_png()
        html = f'<img srcset="{uri}">'
        refs = _extract_image_refs_from_html(html)
        self.assertEqual(len(refs), 1)
        self.assertTrue(refs[0].startswith("data:image/png;base64,"))

    def test_og_image_meta(self):
        html = '<meta property="og:image" content="https://cdn.example.com/og.png">'
        refs = _extract_image_refs_from_html(html)
        self.assertIn("https://cdn.example.com/og.png", refs)

    def test_css_url_background(self):
        html = '<div style="background-image: url(\'https://cdn.example.com/bg.webp\')"></div>'
        refs = _extract_image_refs_from_html(html)
        self.assertIn("https://cdn.example.com/bg.webp", refs)

    def test_relative_paths_kept(self):
        html = '<img src="images/a.png"><img src="b.jpg"><img src="readme.txt">'
        refs = _extract_image_refs_from_html(html)
        self.assertIn("images/a.png", refs)
        self.assertIn("b.jpg", refs)
        self.assertNotIn("readme.txt", refs)

    def test_non_image_links_excluded(self):
        html = (
            '<img src="javascript:void(0)">'
            '<img src="https://example.com/page.html">'
            '<img src="https://example.com/assets/app.js">'
        )
        refs = _extract_image_refs_from_html(html)
        self.assertEqual(refs, [])

    def test_dingtalk_cangjie_stripped(self):
        """钉钉 data-clipboard-cangjie 与真实 <img> 重复，只应抽出一张。"""
        html = (
            '<div data-clipboard-cangjie="{&quot;resources&quot;:[{&quot;src&quot;:'
            '&quot;https://static.dingtalk.com/media/img1.png&quot;}]}">'
            '<img src="https://static.dingtalk.com/media/img1.png"></div>'
        )
        refs = _extract_image_refs_from_html(html)
        self.assertEqual(refs, ["https://static.dingtalk.com/media/img1.png"])

    def test_script_style_blocks_ignored(self):
        html = (
            "<script>var u='https://cdn.example.com/js.png';</script>"
            "<style>.a{background:url(https://cdn.example.com/css.png)}</style>"
            '<img src="https://cdn.example.com/real.png">'
        )
        refs = _extract_image_refs_from_html(html)
        self.assertEqual(refs, ["https://cdn.example.com/real.png"])

    def test_empty_input(self):
        self.assertEqual(_extract_image_refs_from_html(""), [])
        self.assertEqual(_extract_image_refs_from_html(None), [])


class TestCfHtml(unittest.TestCase):
    def _build_cf_html(self, body: str, source_url: str = "") -> str:
        head = "Version:0.9\r\nStartHTML:{off:08d}\r\n"
        if source_url:
            head += "SourceURL:" + source_url + "\r\n"
        off = len(head.format(off=0))
        return head.format(off=off) + body

    def test_strip_cf_html_header_by_offset(self):
        body = '<html><body><img src="x.png"></body></html>'
        raw = self._build_cf_html(body, "https://example.com/page")
        self.assertEqual(_strip_cf_html_header(raw), body)

    def test_strip_falls_back_to_html_tag(self):
        raw = "junk header\n<html><body></body></html>"
        self.assertEqual(_strip_cf_html_header(raw), "<html><body></body></html>")

    def test_source_url(self):
        raw = self._build_cf_html("<html></html>", "https://example.com/page?q=1")
        self.assertEqual(_cf_html_source_url(raw), "https://example.com/page?q=1")
        self.assertEqual(_cf_html_source_url("<html></html>"), "")

    def test_cf_html_end_to_end(self):
        body = '<html><body><img src="https://cdn.example.com/images/pic2.jpg"></body></html>'
        raw = self._build_cf_html(body, "https://example.com/page")
        refs = _extract_image_refs_from_html(raw)
        self.assertEqual(refs, ["https://cdn.example.com/images/pic2.jpg"])


class TestDedupeKey(unittest.TestCase):
    def test_http_ignores_query_and_hash(self):
        k1 = _image_ref_dedupe_key("https://cdn.x.com/a.png?sig=abc")
        k2 = _image_ref_dedupe_key("https://cdn.x.com/a.png?x=1#frag")
        self.assertEqual(k1, k2)

    def test_http_case_and_trailing_slash_normalized(self):
        k1 = _image_ref_dedupe_key("https://CDN.X.com/A.PNG")
        k2 = _image_ref_dedupe_key("https://cdn.x.com/a.png")
        self.assertEqual(k1, k2)
        k3 = _image_ref_dedupe_key("https://cdn.x.com/img/")
        k4 = _image_ref_dedupe_key("https://cdn.x.com/img")
        self.assertEqual(k3, k4)

    def test_scheme_is_part_of_key(self):
        self.assertNotEqual(
            _image_ref_dedupe_key("http://cdn.x.com/a.png"),
            _image_ref_dedupe_key("https://cdn.x.com/a.png"),
        )

    def test_protocol_relative_upgraded_to_https(self):
        self.assertEqual(
            _image_ref_dedupe_key("//cdn.x.com/a.png"),
            _image_ref_dedupe_key("https://cdn.x.com/a.png"),
        )

    def test_data_uri_by_content_hash(self):
        raw = base64.b64encode(PNG_BYTES).decode("ascii")
        k1 = _image_ref_dedupe_key("data:image/png;base64," + raw)
        k2 = _image_ref_dedupe_key("data:image/png;base64," + raw[:6] + " " + raw[6:])
        self.assertEqual(k1, k2)
        k3 = _image_ref_dedupe_key(b64_png(JPG_BYTES))
        self.assertNotEqual(k1, k3)

    def test_relative_path_normalized(self):
        k1 = _image_ref_dedupe_key("images/A.png?x=1")
        k2 = _image_ref_dedupe_key("images\\a.png")
        self.assertEqual(k1, k2)

    def test_empty_ref(self):
        self.assertEqual(_image_ref_dedupe_key(""), "")
        self.assertEqual(_image_ref_dedupe_key("   "), "")


class TestNormalizeAndFilter(unittest.TestCase):
    def test_normalize_ref_text(self):
        self.assertEqual(
            _normalize_ref_text('"https://x.com/a.png"'), "https://x.com/a.png"
        )
        self.assertEqual(
            _normalize_ref_text("https://x.com/a.png?x=1&amp;y=2"),
            "https://x.com/a.png?x=1&y=2",
        )
        self.assertEqual(_normalize_ref_text("https://x.com/a.png\\"), "https://x.com/a.png")
        self.assertEqual(_normalize_ref_text("https://x.com/a.png&"), "https://x.com/a.png")
        self.assertEqual(_normalize_ref_text("  "), "")

    def test_looks_like_image_url(self):
        self.assertTrue(_looks_like_image_url("https://x.com/a.png"))
        self.assertTrue(_looks_like_image_url("https://x.com/a.png?sig=1"))
        self.assertTrue(_looks_like_image_url("https://x.com/img/abc"))
        self.assertTrue(_looks_like_image_url("https://x.com/upload/123"))
        self.assertTrue(_looks_like_image_url("data:image/png;base64,xx"))
        self.assertFalse(_looks_like_image_url("https://x.com/page"))
        self.assertFalse(_looks_like_image_url("javascript:void(0)"))
        self.assertFalse(_looks_like_image_url("mailto:a@b.c"))
        self.assertFalse(_looks_like_image_url("#"))
        self.assertFalse(_looks_like_image_url(""))

    def test_guess_image_ext(self):
        self.assertEqual(_guess_image_ext(PNG_BYTES), ".png")
        self.assertEqual(_guess_image_ext(JPG_BYTES), ".jpg")
        self.assertEqual(_guess_image_ext(WEBP_BYTES), ".webp")
        self.assertEqual(_guess_image_ext(b"RIFF\x00\x00\x00\x00AVI "), ".png")
        self.assertEqual(_guess_image_ext(b"II*\x00" + b"0" * 20), ".tif")
        self.assertEqual(_guess_image_ext(b"", hint="photo.jpeg"), ".jpg")
        self.assertEqual(_guess_image_ext(b"", hint="image/webp"), ".webp")
        self.assertEqual(_guess_image_ext(b""), ".png")


class TestWriteImageBytes(unittest.TestCase):
    def test_content_addressed_dedupe(self):
        created = set()
        try:
            p1 = _write_image_bytes(PNG_BYTES, preferred_name="x.png")
            self.assertIsNotNone(p1)
            # 同内容 + 同 preferred_name 复用同一文件（内容寻址去重）
            p2 = _write_image_bytes(PNG_BYTES, preferred_name="x.png")
            self.assertEqual(p1, p2)
            # 不同 preferred_name 落不同文件；跨文件内容去重由
            # _materialize_image_refs 的 sha1 层完成（基线：此处不去重）
            p3 = _write_image_bytes(PNG_BYTES, preferred_name="y.png")
            self.assertNotEqual(p1, p3)
            # 同名下内容不同 → 不同文件，扩展名按魔数识别
            p4 = _write_image_bytes(JPG_BYTES, preferred_name="x.png")
            self.assertNotEqual(p1, p4)
            self.assertEqual(Path(p4).suffix, ".jpg")
            created.update((p1, p3, p4))
        finally:
            for p in created:
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError:
                    pass

    def test_too_small_rejected(self):
        self.assertIsNone(_write_image_bytes(b"tiny"))


class TestImportGroups(unittest.TestCase):
    """文件夹扫描 / 导入分组 / 抽图容器收集的相对路径基线。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name) / "dataset"
        (root / "sub" / "deep").mkdir(parents=True)
        (root / "a.png").write_bytes(PNG_BYTES)
        (root / "b.jpg").write_bytes(JPG_BYTES)
        (root / "page.html").write_text("<img src='x.png'>", encoding="utf-8")
        (root / "doc.docx").write_bytes(b"PK\x03\x04")
        (root / "sub" / "c.webp").write_bytes(WEBP_BYTES)
        (root / "sub" / "deep" / "d.gif").write_bytes(b"GIF89a" + b"0" * 30)
        (root / "sub" / "deep" / "note.pdf").write_bytes(b"%PDF-1.4")
        self.root = root

    def tearDown(self):
        self._tmp.cleanup()

    def test_scan_folder_images_recursive_sorted(self):
        files = _scan_folder_images(self.root)
        names = [Path(f).name for f in files]
        self.assertEqual(names, ["a.png", "b.jpg", "c.webp", "d.gif"])
        for f in files:
            self.assertTrue(Path(f).is_absolute())

    def test_scan_folder_extract_files(self):
        files = _scan_folder_extract_files(self.root)
        names = sorted(Path(f).name for f in files)
        self.assertEqual(names, ["doc.docx", "note.pdf", "page.html"])

    def test_collect_import_groups_folder(self):
        groups = _collect_import_groups([self.root])
        self.assertEqual(len(groups), 1)
        files, base_dir = groups[0]
        self.assertEqual(len(files), 4)
        self.assertEqual(base_dir, str(self.root.resolve()))

    def test_collect_import_groups_single_file(self):
        single = self.root / "a.png"
        groups = _collect_import_groups([single])
        self.assertEqual(groups, [([str(single.resolve())], None)])

    def test_collect_import_groups_skips_containers(self):
        groups = _collect_import_groups([self.root / "page.html"])
        self.assertEqual(groups, [])

    def test_collect_import_groups_mixed(self):
        groups = _collect_import_groups(
            [self.root / "a.png", self.root / "sub", self.root / "doc.docx"]
        )
        # 单图一组 + 文件夹一组；docx 不进入分组
        self.assertEqual(len(groups), 2)
        self.assertIsNone(groups[0][1])
        self.assertEqual(groups[1][1], str((self.root / "sub").resolve()))

    def test_collect_extract_files_dedupe(self):
        files = _collect_extract_files(
            [self.root, self.root / "page.html"]
        )
        names = [Path(f).name for f in files]
        self.assertEqual(sorted(names), ["doc.docx", "note.pdf", "page.html"])
        # page.html 来自文件夹递归与直接指定，只出现一次
        self.assertEqual(names.count("page.html"), 1)

    def test_image_and_extract_exts_disjoint(self):
        self.assertFalse(IMAGE_EXTS & EXTRACT_EXTS)


class TestResolveHtmlRef(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        (self.base / "images").mkdir()
        (self.base / "images" / "a.png").write_bytes(PNG_BYTES)
        self.html_file = self.base / "page.html"
        self.html_file.write_text("<html></html>", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_data_uri(self):
        kind, value = _resolve_html_ref(b64_png())
        self.assertEqual(kind, "data")

    def test_protocol_relative_uses_page_scheme(self):
        kind, value = _resolve_html_ref("//cdn.x.com/a.png")
        self.assertEqual((kind, value), ("http", "https://cdn.x.com/a.png"))
        kind, value = _resolve_html_ref(
            "//cdn.x.com/a.png", page_url="http://site.com/p"
        )
        self.assertEqual((kind, value), ("http", "http://cdn.x.com/a.png"))

    def test_absolute_http(self):
        kind, value = _resolve_html_ref("https://cdn.x.com/a.png")
        self.assertEqual((kind, value), ("http", "https://cdn.x.com/a.png"))

    def test_relative_to_html_file(self):
        kind, value = _resolve_html_ref("images/a.png", html_base=self.html_file)
        self.assertEqual(kind, "file")
        self.assertEqual(Path(value), (self.base / "images" / "a.png").resolve())

    def test_relative_missing_falls_back_to_page_url(self):
        kind, value = _resolve_html_ref(
            "img/notlocal.png",
            html_base=self.html_file,
            page_url="https://site.com/pages/",
        )
        self.assertEqual((kind, value), ("http", "https://site.com/pages/img/notlocal.png"))

    def test_windows_absolute_path(self):
        kind, value = _resolve_html_ref(r"C:\photos\a.png")
        self.assertEqual((kind, value), ("file", r"C:\photos\a.png"))

    def test_unresolvable_relative_skipped(self):
        kind, value = _resolve_html_ref("no/such.png")
        self.assertEqual((kind, value), ("skip", ""))

    def test_empty_skipped(self):
        kind, value = _resolve_html_ref("   ")
        self.assertEqual((kind, value), ("skip", ""))


if __name__ == "__main__":
    unittest.main()
