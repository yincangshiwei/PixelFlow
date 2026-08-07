"""PixelFlow 应用逻辑层（services）。

分层与依赖规则（见 TECHNICAL.md §1 分层与依赖规则）：

    ui/routes ──→ services ──→ core pure API

- 禁止：services → 具体 Route / QWidget
- 禁止：services → ui 下任何模块
- contracts 子包为跨层稳定 DTO，仅使用标准库，不得携带 Qt / PIL 对象
"""
