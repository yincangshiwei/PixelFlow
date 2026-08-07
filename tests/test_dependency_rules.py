"""契约检查：依赖方向护栏（AST 静态扫描，不执行任何模块）。

规则（TECHNICAL.md §1 分层与依赖规则）：
- services 不得依赖 ui（Service 不 import 具体 Route / QWidget）
- services 不得 import PySide6；例外：batch_orchestrator 为 Qt 基础设施
  （QThread 生命周期 / 信号转发），仅允许 PySide6.QtCore，禁止 Widget/Gui
- services/contracts 只允许标准库（纯 DTO）
- core 不得反向依赖 ui
"""
import ast
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_ROOTS_IN_SERVICES = ("ui",)
FORBIDDEN_IN_CONTRACTS = ("PySide6", "ui", "core", "PIL")
FORBIDDEN_IN_CORE = ("ui",)

# 允许 import PySide6.QtCore 的 services 模块（相对路径，正斜杠）
SERVICES_QTCORE_WHITELIST = {
    "services/common/batch_orchestrator.py",
}


def _iter_py_files(root: Path):
    if not root.is_dir():
        return
    for f in sorted(root.rglob("*.py")):
        yield f


def _imported_roots(source: str) -> set:
    """收集一个模块顶层 import 的根包名。"""
    tree = ast.parse(source)
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


def _pyside6_modules(source: str) -> set:
    """收集一个模块 import 的 PySide6 完整模块名。"""
    tree = ast.parse(source)
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "PySide6" or alias.name.startswith("PySide6."):
                    mods.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module and (
                node.module == "PySide6" or node.module.startswith("PySide6.")
            ):
                mods.add(node.module)
    return mods


class TestDependencyDirection(unittest.TestCase):
    def assert_no_forbidden_imports(self, package: str, forbidden):
        violations = []
        for f in _iter_py_files(PROJECT_ROOT / package):
            roots = _imported_roots(f.read_text(encoding="utf-8"))
            bad = roots & set(forbidden)
            if bad:
                violations.append(f"{f.relative_to(PROJECT_ROOT)}: {sorted(bad)}")
        self.assertEqual(
            violations, [],
            f"{package} 存在违禁依赖（根包）: {violations}",
        )

    def test_services_does_not_import_ui(self):
        self.assert_no_forbidden_imports("services", FORBIDDEN_ROOTS_IN_SERVICES)

    def test_services_qt_imports_restricted(self):
        """services 仅白名单模块可 import PySide6.QtCore，其余一律禁止。"""
        violations = []
        for f in _iter_py_files(PROJECT_ROOT / "services"):
            rel = f.relative_to(PROJECT_ROOT).as_posix()
            mods = _pyside6_modules(f.read_text(encoding="utf-8"))
            if not mods:
                continue
            if rel in SERVICES_QTCORE_WHITELIST:
                bad = sorted(m for m in mods if m != "PySide6.QtCore")
                if bad:
                    violations.append(f"{rel}: {bad}")
            else:
                violations.append(f"{rel}: {sorted(mods)}")
        self.assertEqual(
            violations, [],
            f"services 存在违禁 PySide6 依赖: {violations}",
        )

    def test_contracts_are_pure_stdlib(self):
        self.assert_no_forbidden_imports(
            str(Path("services") / "contracts"), FORBIDDEN_IN_CONTRACTS
        )

    def test_core_does_not_import_ui(self):
        self.assert_no_forbidden_imports("core", FORBIDDEN_IN_CORE)


if __name__ == "__main__":
    unittest.main()
