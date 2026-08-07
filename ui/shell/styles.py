"""全局毛玻璃深色主题样式（P5：自 MainWindow._apply_style 迁出）。

样式语义与重构前逐字一致；资源目录由 config.RESOURCES_DIR 提供。
"""
from __future__ import annotations

from config import RESOURCES_DIR

_RES_DIR = str(RESOURCES_DIR).replace("\\", "/")


def build_global_stylesheet() -> str:
    """构建主窗口全局 QSS（毛玻璃深色主题）。"""
    return f"""
            /* ═══ 全局基础 ═══ */
            * {{
                color: #e0e4f0;
                font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
                font-size: 13px;
            }}
            QMainWindow {{
                background: transparent;
            }}
            QWidget {{
                background: transparent;
            }}

            /* ═══ 毛玻璃面板 ═══ */
            QWidget#glass_panel {{
                background: rgba(30, 30, 52, 160);
                border: 1px solid rgba(120, 130, 180, 0.15);
                border-radius: 14px;
            }}

            /* ═══ GroupBox — 内嵌毛玻璃卡片 ═══ */
            QGroupBox {{
                font-weight: bold; font-size: 13px; color: #c0c6d8;
                border: 1px solid rgba(100, 110, 170, 0.18);
                border-radius: 10px;
                margin-top: 14px; padding: 18px 12px 12px 12px;
                background: rgba(38, 38, 62, 140);
            }}
            QGroupBox::title {{
                subcontrol-origin: margin; left: 14px; padding: 2px 10px;
                background: rgba(50, 50, 80, 180);
                border-radius: 6px;
                color: #a8b0d0;
            }}
            QGroupBox::indicator {{ width: 18px; height: 18px; }}
            QGroupBox::indicator:checked {{ image: url({_RES_DIR}/check_on.svg); }}
            QGroupBox::indicator:unchecked {{ image: url({_RES_DIR}/check_off.svg); }}

            /* ═══ 按钮 — 磨砂质感 ═══ */
            QPushButton {{
                padding: 6px 14px;
                border: 1px solid rgba(100, 110, 170, 0.25);
                border-radius: 7px;
                background: rgba(50, 50, 80, 140);
                color: #d0d4e0;
            }}
            QPushButton:hover {{
                background: rgba(70, 75, 120, 160);
                border-color: rgba(100, 140, 255, 0.5);
                color: #fff;
            }}
            QPushButton:pressed {{
                background: rgba(40, 40, 70, 180);
            }}

            /* 开始处理按钮 — 渐变发光 */
            QPushButton#btn_start {{
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1,
                    stop:0 rgba(91,138,245,220), stop:1 rgba(140,110,255,220));
                color: #fff; font-weight: bold; font-size: 14px;
                border: 1px solid rgba(140, 160, 255, 0.3);
                border-radius: 9px; padding: 8px 28px;
            }}
            QPushButton#btn_start:hover {{
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1,
                    stop:0 rgba(107,154,255,240), stop:1 rgba(155,130,255,240));
                border-color: rgba(160, 180, 255, 0.5);
            }}
            QPushButton#btn_start:disabled {{
                background: rgba(50, 50, 70, 140);
                color: #555;
                border-color: rgba(80, 80, 100, 0.2);
            }}

            /* 继续 / 重试失败 */
            QPushButton#btn_resume {{
                background: rgba(55, 70, 120, 160);
                color: #d8e0ff;
                border: 1px solid rgba(100, 140, 255, 0.35);
                border-radius: 8px;
                padding: 6px 14px;
            }}
            QPushButton#btn_resume:hover {{
                background: rgba(70, 90, 150, 190);
                border-color: rgba(130, 170, 255, 0.55);
                color: #fff;
            }}
            QPushButton#btn_resume:disabled {{
                background: rgba(40, 40, 60, 100);
                color: #555;
                border-color: rgba(80, 80, 100, 0.15);
            }}

            /* Tab 按钮 */
            QPushButton#tab_active {{
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 rgba(91,138,245,200), stop:1 rgba(120,110,245,200));
                color: #fff; font-weight: bold;
                border: 1px solid rgba(130, 150, 255, 0.25);
                border-bottom: 2px solid rgba(38, 38, 62, 0);
                border-radius: 8px 8px 0 0; padding: 8px 20px;
            }}
            QPushButton#tab_inactive {{
                background: rgba(42, 42, 68, 120);
                color: #777; font-weight: normal;
                border: 1px solid rgba(80, 80, 120, 0.2);
                border-bottom: none;
                border-radius: 8px 8px 0 0; padding: 8px 20px;
            }}
            QPushButton#tab_inactive:hover {{
                color: #bbb;
                background: rgba(55, 55, 90, 140);
            }}

            /* ═══ 菜单栏 ═══ */
            QMenuBar {{
                background: rgba(22, 22, 40, 200);
                color: #b8bcd0;
                border-bottom: 1px solid rgba(100, 110, 170, 0.12);
                padding: 2px 8px;
                font-size: 12px;
            }}
            QMenuBar::item {{
                padding: 4px 10px;
                border-radius: 5px;
            }}
            QMenuBar::item:hover {{
                background: rgba(60, 65, 110, 100);
                color: #e0e4f0;
            }}
            QMenu {{
                background-color: rgba(30, 30, 55, 230);
                border: 1px solid rgba(100, 110, 170, 0.3);
                border-radius: 8px;
                padding: 4px;
                color: #e0e4f0;
            }}
            QMenu::item {{
                padding: 6px 24px;
                border-radius: 5px;
            }}
            QMenu::item:hover {{
                background-color: rgba(60, 65, 110, 100);
            }}
            QMenu::separator {{
                height: 1px;
                background: rgba(100, 110, 170, 0.2);
                margin: 4px 8px;
            }}

            /* Tab 内容区 — 与 Tab 按钮无缝衔接的卡片 */
            QWidget#tab_content_group, QGroupBox#tab_content_group {{
                background: rgba(38, 38, 62, 140);
                border: 1px solid rgba(100, 110, 170, 0.18);
                border-radius: 0 10px 10px 10px;
                margin: 0; padding: 12px;
            }}
            QGroupBox#tab_content_group {{
                margin-top: 0;
                padding-top: 12px;
            }}

            /* ═══ 输入控件 — 内凹磨砂 ═══ */
            QSpinBox, QComboBox, QLineEdit {{
                padding: 5px 10px;
                border: 1px solid rgba(90, 100, 160, 0.25);
                border-radius: 7px;
                background: rgba(22, 22, 40, 160);
                color: #e0e0e0;
                selection-background-color: #5b8af5;
            }}
            QSpinBox:focus, QComboBox:focus, QLineEdit:focus {{
                border-color: rgba(100, 150, 255, 0.6);
            }}
            QSpinBox::up-button, QSpinBox::down-button {{
                background: rgba(55, 55, 85, 140);
                border: none; width: 20px;
            }}
            QSpinBox::up-arrow {{
                border-left:4px solid transparent; border-right:4px solid transparent;
                border-bottom:5px solid #aaa;
            }}
            QSpinBox::down-arrow {{
                border-left:4px solid transparent; border-right:4px solid transparent;
                border-top:5px solid #aaa;
            }}
            QComboBox::drop-down {{
                border: none;
                background: rgba(55, 55, 85, 140);
                width: 24px; border-radius: 0 7px 7px 0;
            }}
            QComboBox::down-arrow {{
                border-left:5px solid transparent; border-right:5px solid transparent;
                border-top:5px solid #aaa;
            }}
            /* 下拉列表样式 - 确保背景色不被全局透明覆盖 */
            QComboBox QAbstractItemView {{
                background-color: rgba(30, 30, 55, 230);
                color: #e0e0e0;
                border: 1px solid rgba(100, 110, 170, 0.3);
                selection-background-color: rgba(91, 138, 245, 180);
                selection-color: #fff;
                border-radius: 6px;
                outline: none;
            }}
            QComboBox QAbstractItemView::item {{
                padding: 5px 10px;
                background-color: transparent;
            }}
            QComboBox QAbstractItemView::item:hover {{
                background-color: rgba(60, 65, 110, 100);
            }}
            QComboBox QAbstractItemView::item:selected {{
                background-color: rgba(91, 138, 245, 180);
                color: #fff;
            }}

            /* ═══ 列表 — 半透明底 ═══ */
            QListWidget {{
                border: 1px solid rgba(90, 100, 160, 0.2);
                border-radius: 8px;
                background: rgba(18, 18, 34, 140);
                color: #d0d4e0;
                outline: none;
            }}
            QListWidget::item {{
                padding: 4px 6px; border-radius: 5px;
            }}
            QListWidget::item:hover {{
                background: rgba(60, 65, 110, 100);
            }}
            QListWidget::item:selected {{
                background: rgba(80, 100, 180, 120);
                color: #fff;
            }}

            /* ═══ 日志文本框 ═══ */
            QTextEdit {{
                border: 1px solid rgba(90, 100, 160, 0.2);
                border-radius: 8px;
                background: rgba(14, 14, 28, 160);
                color: #a8b8d4;
                font-family: Consolas, "Courier New", monospace;
                font-size: 12px;
            }}

            /* ═══ 版本日志浏览器 ═══ */
            QTextBrowser {{
                border: 1px solid rgba(90, 100, 160, 0.2);
                border-radius: 8px;
                background: rgba(14, 14, 28, 160);
                color: #d0d8f0;
                font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
                font-size: 13px;
                padding: 8px;
            }}

            /* ═══ 进度条 — 发光条 ═══ */
            QProgressBar {{
                border: 1px solid rgba(90, 100, 160, 0.2);
                border-radius: 7px;
                text-align: center; height: 24px;
                background: rgba(18, 18, 36, 160);
                color: #e0e0e0;
            }}
            QProgressBar::chunk {{
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 rgba(91,138,245,220), stop:0.5 rgba(120,115,250,220), stop:1 rgba(160,120,255,200));
                border-radius: 6px;
            }}

            /* ═══ 预览区 ═══ */
            #preview_area {{
                background: rgba(18, 18, 36, 140);
                border: 1px dashed rgba(100, 110, 170, 0.3);
                border-radius: 8px;
                color: #556;
            }}

            /* ═══ Radio / CheckBox ═══ */
            QRadioButton {{ spacing: 6px; color: #c0c6d8; }}
            QRadioButton::indicator {{ width: 18px; height: 18px; }}
            QRadioButton::indicator:checked {{ image: url({_RES_DIR}/radio_on.svg); }}
            QRadioButton::indicator:unchecked {{ image: url({_RES_DIR}/radio_off.svg); }}

            QCheckBox {{ spacing: 6px; color: #c0c6d8; }}
            QCheckBox::indicator {{ width: 18px; height: 18px; }}
            QCheckBox::indicator:checked {{ image: url({_RES_DIR}/check_on.svg); }}
            QCheckBox::indicator:unchecked {{ image: url({_RES_DIR}/check_off.svg); }}

            /* ═══ 标签 & Tooltip ═══ */
            QLabel {{ color: #b8bcd0; background: transparent; }}
            QToolTip {{
                background: rgba(30, 30, 55, 230);
                color: #e0e0e0;
                border: 1px solid rgba(100, 140, 255, 0.4);
                padding: 5px; border-radius: 6px;
            }}

            /* ═══ 滚动条 — 细线发光 ═══ */
            QScrollBar:vertical {{
                background: transparent;
                width: 8px;
                margin: 4px 0;
                border-radius: 4px;
            }}
            QScrollBar::handle:vertical {{
                background: rgba(100, 110, 170, 80);
                min-height: 30px;
                border-radius: 4px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: rgba(120, 140, 220, 120);
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: transparent;
            }}
            QScrollBar:horizontal {{
                background: transparent;
                height: 8px;
                margin: 0 4px;
                border-radius: 4px;
            }}
            QScrollBar::handle:horizontal {{
                background: rgba(100, 110, 170, 80);
                min-width: 30px;
                border-radius: 4px;
            }}
            QScrollBar::handle:horizontal:hover {{
                background: rgba(120, 140, 220, 120);
            }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
                width: 0;
            }}
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
                background: transparent;
            }}
        """
