"""P3 测试：批处理日志格式化纯函数（文案与重构前逐字一致）。"""
import unittest

from services.common.batch_log_format import (
    format_duration,
    format_file_result_line,
    format_image_result_line,
    format_multiline_error_debug,
)


class FakeResult:
    def __init__(self, input_path, success=True, error="", output_path="", details=None):
        self.input_path = input_path
        self.success = success
        self.error = error
        self.output_path = output_path
        self.details = details or {}


class TestFormatDuration(unittest.TestCase):
    def test_sub_second(self):
        self.assertEqual(format_duration(0.5), "0.5 秒")

    def test_seconds_with_ms(self):
        self.assertEqual(format_duration(12.3), "12.3 秒")

    def test_minutes(self):
        self.assertEqual(format_duration(65.25), "1 分 5.25 秒（共 65.250 秒）")

    def test_hours(self):
        self.assertEqual(
            format_duration(3723.1), "1 小时 02 分 3.1 秒（共 3723.100 秒）"
        )

    def test_negative_clamped(self):
        self.assertEqual(format_duration(-1), "0 秒")


class TestImageResultLine(unittest.TestCase):
    def test_success_basic_fields(self):
        r = FakeResult(
            r"C:\img\photo.png", True,
            details={
                "trimmed_size": (100, 200),
                "canvas_size": (300, 400),
                "dpi": 300,
                "output_format": "png",
            },
        )
        line = format_image_result_line(r)
        self.assertIn("✓ photo.png", line)
        self.assertIn("裁剪→100×200", line)
        self.assertIn("画布→300×400", line)
        self.assertIn("DPI→300", line)
        self.assertIn("格式→PNG", line)

    def test_success_matting_bits(self):
        r = FakeResult(
            r"C:\img\a.jpg", True,
            details={
                "matting_model": "ben2",
                "matting_refine": True,
                "matting_batch": 2,
                "matting_device": "cuda",
                "matting_path": "refine_rgba",
                "matting_pipeline": True,
            },
        )
        line = format_image_result_line(r)
        self.assertIn("抠图→ben2+精炼", line)
        self.assertIn("batch=2", line)
        self.assertIn("CUDA", line)
        self.assertIn("精炼全尺寸", line)
        self.assertIn("流水线", line)

    def test_success_group_merge(self):
        r = FakeResult(
            "分组: 排版导出", True, output_path=r"C:\out\排版.pdf",
            details={"files_count": 5, "pages": 2},
        )
        line = format_image_result_line(r)
        self.assertIn("✓ 分组: 排版导出", line)
        self.assertIn("共 5 张图 → 2 页", line)
        self.assertIn("排版.pdf", line)

    def test_failed_first_line_only(self):
        r = FakeResult(r"C:\img\a.png", False, error="line1\nline2")
        line = format_image_result_line(r)
        self.assertEqual(line, "✗ a.png  错误: line1")

    def test_failed_empty_error(self):
        r = FakeResult(r"C:\img\a.png", False, error="")
        self.assertEqual(format_image_result_line(r), "✗ a.png  错误: ")


class TestFileResultLine(unittest.TestCase):
    def test_success_details(self):
        r = FakeResult(
            r"C:\doc\a.docx", True,
            details={"paragraphs": 10, "tables": 2, "pages": 3, "chars": 500},
        )
        line = format_file_result_line(r)
        self.assertIn("✓ a.docx", line)
        self.assertIn("段落数: 10", line)
        self.assertIn("表格数: 2", line)
        self.assertIn("页数: 3", line)
        self.assertIn("字符数: 500", line)

    def test_failed(self):
        r = FakeResult(r"C:\doc\a.pdf", False, error="parse error")
        self.assertEqual(format_file_result_line(r), "✗ a.pdf  错误: parse error")


class TestMultilineErrorDebug(unittest.TestCase):
    def test_single_line_returns_none(self):
        r = FakeResult(r"C:\img\a.png", False, error="one line")
        self.assertIsNone(format_multiline_error_debug(r))

    def test_multiline_returns_full(self):
        r = FakeResult(r"C:\img\a.png", False, error="l1\nl2")
        text = format_multiline_error_debug(r)
        self.assertIn("a.png 完整错误信息:", text)
        self.assertIn("l1\nl2", text)


if __name__ == "__main__":
    unittest.main()
