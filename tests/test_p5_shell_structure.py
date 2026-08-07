"""壳结构契约测试：壳 / 依赖方向。

不实例化 Qt 控件，仅做 AST / 导入级检查：
- 旧 ui/main_window.py、ui/settings_panel.py 已删除（P6），入口唯一为 ui.shell.main_window
- AppContext 不 import PySide6（只读依赖容器）
- 新路由包可导入（ui.shell / ui.routes.settings / log / changelog）
"""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _module_ast(rel_path: str) -> ast.Module:
    return ast.parse((ROOT / rel_path).read_text(encoding="utf-8"))


class LegacyEntryRemovedTests(unittest.TestCase):
    """P6：旧入口 re-export 文件已删除，入口唯一。"""

    def test_old_main_window_removed(self):
        self.assertFalse(
            (ROOT / "ui" / "main_window.py").exists(),
            "ui/main_window.py 应已删除，入口唯一为 ui.shell.main_window",
        )

    def test_old_settings_panel_removed(self):
        self.assertFalse(
            (ROOT / "ui" / "settings_panel.py").exists(),
            "ui/settings_panel.py 应已删除，配置页为 ui.routes.settings",
        )


class AppContextTests(unittest.TestCase):
    def test_no_pyside6_import(self):
        """AppContext 是纯只读容器，不依赖 Qt。"""
        tree = _module_ast("ui/shell/app_context.py")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(alias.name.startswith("PySide6"))
            elif isinstance(node, ast.ImportFrom):
                self.assertFalse((node.module or "").startswith("PySide6"))

    def test_read_only_properties(self):
        """AppContext 只暴露属性，无字符串查找 / 可变状态接口。"""
        from ui.shell.app_context import AppContext

        ctx = AppContext(
            orchestrator="o", import_service="i", log_manager="l",
        )
        self.assertEqual(ctx.orchestrator, "o")
        self.assertEqual(ctx.import_service, "i")
        self.assertEqual(ctx.log_manager, "l")
        # 属性不可写
        with self.assertRaises(AttributeError):
            ctx.orchestrator = "x"


class NewPackageImportTests(unittest.TestCase):
    """新路由包可导入（导入即校验模块语法 / 依赖完整性）。"""

    def test_import_shell_packages(self):
        import ui.shell.styles  # noqa: F401
        from ui.shell.app_context import AppContext  # noqa: F401

    def test_import_route_packages(self):
        from ui.routes.log import LogRoute  # noqa: F401
        from ui.routes.changelog import ChangelogRoute  # noqa: F401
        from ui.routes.settings import (  # noqa: F401
            DevEnvRoute,
            MattingModelRoute,
            SettingsRoute,
        )

    def test_import_shell_main_window(self):
        from ui.shell.main_window import MainWindow  # noqa: F401

    def test_styles_stylesheet_contains_resources(self):
        from ui.shell.styles import build_global_stylesheet

        css = build_global_stylesheet()
        self.assertIn("glass_panel", css)
        self.assertIn("check_on.svg", css)
        self.assertIn("radio_on.svg", css)


if __name__ == "__main__":
    unittest.main()
