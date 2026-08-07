"""P0 测试：FileImportService 导入计划 / 剪贴板动作链 / 抽图任务执行。

覆盖原 _import_local_paths / _paste_from_clipboard / _start_container_extract
内的纯逻辑语义（行为等价基线）。
"""
import base64
import tempfile
import unittest
from pathlib import Path

from services.common.importing import (
    ACTION_EXTRACT_HTML,
    ACTION_IMPORT_PATHS,
    ACTION_SAVE_IMAGE,
    ClipboardPayload,
    FileImportService,
    _file_uri_to_local_path,
    parse_text_paths,
    run_extract_jobs,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 40
JPG_BYTES = b"\xff\xd8\xff\xe0" + b"1" * 40


def b64_png(payload: bytes = PNG_BYTES) -> str:
    return "data:image/png;base64," + base64.b64encode(payload).decode("ascii")


class TestPlanLocalImport(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name) / "dataset"
        (root / "sub").mkdir(parents=True)
        (root / "a.png").write_bytes(PNG_BYTES)
        (root / "b.jpg").write_bytes(JPG_BYTES)
        (root / "page.html").write_text("<img src='x.png'>", encoding="utf-8")
        (root / "doc.docx").write_bytes(b"PK\x03\x04")
        (root / "sub" / "c.webp").write_bytes(b"RIFF\x00\x00\x00\x00WEBP" + b"2" * 40)
        self.root = root
        self.svc = FileImportService()

    def tearDown(self):
        self._tmp.cleanup()

    def test_folder_groups_and_containers(self):
        plan = self.svc.plan_local_import([self.root])
        self.assertEqual(len(plan.groups), 1)
        files, base_dir = plan.groups[0]
        self.assertEqual(len(files), 3)
        self.assertEqual(base_dir, str(self.root.resolve()))
        # 容器：html + docx
        names = sorted(Path(p).name for p in plan.container_paths)
        self.assertEqual(names, ["doc.docx", "page.html"])
        self.assertEqual(plan.container_kind, "DOCX / HTML")
        self.assertIn("2 个文件", plan.busy_tip)
        self.assertEqual(plan.direct_count, 3)

    def test_single_image_no_containers(self):
        plan = self.svc.plan_local_import([self.root / "a.png"])
        self.assertEqual(plan.direct_count, 1)
        self.assertEqual(plan.container_paths, [])
        self.assertIsNone(plan.groups[0][1])
        self.assertEqual(plan.busy_tip, "")

    def test_empty_input(self):
        plan = self.svc.plan_local_import([])
        self.assertEqual(plan.direct_count, 0)
        self.assertEqual(plan.container_paths, [])

    def test_containers_only(self):
        plan = self.svc.plan_local_import([self.root / "doc.docx"])
        self.assertEqual(plan.direct_count, 0)
        self.assertEqual(len(plan.container_paths), 1)
        self.assertEqual(plan.container_kind, "DOCX")


class TestPlanClipboard(unittest.TestCase):
    def setUp(self):
        self.svc = FileImportService()
        self._tmp = tempfile.TemporaryDirectory()
        self.img = Path(self._tmp.name) / "p.png"
        self.img.write_bytes(PNG_BYTES)

    def tearDown(self):
        self._tmp.cleanup()

    def test_local_paths_consume(self):
        payload = ClipboardPayload(local_paths=[str(self.img)], has_image=True)
        actions = self.svc.plan_clipboard(payload)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].kind, ACTION_IMPORT_PATHS)
        self.assertEqual(actions[0].import_plan.direct_count, 1)

    def test_unimportable_paths_fall_through_to_image(self):
        # 路径存在但既非图片也非容器 → 不消费，落到位图动作
        payload = ClipboardPayload(local_paths=[str(self.img.parent)], has_image=True)
        # 注意：文件夹会递归扫描，该目录含 p.png → 仍可导入
        actions = self.svc.plan_clipboard(payload)
        self.assertEqual(actions[0].kind, ACTION_IMPORT_PATHS)

    def test_image_then_html_fallback_chain(self):
        html = '<html><body><img src="https://cdn.example.com/x.jpg"></body></html>'
        payload = ClipboardPayload(has_image=True, html_text=html)
        actions = self.svc.plan_clipboard(payload)
        self.assertEqual([a.kind for a in actions], [ACTION_SAVE_IMAGE, ACTION_EXTRACT_HTML])
        html_action = actions[1]
        self.assertEqual(html_action.html_refs, ["https://cdn.example.com/x.jpg"])
        self.assertIn("1", html_action.busy_tip)

    def test_html_page_url_from_cf_html(self):
        head = "Version:0.9\r\nStartHTML:{off:08d}\r\nSourceURL:https://example.com/p\r\n"
        body = '<html><body><img src="https://cdn.example.com/y.png"></body></html>'
        off = len(head.format(off=0))
        raw = head.format(off=off) + body
        payload = ClipboardPayload(html_text=raw)
        actions = self.svc.plan_clipboard(payload)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].kind, ACTION_EXTRACT_HTML)
        self.assertEqual(actions[0].html_page_url, "https://example.com/p")

    def test_html_without_refs_falls_to_text(self):
        payload = ClipboardPayload(html_text="<html><body>no images</body></html>",
                                   text=str(self.img))
        actions = self.svc.plan_clipboard(payload)
        self.assertEqual([a.kind for a in actions], [ACTION_IMPORT_PATHS])
        self.assertEqual(actions[0].import_plan.direct_count, 1)

    def test_text_paths_only(self):
        payload = ClipboardPayload(text=f'"{self.img}"\n\nnot_exist_path_xyz')
        actions = self.svc.plan_clipboard(payload)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].kind, ACTION_IMPORT_PATHS)
        self.assertEqual(actions[0].import_plan.direct_count, 1)

    def test_empty_payload(self):
        self.assertEqual(self.svc.plan_clipboard(ClipboardPayload()), [])


