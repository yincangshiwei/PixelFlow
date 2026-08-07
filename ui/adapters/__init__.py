"""ui.adapters —— Qt 边缘适配器。

唯一允许触碰 QClipboard / QMimeData / QImage 等 Qt 对象的层；
只负责 Qt 对象 ↔ 普通 Python 数据的转换，不写业务规则。
"""
