"""
配置中心面板 —— 左侧菜单 + 右侧内容
菜单：开发环境 → 抠图模型配置
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QObject, Signal, Slot, QTimer, QThread
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel, QPushButton,
    QComboBox, QLineEdit, QListWidget, QListWidgetItem, QStackedWidget,
    QFileDialog, QProgressBar, QTextEdit, QMessageBox,
    QScrollArea, QFrame, QSizePolicy,
)

import config
from core.matting.hardware import detect_hardware, evaluate_model, HardwareInfo
from core.matting.model_manager import get_matting_manager
from core.matting.model_registry import list_models, get_model_info
from core.matting.inference import clear_model_cache
from core.runtime.env_manager import get_runtime_manager, UvInfo, ModelEnvStatus


def _get_desktop_path() -> str:
    return str(Path.home() / "Desktop")


class _AsyncBridge(QObject):
    """后台线程回调 → UI 线程"""
    progress = Signal(str, float, str)          # key, percent, message
    finished = Signal(str, bool, str)           # key, ok, message
    dev_snapshot = Signal(object, object, str)  # pys, uv, diagnose
    model_env_snapshot = Signal(str, object)    # model_id, ModelEnvStatus
    hw_snapshot = Signal(object)                # HardwareInfo


class _DevScanWorker(QThread):
    """后台扫描 Python / uv，避免阻塞 UI"""
    done = Signal(object, object, str)

    def __init__(self, force: bool = False, parent=None):
        super().__init__(parent)
        self.force = force

    def run(self):
        rt = get_runtime_manager()
        if self.force:
            rt.invalidate_caches(pythons=True, uv=True)
        pys = rt.discover_pythons(force=self.force)
        uv = rt.resolve_uv(force=self.force)
        diag = rt.diagnose_text()
        self.done.emit(pys, uv, diag)


class _EnvCheckWorker(QThread):
    """后台校验模型环境依赖（真实 import）"""
    done = Signal(str, object)

    def __init__(self, model_id: str, packages: list[str] | None = None, parent=None):
        super().__init__(parent)
        self.model_id = model_id
        self.packages = packages

    def run(self):
        rt = get_runtime_manager()
        # 使用注册表完整检测列表；写回 meta，纠正历史脏数据
        st = rt.get_model_env_status(self.model_id, self.packages, force=True)
        try:
            from core.matting.model_registry import get_model_info
            meta = get_model_info(self.model_id)
            pkgs = list(meta.env_packages) if meta else []
            rt.write_env_meta(self.model_id, packages=pkgs, status=st)
        except Exception:
            pass
        self.done.emit(self.model_id, st)


class _HwDetectWorker(QThread):
    done = Signal(object)

    def run(self):
        self.done.emit(detect_hardware())


class SettingsPanel(QWidget):
    """主窗口「配置」Tab 的内容区"""

    def __init__(self, parent=None, lazy: bool = False):
        super().__init__(parent)
        self._mgr = get_matting_manager()
        self._rt = get_runtime_manager()
        self._bridge = _AsyncBridge(self)
        self._bridge.progress.connect(self._on_async_progress)
        self._bridge.finished.connect(self._on_async_finished)
        self._async_kind = ""  # uv / env / download
        self._dev_loaded = False
        self._model_loaded = False
        self._hw_loaded = False
        self._scan_worker: QThread | None = None
        self._env_worker: QThread | None = None
        self._hw_worker: QThread | None = None
        self._build_ui()
        # 不在构造时做重扫描；仅展示占位，切入菜单时再异步刷新
        self._apply_dev_placeholder()
        self._apply_model_placeholder()
        if not lazy:
            QTimer.singleShot(50, lambda: self._schedule_refresh(0))

    # ── UI 构建 ──
    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        left = QWidget()
        left.setFixedWidth(168)
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(6)

        title = QLabel("配置菜单")
        title.setStyleSheet(
            "color:#a8b0d0;font-size:12px;font-weight:bold;padding:4px 2px;"
        )
        left_lay.addWidget(title)

        self.menu_list = QListWidget()
        self.menu_list.setObjectName("settings_menu")
        self.menu_list.addItem(QListWidgetItem("🛠  开发环境"))
        self.menu_list.addItem(QListWidgetItem("✂  抠图模型配置"))
        self.menu_list.setCurrentRow(0)
        self.menu_list.currentRowChanged.connect(self._on_menu_changed)
        left_lay.addWidget(self.menu_list, 1)
        root.addWidget(left)

        self.content_stack = QStackedWidget()
        self.content_stack.addWidget(self._build_dev_page())       # 0
        self.content_stack.addWidget(self._build_matting_page())   # 1
        root.addWidget(self.content_stack, 1)

    def _wrap_scroll(self, body: QWidget) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("background: transparent;")
        scroll.setWidget(body)
        outer.addWidget(scroll)
        return page

    # ═══ 开发环境 ═══
    def _build_dev_page(self) -> QWidget:
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(4, 2, 8, 8)
        lay.setSpacing(10)

        tip = QLabel(
            "AI 抠图等重型依赖不装进主程序，而是用 <b>uv</b> 为每个模型创建独立虚拟环境，"
            "通过子进程调用。这样打包体积不受影响，且不同模型依赖互不冲突。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#a8b0d0;font-size:12px;")
        lay.addWidget(tip)

        # Python
        grp_py = QGroupBox("系统 Python")
        gp = QVBoxLayout(grp_py)
        gp.setSpacing(8)
        row = QHBoxLayout()
        row.addWidget(QLabel("基础解释器:"))
        self.combo_python = QComboBox()
        self.combo_python.setStyleSheet(config.COMBOBOX_STYLE)
        self.combo_python.setMinimumWidth(320)
        row.addWidget(self.combo_python, 1)
        self.btn_browse_py = QPushButton("浏览…")
        self.btn_browse_py.clicked.connect(self._browse_python)
        row.addWidget(self.btn_browse_py)
        self.btn_apply_py = QPushButton("设为默认")
        self.btn_apply_py.clicked.connect(self._apply_python)
        row.addWidget(self.btn_apply_py)
        gp.addLayout(row)

        self.lbl_py_hint = QLabel("")
        self.lbl_py_hint.setWordWrap(True)
        self.lbl_py_hint.setStyleSheet("color:#8a90b0;font-size:12px;")
        gp.addWidget(self.lbl_py_hint)

        py_btn = QHBoxLayout()
        self.btn_refresh_py = QPushButton("重新检测 Python")
        self.btn_refresh_py.clicked.connect(lambda: self._schedule_dev_refresh(force=True))
        py_btn.addWidget(self.btn_refresh_py)
        self.btn_open_python_org = QPushButton("打开 Python 下载页")
        self.btn_open_python_org.clicked.connect(
            lambda: self._open_url("https://www.python.org/downloads/")
        )
        py_btn.addWidget(self.btn_open_python_org)
        py_btn.addStretch()
        gp.addLayout(py_btn)
        lay.addWidget(grp_py)

        # uv
        grp_uv = QGroupBox("包管理工具 uv")
        gu = QVBoxLayout(grp_uv)
        gu.setSpacing(8)
        self.lbl_uv = QLabel("状态: —")
        self.lbl_uv.setStyleSheet("color:#c0c6d8;")
        gu.addWidget(self.lbl_uv)

        uv_row = QHBoxLayout()
        self.btn_install_uv = QPushButton("安装 / 修复 uv")
        self.btn_install_uv.setObjectName("btn_start")
        self.btn_install_uv.setMinimumHeight(34)
        self.btn_install_uv.clicked.connect(self._install_uv)
        uv_row.addWidget(self.btn_install_uv)
        self.btn_open_runtime = QPushButton("打开 runtime 目录")
        self.btn_open_runtime.clicked.connect(self._open_runtime_dir)
        uv_row.addWidget(self.btn_open_runtime)
        uv_row.addStretch()
        gu.addLayout(uv_row)

        self.progress_dev = QProgressBar()
        self.progress_dev.setRange(0, 100)
        self.progress_dev.setValue(0)
        gu.addWidget(self.progress_dev)
        self.lbl_dev_msg = QLabel("")
        self.lbl_dev_msg.setWordWrap(True)
        self.lbl_dev_msg.setStyleSheet("color:#a8b8d4;font-size:12px;")
        gu.addWidget(self.lbl_dev_msg)
        lay.addWidget(grp_uv)

        # 诊断
        grp_diag = QGroupBox("环境诊断")
        gd = QVBoxLayout(grp_diag)
        self.txt_diag = QTextEdit()
        self.txt_diag.setReadOnly(True)
        self.txt_diag.setMinimumHeight(200)
        gd.addWidget(self.txt_diag)
        lay.addWidget(grp_diag)
        lay.addStretch()
        return self._wrap_scroll(body)

    # ═══ 抠图模型 ═══
    def _build_matting_page(self) -> QWidget:
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(4, 2, 8, 8)
        lay.setSpacing(10)

        # 模型选择
        grp_model = QGroupBox("抠图模型")
        gm = QVBoxLayout(grp_model)
        gm.setSpacing(8)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("当前模型:"))
        self.combo_model = QComboBox()
        self.combo_model.setStyleSheet(config.COMBOBOX_STYLE)
        for m in list_models():
            self.combo_model.addItem(m.name, m.id)
        self.combo_model.currentIndexChanged.connect(self._on_model_combo_changed)
        row1.addWidget(self.combo_model)
        row1.addWidget(QLabel("推理设备:"))
        self.combo_device = QComboBox()
        self.combo_device.setStyleSheet(config.COMBOBOX_STYLE)
        self.combo_device.addItem("自动 (优先 GPU)", "auto")
        self.combo_device.addItem("CUDA (GPU)", "cuda")
        self.combo_device.addItem("CPU", "cpu")
        self.combo_device.currentIndexChanged.connect(self._on_device_changed)
        row1.addWidget(self.combo_device)
        row1.addStretch()
        gm.addLayout(row1)

        self.lbl_model_desc = QLabel("")
        self.lbl_model_desc.setWordWrap(True)
        self.lbl_model_desc.setStyleSheet("color:#a8b0d0;font-size:12px;")
        gm.addWidget(self.lbl_model_desc)

        row_meta = QHBoxLayout()
        self.lbl_weight_status = QLabel("权重: —")
        row_meta.addWidget(self.lbl_weight_status)
        self.lbl_size = QLabel("")
        self.lbl_size.setStyleSheet("color:#8a90b0;font-size:12px;")
        row_meta.addWidget(self.lbl_size)
        row_meta.addStretch()
        self.btn_open_page = QPushButton("打开模型主页")
        self.btn_open_page.clicked.connect(self._open_model_page)
        row_meta.addWidget(self.btn_open_page)
        gm.addLayout(row_meta)
        lay.addWidget(grp_model)

        # 隔离环境
        grp_env = QGroupBox("模型隔离环境（uv）")
        ge = QVBoxLayout(grp_env)
        ge.setSpacing(8)
        env_tip = QLabel(
            "每个模型使用独立虚拟环境，避免依赖版本冲突。"
            "创建环境会安装 torch / ben2 等，体积较大、耗时较长，请保持网络畅通。"
        )
        env_tip.setWordWrap(True)
        env_tip.setStyleSheet("color:#8a90b0;font-size:12px;")
        ge.addWidget(env_tip)

        self.lbl_env_status = QLabel("环境: —")
        self.lbl_env_status.setWordWrap(True)
        ge.addWidget(self.lbl_env_status)

        self.lbl_env_path = QLabel("")
        self.lbl_env_path.setStyleSheet("color:#8a90b0;font-size:11px;")
        self.lbl_env_path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        ge.addWidget(self.lbl_env_path)

        env_btn = QHBoxLayout()
        self.btn_setup_env = QPushButton("创建 / 修复环境")
        self.btn_setup_env.setObjectName("btn_start")
        self.btn_setup_env.setMinimumHeight(34)
        self.btn_setup_env.clicked.connect(lambda: self._setup_model_env(False))
        env_btn.addWidget(self.btn_setup_env)
        self.btn_recreate_env = QPushButton("强制重建环境")
        self.btn_recreate_env.clicked.connect(lambda: self._setup_model_env(True))
        env_btn.addWidget(self.btn_recreate_env)
        self.btn_open_env = QPushButton("打开环境目录")
        self.btn_open_env.clicked.connect(self._open_env_folder)
        env_btn.addWidget(self.btn_open_env)
        env_btn.addStretch()
        ge.addLayout(env_btn)

        self.progress_env = QProgressBar()
        self.progress_env.setRange(0, 100)
        self.progress_env.setValue(0)
        ge.addWidget(self.progress_env)
        self.lbl_env_msg = QLabel("")
        self.lbl_env_msg.setWordWrap(True)
        self.lbl_env_msg.setStyleSheet("color:#a8b8d4;font-size:12px;")
        ge.addWidget(self.lbl_env_msg)
        lay.addWidget(grp_env)

        # 权重下载
        grp_dl = QGroupBox("本地模型权重")
        gd = QVBoxLayout(grp_dl)
        gd.setSpacing(8)
        tip = QLabel(
            "从 ModelScope 下载权重到 models/matting/；也可指定已有权重文件/文件夹。"
            "下载依赖模型隔离环境中的 modelscope。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#8a90b0;font-size:12px;")
        gd.addWidget(tip)

        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("本地路径:"))
        self.txt_custom_path = QLineEdit()
        self.txt_custom_path.setPlaceholderText("留空=默认下载目录；可填权重文件或文件夹")
        path_row.addWidget(self.txt_custom_path, 1)
        self.btn_browse_path = QPushButton("浏览…")
        self.btn_browse_path.clicked.connect(self._browse_custom_path)
        path_row.addWidget(self.btn_browse_path)
        self.btn_save_path = QPushButton("保存路径")
        self.btn_save_path.clicked.connect(self._save_custom_path)
        path_row.addWidget(self.btn_save_path)
        gd.addLayout(path_row)

        self.lbl_default_dir = QLabel("")
        self.lbl_default_dir.setStyleSheet("color:#8a90b0;font-size:11px;")
        self.lbl_default_dir.setTextInteractionFlags(Qt.TextSelectableByMouse)
        gd.addWidget(self.lbl_default_dir)

        btn_row = QHBoxLayout()
        self.btn_download = QPushButton("下载权重")
        self.btn_download.setMinimumHeight(34)
        self.btn_download.clicked.connect(self._start_download)
        btn_row.addWidget(self.btn_download)
        self.btn_open_folder = QPushButton("打开权重目录")
        self.btn_open_folder.clicked.connect(self._open_model_folder)
        btn_row.addWidget(self.btn_open_folder)
        self.btn_refresh_status = QPushButton("刷新状态")
        self.btn_refresh_status.clicked.connect(
            lambda: self._schedule_model_refresh(force_env=True)
        )
        btn_row.addWidget(self.btn_refresh_status)
        btn_row.addStretch()
        gd.addLayout(btn_row)

        self.progress_dl = QProgressBar()
        self.progress_dl.setRange(0, 100)
        self.progress_dl.setValue(0)
        gd.addWidget(self.progress_dl)
        self.lbl_dl_msg = QLabel("")
        self.lbl_dl_msg.setWordWrap(True)
        self.lbl_dl_msg.setStyleSheet("color:#a8b8d4;font-size:12px;")
        gd.addWidget(self.lbl_dl_msg)
        lay.addWidget(grp_dl)

        # 硬件
        grp_hw = QGroupBox("电脑配置检测")
        gh = QVBoxLayout(grp_hw)
        gh.setSpacing(8)
        hw_btn_row = QHBoxLayout()
        self.btn_detect = QPushButton("重新检测")
        self.btn_detect.clicked.connect(lambda: self._run_hardware_detect(async_=True))
        hw_btn_row.addWidget(self.btn_detect)
        self.lbl_compat = QLabel("")
        self.lbl_compat.setStyleSheet("font-weight:bold;color:#c0c6d8;")
        # 短状态文案；详情只写下方文本框，避免撑出横向滚动条
        self.lbl_compat.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        hw_btn_row.addWidget(self.lbl_compat)
        hw_btn_row.addStretch()
        gh.addLayout(hw_btn_row)
        self.txt_hw = QTextEdit()
        self.txt_hw.setReadOnly(True)
        self.txt_hw.setMinimumHeight(150)
        self.txt_hw.setMaximumHeight(220)
        gh.addWidget(self.txt_hw)
        lay.addWidget(grp_hw)
        lay.addStretch()
        return self._wrap_scroll(body)

    # ── 菜单 ──
    def _on_menu_changed(self, row: int):
        if row < 0:
            return
        self.content_stack.setCurrentIndex(row)
        # 延迟一帧再刷，保证 stacked 切换先完成、不卡顿
        QTimer.singleShot(0, lambda r=row: self._schedule_refresh(r))

    def _schedule_refresh(self, row: int):
        if row == 0:
            self._schedule_dev_refresh(force=False)
        elif row == 1:
            self._schedule_model_refresh(force_env=False)
            if not self._hw_loaded:
                self._run_hardware_detect(async_=True)

    def _current_model_id(self) -> str:
        mid = self.combo_model.currentData()
        return mid or "ben2"

    def _apply_dev_placeholder(self):
        self.lbl_py_hint.setText("检测将在后台进行…")
        self.lbl_py_hint.setStyleSheet("color:#8a90b0;font-size:12px;")
        self.lbl_uv.setText("状态: 检测中…")
        self.lbl_uv.setStyleSheet("color:#8a90b0;")
        self.txt_diag.setPlainText("正在后台扫描开发环境…")

    def _apply_model_placeholder(self):
        self.lbl_env_status.setText("环境: …")
        self.lbl_env_status.setStyleSheet("color:#8a90b0;")

    # ── 开发环境刷新（异步）──
    def _schedule_dev_refresh(self, force: bool = False):
        # 有缓存且非强制：直接画 UI，零等待
        if not force and self._rt._py_list_cache is not None and self._rt._uv_cache is not None:
            self._apply_dev_snapshot(
                self._rt.discover_pythons(),
                self._rt.resolve_uv(),
                self._rt.diagnose_text(),
            )
            return
        if self._scan_worker is not None and self._scan_worker.isRunning():
            return
        self.lbl_dev_msg.setText("正在后台检测 Python / uv…")
        self.btn_refresh_py.setEnabled(False)
        worker = _DevScanWorker(force=force, parent=self)
        self._scan_worker = worker
        worker.done.connect(self._on_dev_scan_done)
        worker.finished.connect(lambda: self.btn_refresh_py.setEnabled(True))
        worker.start()

    @Slot(object, object, str)
    def _on_dev_scan_done(self, pys, uv, diag: str):
        self._apply_dev_snapshot(pys, uv, diag)
        self.lbl_dev_msg.setText("")

    def _apply_dev_snapshot(self, pys, uv: UvInfo, diag: str):
        self._dev_loaded = True
        saved = self._rt.get_saved_python_path()
        self.combo_python.blockSignals(True)
        self.combo_python.clear()
        sel = 0
        for i, p in enumerate(pys or []):
            label = p.display
            if self._rt._version_ok(p, self._rt.DEFAULT_PY_MIN, self._rt.DEFAULT_PY_MAX):
                label += "  ✓"
            self.combo_python.addItem(label, p.path)
            if saved:
                try:
                    if Path(saved).resolve() == Path(p.path).resolve():
                        sel = i
                except Exception:
                    pass
        if not pys:
            self.combo_python.addItem("（未检测到 Python 3）", "")
        self.combo_python.setCurrentIndex(sel)
        self.combo_python.blockSignals(False)

        if pys:
            self.lbl_py_hint.setText(
                "推荐 64 位 Python 3.10–3.12。将用于创建各模型的 uv 虚拟环境，"
                "不会修改你的系统全局 site-packages。"
            )
            self.lbl_py_hint.setStyleSheet("color:#8a90b0;font-size:12px;")
        else:
            self.lbl_py_hint.setText(
                "未检测到可用 Python。请安装 3.10–3.12（64 位），安装时勾选 "
                "“Add python.exe to PATH”，然后点「重新检测」。"
            )
            self.lbl_py_hint.setStyleSheet("color:#e0a060;font-size:12px;")

        if uv and uv.found:
            self.lbl_uv.setText(f"状态: 已就绪  ·  {uv.display}")
            self.lbl_uv.setStyleSheet("color:#6dcea0;font-weight:bold;")
            self.btn_install_uv.setText("重新安装 uv")
        else:
            self.lbl_uv.setText("状态: 未安装 — 可一键下载到 runtime/uv/")
            self.lbl_uv.setStyleSheet("color:#e0a060;font-weight:bold;")
            self.btn_install_uv.setText("安装 uv")

        self.txt_diag.setPlainText(diag or "")

    # ── 模型区刷新（轻量同步 + 可选后台校验）──
    def _schedule_model_refresh(self, force_env: bool = False):
        self._refresh_model_section_light()
        # 进入模型页或手动刷新时，后台做一次真实依赖检测
        if force_env or not getattr(self, "_env_verified_once", False):
            self._schedule_env_check()

    def _refresh_model_section_light(self):
        """只做路径/JSON 级读取，不阻塞 UI。"""
        mid = self._current_model_id()
        meta = get_model_info(mid)
        mgr = self._mgr

        default_id = mgr.get_default_model_id()
        for i in range(self.combo_model.count()):
            if self.combo_model.itemData(i) == default_id:
                if self.combo_model.currentIndex() != i:
                    self.combo_model.blockSignals(True)
                    self.combo_model.setCurrentIndex(i)
                    self.combo_model.blockSignals(False)
                break

        pref = mgr.get_device_preference()
        for i in range(self.combo_device.count()):
            if self.combo_device.itemData(i) == pref:
                self.combo_device.blockSignals(True)
                self.combo_device.setCurrentIndex(i)
                self.combo_device.blockSignals(False)
                break

        if meta:
            self.lbl_model_desc.setText(meta.description)
            self.lbl_size.setText(f"约 {meta.approx_size_mb} MB  ·  {meta.repo_id}")
            deps = ", ".join(meta.env_check_packages) if meta.env_check_packages else ""
            if deps:
                self.lbl_env_path.setToolTip(f"将检测依赖: {deps}")
        else:
            self.lbl_model_desc.setText("")
            self.lbl_size.setText("")

        w_ready = mgr.is_ready(mid)
        w_text = mgr.status_text(mid)
        w_color = "#6dcea0" if w_ready else "#e0a060"
        if mgr.is_downloading(mid):
            w_color = "#5b8af5"
        self.lbl_weight_status.setText(f"权重: {w_text}")
        self.lbl_weight_status.setStyleSheet(f"color:{w_color};font-weight:bold;")

        self.lbl_default_dir.setText(f"默认下载目录: {mgr.default_local_dir(mid)}")
        custom = mgr.get_custom_path(mid)
        if self.txt_custom_path.text() != custom:
            self.txt_custom_path.setText(custom)

        # 快速环境状态（无子进程）；完整结果由后台 worker 覆盖
        st = self._rt.get_model_env_status(mid, quick=True)
        self._apply_env_status(st, pending_verify=True)

        self.btn_setup_env.setEnabled(not self._rt.is_busy)
        self.btn_recreate_env.setEnabled(not self._rt.is_busy)
        self.btn_download.setEnabled(not mgr.is_downloading(mid))
        self.btn_download.setText(
            "下载中…" if mgr.is_downloading(mid) else "下载权重"
        )
        self._model_loaded = True

    def _schedule_env_check(self):
        mid = self._current_model_id()
        if self._env_worker is not None and self._env_worker.isRunning():
            return
        self.lbl_env_msg.setText("正在后台校验完整依赖（torch / ben2 等）…")
        worker = _EnvCheckWorker(mid, None, parent=self)
        self._env_worker = worker
        worker.done.connect(self._on_env_check_done)
        worker.start()

    @Slot(str, object)
    def _on_env_check_done(self, model_id: str, st: ModelEnvStatus):
        if model_id != self._current_model_id():
            return
        self._env_verified_once = True
        self._apply_env_status(st, pending_verify=False)
        if st.ready:
            self.lbl_env_msg.setText(st.detail or "依赖校验通过")
        else:
            miss = ", ".join(st.missing_packages) if st.missing_packages else st.detail
            self.lbl_env_msg.setText(
                f"依赖未齐: {miss}。请点击「创建/修复环境」安装完整依赖。"
            )

    def _apply_env_status(self, st: ModelEnvStatus, pending_verify: bool = False):
        # 清理可能的脏字符
        detail = (st.detail or "").replace("\x1b", "").strip()
        if st.ready:
            ver = f"  ·  Python {st.python_version}" if st.python_version else ""
            self.lbl_env_status.setText(f"环境: 就绪{ver}")
            self.lbl_env_status.setStyleSheet("color:#6dcea0;font-weight:bold;")
        elif st.python_path:
            if pending_verify and "缺少" not in detail:
                self.lbl_env_status.setText("环境: 已创建 · 依赖校验中…")
                self.lbl_env_status.setStyleSheet("color:#8a90b0;font-weight:bold;")
            else:
                # 只显示干净的缺失包名
                if st.missing_packages:
                    miss = ", ".join(
                        m for m in st.missing_packages
                        if m.isidentifier() or all(c.isalnum() or c in "-_." for c in m)
                    )
                    text = f"环境: 缺少 {miss}" if miss else f"环境: {detail or '不完整'}"
                else:
                    text = f"环境: {detail or '已创建'}"
                # 截断，避免撑宽
                if len(text) > 48:
                    text = text[:45] + "…"
                self.lbl_env_status.setText(text)
                self.lbl_env_status.setStyleSheet("color:#e0c060;font-weight:bold;")
                self.lbl_env_status.setToolTip(detail)
        else:
            self.lbl_env_status.setText("环境: 未创建")
            self.lbl_env_status.setStyleSheet("color:#e0a060;font-weight:bold;")
        self.lbl_env_path.setText(f"环境目录: {st.env_dir}")

    # 兼容旧方法名
    def _refresh_dev_section(self):
        self._schedule_dev_refresh(force=True)

    def _refresh_model_section(self):
        self._schedule_model_refresh(force_env=False)

    # ── 开发环境操作 ──
    def _browse_python(self):
        start = _get_desktop_path()
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 python.exe",
            start,
            "Python (python.exe);;所有文件 (*.*)",
        )
        if path:
            self._rt.set_python_path(path)
            self._schedule_dev_refresh(force=True)

    def _apply_python(self):
        path = self.combo_python.currentData() or ""
        if not path:
            QMessageBox.warning(self, "无效", "请先选择或浏览一个 Python 解释器")
            return
        self._rt.set_python_path(path)
        QMessageBox.information(self, "已保存", f"已设为默认基础 Python:\n{path}")
        self._schedule_dev_refresh(force=True)

    def _install_uv(self):
        if self._rt.is_busy:
            QMessageBox.information(self, "请稍候", "已有任务进行中")
            return
        self._async_kind = "uv"
        self.progress_dev.setValue(0)
        self.lbl_dev_msg.setText("开始安装 uv…")
        self.btn_install_uv.setEnabled(False)
        bridge = self._bridge

        def prog(stage, pct, msg):
            bridge.progress.emit("uv", float(pct), msg)

        def work():
            ok, msg = False, ""
            try:
                info = self._rt.install_uv(progress=prog)
                ok, msg = True, f"安装成功: {info.display}"
            except Exception as e:
                ok, msg = False, str(e)
            bridge.finished.emit("uv", ok, msg)

        import threading
        threading.Thread(target=work, daemon=True).start()

    def _open_runtime_dir(self):
        d = self._rt.runtime_root()
        self._open_path(d)

    def _open_url(self, url: str):
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        QDesktopServices.openUrl(QUrl(url))

    # ── 模型环境 ──
    def _setup_model_env(self, force: bool):
        mid = self._current_model_id()
        meta = get_model_info(mid)
        if meta is None:
            return
        if self._rt.is_busy:
            QMessageBox.information(self, "请稍候", "已有环境任务进行中")
            return

        # 前置检查
        uv = self._rt.resolve_uv()
        if not uv.found:
            ret = QMessageBox.question(
                self,
                "需要 uv",
                "尚未安装 uv，是否先自动安装？",
            )
            if ret != QMessageBox.Yes:
                return
            self.menu_list.setCurrentRow(0)
            self._install_uv()
            return

        base = self._rt.resolve_base_python()
        if base is None:
            QMessageBox.warning(
                self,
                "需要 Python",
                "未找到可用的 Python 3.10–3.12（64 位）。\n"
                "请到「开发环境」安装/选择 Python 后再试。",
            )
            self.menu_list.setCurrentRow(0)
            return

        if force:
            ret = QMessageBox.question(
                self,
                "强制重建",
                f"将删除并重建模型 {meta.name} 的隔离环境，是否继续？",
            )
            if ret != QMessageBox.Yes:
                return

        self._async_kind = "env"
        self.progress_env.setValue(0)
        self.lbl_env_msg.setText("准备创建环境…")
        self.btn_setup_env.setEnabled(False)
        self.btn_recreate_env.setEnabled(False)
        bridge = self._bridge

        def prog(stage, pct, msg):
            bridge.progress.emit("env", float(pct), msg)

        def fin(ok, msg, st):
            bridge.finished.emit("env", ok, msg)

        self._rt.ensure_model_env_async(
            mid,
            python_version=meta.python_version,
            packages=list(meta.env_packages),
            progress=prog,
            finished=fin,
            force_recreate=force,
        )

    def _open_env_folder(self):
        d = self._rt.model_env_dir(self._current_model_id())
        d.mkdir(parents=True, exist_ok=True)
        self._open_path(d)

    # ── 模型事件 ──
    def _on_model_combo_changed(self, _idx: int):
        mid = self._current_model_id()
        self._mgr.set_default_model_id(mid)
        clear_model_cache()
        self._schedule_model_refresh(force_env=False)
        self._update_compat_label()

    def _on_device_changed(self, _idx: int):
        dev = self.combo_device.currentData() or "auto"
        self._mgr.set_device_preference(dev)
        clear_model_cache()

    def _browse_custom_path(self):
        start = self.txt_custom_path.text().strip() or _get_desktop_path()
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择模型权重文件（可取消后改选文件夹）",
            start,
            "模型权重 (*.safetensors *.pth *.pt *.bin);;所有文件 (*.*)",
        )
        if path:
            self.txt_custom_path.setText(path)
            return
        folder = QFileDialog.getExistingDirectory(self, "选择模型文件夹", start)
        if folder:
            self.txt_custom_path.setText(folder)

    def _save_custom_path(self):
        mid = self._current_model_id()
        self._mgr.set_custom_path(mid, self.txt_custom_path.text().strip())
        clear_model_cache()
        self._schedule_model_refresh(force_env=False)
        QMessageBox.information(self, "已保存", "本地路径已保存。")

    def _open_model_page(self):
        meta = get_model_info(self._current_model_id())
        if meta:
            self._open_url(meta.page_url)

    def _open_model_folder(self):
        d = self._mgr.open_model_folder(self._current_model_id())
        self._open_path(d)

    def _open_path(self, d: Path):
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", str(d)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(d)])
            else:
                subprocess.Popen(["xdg-open", str(d)])
        except Exception as e:
            QMessageBox.warning(self, "打开失败", str(e))

    def _start_download(self):
        mid = self._current_model_id()
        meta = get_model_info(mid)
        if meta is None:
            return

        # 仅检查环境是否存在；完整依赖由下载时子进程报错提示
        if not self._rt.env_exists(mid):
            QMessageBox.warning(
                self,
                "请先创建环境",
                "下载权重需要模型隔离环境中的 modelscope。\n"
                "请先点击「创建 / 修复环境」。",
            )
            return

        if self._mgr.is_ready(mid):
            ret = QMessageBox.question(
                self,
                "重新下载",
                f"模型 {meta.name} 权重已就绪，是否仍要重新下载？",
            )
            if ret != QMessageBox.Yes:
                return

        self._async_kind = "download"
        self.progress_dl.setValue(0)
        self.lbl_dl_msg.setText("准备下载…")
        self.btn_download.setEnabled(False)
        self.btn_download.setText("下载中…")
        bridge = self._bridge

        def on_prog(model_id, percent, message):
            bridge.progress.emit("download", float(percent), message)

        def on_fin(model_id, ok, message):
            bridge.finished.emit("download", ok, message)

        self._mgr.download_async(mid, progress=on_prog, finished=on_fin)

    # ── 异步回调 ──
    @Slot(str, float, str)
    def _on_async_progress(self, key: str, percent: float, message: str):
        if key == "uv":
            if percent >= 0:
                self.progress_dev.setValue(int(min(100, max(0, percent))))
            self.lbl_dev_msg.setText(message)
        elif key == "env":
            if percent >= 0:
                self.progress_env.setValue(int(min(100, max(0, percent))))
            self.lbl_env_msg.setText(message)
        elif key == "download":
            if percent >= 0:
                self.progress_dl.setValue(int(min(100, max(0, percent))))
            self.lbl_dl_msg.setText(message)

    @Slot(str, bool, str)
    def _on_async_finished(self, key: str, ok: bool, message: str):
        if key == "uv":
            self.btn_install_uv.setEnabled(True)
            self._rt.invalidate_caches(uv=True)
            self._schedule_dev_refresh(force=True)
            if ok:
                self.progress_dev.setValue(100)
                self.lbl_dev_msg.setText(message)
                QMessageBox.information(self, "uv 就绪", message)
            else:
                self.lbl_dev_msg.setText(f"失败: {message}")
                QMessageBox.warning(self, "安装失败", message)
        elif key == "env":
            self.btn_setup_env.setEnabled(True)
            self.btn_recreate_env.setEnabled(True)
            self._rt.invalidate_caches(env=self._current_model_id())
            self._schedule_model_refresh(force_env=True)
            if ok:
                self.progress_env.setValue(100)
                self.lbl_env_msg.setText(message)
                QMessageBox.information(
                    self, "环境就绪",
                    f"{message}\n\n接下来可下载模型权重，然后在透明图处理中启用 AI 抠图。",
                )
            else:
                self.lbl_env_msg.setText(f"失败: {message}")
                QMessageBox.warning(self, "环境创建失败", message)
        elif key == "download":
            self._schedule_model_refresh(force_env=False)
            if ok:
                self.progress_dl.setValue(100)
                self.lbl_dl_msg.setText(message)
                clear_model_cache()
                QMessageBox.information(self, "下载完成", f"{message}\n权重已就绪。")
            else:
                self.lbl_dl_msg.setText(f"失败: {message}")
                QMessageBox.warning(self, "下载失败", message)

    # ── 硬件（异步）──
    def _run_hardware_detect(self, async_: bool = True):
        if not async_:
            self._apply_hw(detect_hardware())
            return
        if self._hw_worker is not None and self._hw_worker.isRunning():
            return
        self.txt_hw.setPlainText("正在后台检测硬件…")
        worker = _HwDetectWorker(parent=self)
        self._hw_worker = worker
        worker.done.connect(self._apply_hw)
        worker.start()

    @Slot(object)
    def _apply_hw(self, hw: HardwareInfo):
        self._hw_loaded = True
        lines = [
            f"操作系统: {hw.os_name}",
            f"CPU: {hw.cpu_name or '未知'}  ·  逻辑核心 {hw.cpu_cores or '?'}",
            f"内存: 总计 {hw.ram_total_gb:.1f} GB  /  可用 {hw.ram_available_gb:.1f} GB",
            "",
            "— 主程序进程（不含 AI 依赖，属正常）—",
            f"PyTorch(主进程): {'已安装 ' + hw.torch_version if hw.torch_installed else '未安装（AI 在隔离环境中运行）'}",
            f"CUDA(主进程探测): {'可用 ' + (hw.cuda_version or '') if hw.cuda_available else '不可用 / 未装 torch'}",
        ]
        if hw.gpu_count:
            for i, name in enumerate(hw.gpu_names):
                vram = hw.gpu_vram_gb[i] if i < len(hw.gpu_vram_gb) else 0
                lines.append(f"GPU[{i}]: {name}  ·  显存约 {vram:.1f} GB")
        else:
            lines.append("GPU: 主进程未检测到 CUDA 设备（隔离环境内的 torch 仍可能使用 GPU）")

        if hw.errors:
            lines.append("")
            for e in hw.errors:
                lines.append(f"  · {e}")

        self.txt_hw.setPlainText("\n".join(lines))
        self._last_hw = hw
        self._update_compat_label()

    def _update_compat_label(self):
        hw: HardwareInfo | None = getattr(self, "_last_hw", None)
        if hw is None:
            self.lbl_compat.setText("检测中…")
            return
        mid = self._current_model_id()
        compat = evaluate_model(mid, hw)
        color_map = {
            "excellent": "#6dcea0",
            "good": "#6dcea0",
            "acceptable": "#e0c060",
            "poor": "#e0a060",
            "unsupported": "#e07070",
        }
        color = color_map.get(compat.level, "#c0c6d8")
        # 标题行只保留短结论，完整说明放下方文本框
        short_map = {
            "excellent": "非常适合",
            "good": "适合",
            "acceptable": "可运行",
            "poor": "勉强",
            "unsupported": "不建议",
        }
        short = short_map.get(compat.level, compat.level)
        if compat.level == "unsupported" and "PyTorch" in (compat.summary or ""):
            short = "可评估"
            color = "#e0c060"
        self.lbl_compat.setText(f"{compat.model_name}: {short}")
        self.lbl_compat.setStyleSheet(f"font-weight:bold;color:{color};")
        self.lbl_compat.setToolTip(compat.summary or "")

        base = self.txt_hw.toPlainText().split("\n\n—— 模型兼容性 ——")[0]
        summary = compat.summary
        if compat.level == "unsupported" and "PyTorch" in (compat.summary or ""):
            summary = "硬件可评估 · AI 依赖在隔离环境安装"
        extra = ["", "—— 模型兼容性 ——", f"{compat.model_name}: {summary}"]
        for d in compat.details:
            if "缺少 PyTorch" in d or "未安装 PyTorch" in d or "主程序未安装 torch" in d:
                extra.append("  · 主进程无 torch 为预期行为；请在模型页创建隔离环境")
            else:
                extra.append(f"  · {d}")
        self.txt_hw.setPlainText(base.rstrip() + "\n" + "\n".join(extra))

    def showEvent(self, event):
        super().showEvent(event)
        # 仅首次显示时调度一次后台刷新（切换菜单走 _on_menu_changed）
        if not self._dev_loaded and self.menu_list.currentRow() == 0:
            QTimer.singleShot(0, lambda: self._schedule_refresh(0))
