"""ui.shell —— 应用壳 / Composition Root。

瘦壳 MainWindow 只负责布局骨架、显式创建依赖、挂载路由与跨区域连线；
不保存跨页面业务状态（见 TECHNICAL.md §1 分层与依赖规则）。
"""
from .main_window import MainWindow

__all__ = ["MainWindow"]