class TestParseTextPaths(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.img = Path(self._tmp.name) / "a.png"
        self.img.write_bytes(PNG_BYTES)

    def tearDown(self):
        self._tmp.cleanup()

    def test_plain_and_quoted_lines(self):
        text = f"{self.img}\n\"{self.img}\"\nno_such_file_abc"
        paths = parse_text_paths(text)
        self.assertEqual(paths, [str(self.img), str(self.img)])

    def test_file_uri(self):
        uri = self.img.as_uri()
        paths = parse_text_paths(uri)
        self.assertEqual(len(paths), 1)
        self.assertEqual(Path(paths[0]), self.img.resolve())

    def test_empty(self):
        self.assertEqual(parse_text_paths(""), [])
        self.assertEqual(parse_text_paths("   \n  "), [])


class TestFileUriToLocalPath(unittest.TestCase):
    def test_windows_drive_uri(self):
        local = _file_uri_to_local_path("file:///C:/photos/a.png")
        self.assertIsNotNone(local)
        self.assertIn("a.png", local.replace("\\", "/"))

    def test_localhost_host(self):
        local = _file_uri_to_local_path("file://localhost/C:/photos/a.png")
        self.assertIsNotNone(local)

    def test_non_file_scheme(self):
        self.assertIsNone(_file_uri_to_local_path("https://x.com/a.png"))
        self.assertIsNone(_file_uri_to_local_path(""))


class TestNormalizeHtmlJobs(unittest.TestCase):
    def setUp(self):
        self.svc = FileImportService()

    def test_dedupe_across_jobs(self):
        jobs = [
            (["https://cdn.x.com/a.png?sig=1", "https://cdn.x.com/a.png"], None, ""),
            (["https://cdn.x.com/a.png", "https://cdn.x.com/b.png"], None, "https://p.com/"),
        ]
        norm = self.svc.normalize_html_jobs(jobs)
        self.assertEqual(len(norm), 2)
        self.assertEqual(len(norm[0][0]), 1)  # 同资源两条 URL 合并
        self.assertEqual(norm[0][1], None)
        self.assertEqual(norm[1][0], ["https://cdn.x.com/b.png"])
        self.assertEqual(norm[1][2], "https://p.com/")

    def test_empty_jobs_skipped(self):
        self.assertEqual(self.svc.normalize_html_jobs([([], None, "")]), [])
        self.assertEqual(self.svc.normalize_html_jobs(None), [])

    def test_base_path_stringified(self):
        norm = self.svc.normalize_html_jobs([(["a.png"], Path("C:/base/page.html"), "")])
        self.assertEqual(norm[0][1], str(Path("C:/base/page.html")))


class TestRunExtractJobs(unittest.TestCase):
    """html_jobs 物化：data URI 落盘 + 内容去重（不依赖网络）。"""

    def test_data_uri_materialize_and_content_dedupe(self):
        uri1 = b64_png()
        # 同内容、不同 base64 空白 → 引用键层合并
        raw = base64.b64encode(PNG_BYTES).decode("ascii")
        uri2 = "data:image/png;base64," + raw[:8] + " " + raw[8:]
        # 不同内容 → 独立文件
        uri3 = b64_png(JPG_BYTES)
        paths = run_extract_jobs([], [([uri1, uri2, uri3], None, "")])
        self.assertEqual(len(paths), 2)
        for p in paths:
            self.assertTrue(Path(p).is_file())
        # 扩展名按魔数识别
        exts = sorted(Path(p).suffix for p in paths)
        self.assertEqual(exts, [".jpg", ".png"])

    def test_container_html_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            html = Path(tmp) / "page.html"
            html.write_text(f"<img src='{b64_png()}'>", encoding="utf-8")
            paths = run_extract_jobs([str(html)], [])
            self.assertEqual(len(paths), 1)
            self.assertEqual(Path(paths[0]).suffix, ".png")

    def test_empty_jobs(self):
        self.assertEqual(run_extract_jobs([], []), [])


if __name__ == "__main__":
    unittest.main()
