"""
配置中心面板 —— 左侧菜单 + 右侧内容
菜单：开发环境 → 抠图模型配置
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QObject, Signal, Slot, QTimer, QThread
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel, QPushButton,
    QComboBox, QLineEdit, QListWidget, QListWidgetItem, QStackedWidget,
    QFileDialog, QProgressBar, QTextEdit, QMessageBox, QDialog,
    QScrollArea, QFrame, QSizePolicy,
)

import config
from core.matting.hardware import detect_hardware, evaluate_model, HardwareInfo
from core.matting.model_manager import get_matting_manager
from core.matting.model_registry import list_models, get_model_info
from core.matting.inference import clear_model_cache
from core.runtime.env_manager import (
    get_runtime_manager,
    UvInfo,
    ModelEnvStatus,
    DEFAULT_PIP_INDEX_URL,
    PIP_INDEX_PRESETS,
    VC_REDIST_X64_URL,
    VC_REDIST_HELP_URL,
    NVIDIA_DRIVER_URL,
    plan_torch_install,
    build_ai_setup_prompt,
    match_local_gpu,
)


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
            rt.invalidate_caches(pythons=True, uv=True, vc=True)
        pys = rt.discover_pythons(force=self.force)
        uv = rt.resolve_uv(force=self.force)
        # 强制刷新时同步重检 VC++，写入诊断文本
        if self.force:
            rt.get_vc_redist(force=True)
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

    # 后台日志：由主窗口连接，写入「后台日志」页
    log_begin = Signal(str, str)   # feature_id, title
    log_line = Signal(str)         # 一行日志

    def __init__(self, parent=None, lazy: bool = False):
        super().__init__(parent)
        self._mgr = get_matting_manager()
        self._rt = get_runtime_manager()
        self._bridge = _AsyncBridge(self)
        self._bridge.progress.connect(self._on_async_progress)
        self._bridge.finished.connect(self._on_async_finished)
        self._async_kind = ""  # uv / env / download
        self._async_feature = ""  # 当前任务日志 feature_id
        self._last_log_msg = ""   # 去重进度文案
        self._dev_loaded = False
        self._model_loaded = False
        self._hw_loaded = False
        self._dev_ready = False
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

        # VC++ 运行库（Windows / PyTorch 必需）
        grp_vc = QGroupBox("VC++ 运行库（Windows）")
        gv = QVBoxLayout(grp_vc)
        gv.setSpacing(8)
        vc_tip = QLabel(
            "PyTorch 等原生扩展依赖系统 <b>Microsoft Visual C++ 2015–2022</b> 运行库。"
            "即使 pip 安装成功，运行库过旧/损坏也会导致 "
            "<code>WinError 1114</code> / <code>c10.dll</code> 加载失败（与网络无关）。"
        )
        vc_tip.setWordWrap(True)
        vc_tip.setStyleSheet("color:#8a90b0;font-size:12px;")
        gv.addWidget(vc_tip)
        self.lbl_vc = QLabel("状态: —")
        self.lbl_vc.setWordWrap(True)
        self.lbl_vc.setStyleSheet("color:#c0c6d8;")
        gv.addWidget(self.lbl_vc)
        vc_row = QHBoxLayout()
        self.btn_refresh_vc = QPushButton("重新检测 VC++")
        self.btn_refresh_vc.clicked.connect(self._refresh_vc_status)
        vc_row.addWidget(self.btn_refresh_vc)
        self.btn_open_vc_download = QPushButton("下载 VC++ x64")
        self.btn_open_vc_download.setToolTip(VC_REDIST_X64_URL)
        self.btn_open_vc_download.clicked.connect(
            lambda: self._open_url(VC_REDIST_X64_URL)
        )
        vc_row.addWidget(self.btn_open_vc_download)
        self.btn_open_vc_help = QPushButton("官方说明")
        self.btn_open_vc_help.clicked.connect(
            lambda: self._open_url(VC_REDIST_HELP_URL)
        )
        vc_row.addWidget(self.btn_open_vc_help)
        vc_row.addStretch()
        gv.addLayout(vc_row)
        lay.addWidget(grp_vc)

        # PyPI 镜像（仅本应用 uv pip install -i，不改用户全局配置）
        grp_mirror = QGroupBox("依赖安装镜像源")
        gm = QVBoxLayout(grp_mirror)
        gm.setSpacing(8)
        mirror_tip = QLabel(
            "创建/修复模型环境时，会在 <b>uv pip install</b> 命令上自动附加 "
            "<code>-i &lt;镜像地址&gt;</code>。"
            "仅影响本应用安装依赖，不会修改你系统里的 pip / uv 全局配置。"
        )
        mirror_tip.setWordWrap(True)
        mirror_tip.setStyleSheet("color:#8a90b0;font-size:12px;")
        gm.addWidget(mirror_tip)

        m_row = QHBoxLayout()
        m_row.addWidget(QLabel("快捷选择:"))
        self.combo_pip_preset = QComboBox()
        self.combo_pip_preset.setStyleSheet(config.COMBOBOX_STYLE)
        self.combo_pip_preset.setMinimumWidth(140)
        for name, url in PIP_INDEX_PRESETS:
            self.combo_pip_preset.addItem(name, url)
        self.combo_pip_preset.addItem("自定义 / 官方默认", "")
        self.combo_pip_preset.currentIndexChanged.connect(self._on_pip_preset_changed)
        m_row.addWidget(self.combo_pip_preset)
        m_row.addWidget(QLabel("索引 URL:"))
        self.edit_pip_index = QLineEdit()
        self.edit_pip_index.setPlaceholderText(
            "例如 https://pypi.tuna.tsinghua.edu.cn/simple；留空=不指定 -i"
        )
        self.edit_pip_index.setMinimumWidth(280)
        m_row.addWidget(self.edit_pip_index, 1)
        self.btn_save_pip_index = QPushButton("保存镜像")
        self.btn_save_pip_index.clicked.connect(self._save_pip_index)
        m_row.addWidget(self.btn_save_pip_index)
        self.btn_reset_pip_index = QPushButton("恢复默认")
        self.btn_reset_pip_index.setToolTip(f"恢复为清华源\n{DEFAULT_PIP_INDEX_URL}")
        self.btn_reset_pip_index.clicked.connect(self._reset_pip_index)
        m_row.addWidget(self.btn_reset_pip_index)
        gm.addLayout(m_row)

        self.lbl_pip_index_hint = QLabel("")
        self.lbl_pip_index_hint.setWordWrap(True)
        self.lbl_pip_index_hint.setStyleSheet("color:#8a90b0;font-size:12px;")
        gm.addWidget(self.lbl_pip_index_hint)
        lay.addWidget(grp_mirror)
        self._load_pip_index_ui()

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
        """外层 stack：0=开发环境未就绪门禁，1=模型配置正文。"""
        self._matting_stack = QStackedWidget()

        # —— 门禁页：开发环境未就绪 ——
        gate = QWidget()
        gate_lay = QVBoxLayout(gate)
        gate_lay.setContentsMargins(24, 40, 24, 24)
        gate_lay.setSpacing(14)
        gate_lay.addStretch(1)

        gate_title = QLabel("请先完成开发环境配置")
        gate_title.setAlignment(Qt.AlignCenter)
        gate_title.setStyleSheet(
            "color:#e0e4f0;font-size:16px;font-weight:bold;"
        )
        gate_lay.addWidget(gate_title)

        self.lbl_matting_gate = QLabel(
            "抠图模型依赖独立的 Python + uv 环境。\n"
            "请先到「开发环境」检测/选择 Python，并安装 uv，再回来配置模型。"
        )
        self.lbl_matting_gate.setAlignment(Qt.AlignCenter)
        self.lbl_matting_gate.setWordWrap(True)
        self.lbl_matting_gate.setStyleSheet("color:#a8b0d0;font-size:13px;")
        gate_lay.addWidget(self.lbl_matting_gate)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.btn_goto_dev = QPushButton("前往开发环境配置")
        self.btn_goto_dev.setObjectName("btn_start")
        self.btn_goto_dev.setMinimumHeight(38)
        self.btn_goto_dev.setMinimumWidth(200)
        self.btn_goto_dev.clicked.connect(lambda: self.open_menu(0))
        btn_row.addWidget(self.btn_goto_dev)
        btn_row.addStretch()
        gate_lay.addLayout(btn_row)
        gate_lay.addStretch(2)
        self._matting_stack.addWidget(gate)  # 0

        # —— 正文页 ——
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
        # 随推理设备切换，只显示当前模式的最低配置（不占纵向空间）
        self.lbl_device_req = QLabel("")
        self.lbl_device_req.setStyleSheet("color:#8a90b0;font-size:12px;")
        self.lbl_device_req.setToolTip("当前推理设备对应的最低配置要求")
        row1.addWidget(self.lbl_device_req)
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
            "创建环境会按本机是否有 NVIDIA 自动安装 CUDA 或 CPU 版 PyTorch"
            "（有独显装 CUDA 版即可，配置里仍可强制用 CPU），"
            "并安装对应模型依赖；体积较大、耗时较长，请保持网络畅通。"
            "进度详情请查看「后台日志」。"
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
        self.btn_ai_setup = QPushButton("AI 帮装")
        self.btn_ai_setup.setMinimumHeight(34)
        self.btn_ai_setup.setToolTip(
            "生成通用「帮装任务」说明（含复核环境、选 CUDA、安装与验收），"
            "可复制到 Codex / CodeBuddy 等；附录附本机探测快照供参考"
        )
        self.btn_ai_setup.clicked.connect(self._show_ai_setup_dialog)
        env_btn.addWidget(self.btn_ai_setup)
        self.btn_open_env = QPushButton("打开环境目录")
        self.btn_open_env.clicked.connect(self._open_env_folder)
        env_btn.addWidget(self.btn_open_env)
        env_btn.addStretch()
        ge.addLayout(env_btn)

        # GPU 系列匹配摘要（一行，不占纵向）
        self.lbl_gpu_match = QLabel("")
        self.lbl_gpu_match.setWordWrap(True)
        self.lbl_gpu_match.setStyleSheet("color:#8a90b0;font-size:12px;")
        self.lbl_gpu_match.setTextInteractionFlags(Qt.TextSelectableByMouse)
        ge.addWidget(self.lbl_gpu_match)

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
            "下载依赖模型隔离环境中的 modelscope。进度详情请查看「后台日志」。"
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

        self._matting_stack.addWidget(self._wrap_scroll(body))  # 1
        # 初始先显示门禁，待开发环境检测后再切换
        self._matting_stack.setCurrentIndex(0)
        return self._matting_stack

    # ── 菜单 / 对外跳转 ──
    def open_menu(self, row: int):
        """供主窗口/其它模块跳转到指定配置子页。"""
        row = max(0, min(row, self.menu_list.count() - 1))
        if self.menu_list.currentRow() != row:
            self.menu_list.setCurrentRow(row)
        else:
            self._on_menu_changed(row)

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
            # 进入模型页前先确认开发环境是否就绪
            self._update_dev_ready_flag()
            self._apply_matting_gate()
            if self._dev_ready:
                self._schedule_model_refresh(force_env=False)
                if not self._hw_loaded:
                    self._run_hardware_detect(async_=True)

    def _current_model_id(self) -> str:
        mid = self.combo_model.currentData()
        return mid or "ben2"

    # ── 开发环境就绪判断 / 模型页门禁 ──
    def _check_dev_ready(self) -> tuple[bool, str]:
        """返回 (是否可配置模型, 说明文案)。"""
        uv = self._rt.resolve_uv()
        base = self._rt.resolve_base_python()
        missing = []
        if base is None:
            missing.append("可用 Python 3.10–3.12（64 位）")
        if not uv.found:
            missing.append("包管理工具 uv")
        if missing:
            return False, "缺少: " + "、".join(missing)
        return True, "开发环境已就绪"

    def _update_dev_ready_flag(self):
        ok, detail = self._check_dev_ready()
        self._dev_ready = ok
        return ok, detail

    def _apply_matting_gate(self):
        """开发环境未就绪时，模型配置右侧只显示跳转引导。"""
        ok, detail = self._update_dev_ready_flag()
        if ok:
            self._matting_stack.setCurrentIndex(1)
        else:
            self.lbl_matting_gate.setText(
                f"{detail}\n\n"
                "抠图模型依赖独立的 Python + uv 环境。\n"
                "请先到「开发环境」检测/选择 Python，并安装 uv，再回来配置模型。"
            )
            self._matting_stack.setCurrentIndex(0)

    # ── 后台日志辅助 ──
    def _emit_log_begin(self, feature_id: str, title: str):
        self._async_feature = feature_id
        self._last_log_msg = ""
        self.log_begin.emit(feature_id, title)

    def _emit_log(self, text: str, *, dedupe: bool = False):
        msg = (text or "").strip()
        if not msg:
            return
        # 进度条会高频回调同一文案，避免刷屏
        if dedupe and msg == self._last_log_msg:
            return
        self._last_log_msg = msg
        self.log_line.emit(msg)

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
        self._load_pip_index_ui()
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

        self._apply_vc_status()
        self.txt_diag.setPlainText(diag or "")
        # 开发环境状态变化后，同步模型页门禁
        self._apply_matting_gate()

    def _refresh_vc_status(self):
        """手动重检 VC++ 并刷新标签 + 诊断区。"""
        self._rt.invalidate_caches(vc=True)
        self._apply_vc_status()
        # 诊断文本含 VC++ 段落，一并刷新（不重扫 Python，避免卡顿）
        try:
            self.txt_diag.setPlainText(self._rt.diagnose_text())
        except Exception:
            pass

    def _apply_vc_status(self):
        """更新开发环境页 VC++ 状态标签。"""
        if not hasattr(self, "lbl_vc"):
            return
        if sys.platform != "win32":
            self.lbl_vc.setText("状态: 非 Windows，无需检测")
            self.lbl_vc.setStyleSheet("color:#8a90b0;")
            return
        try:
            vc = self._rt.get_vc_redist()
        except Exception as e:
            self.lbl_vc.setText(f"状态: 检测失败 — {e}")
            self.lbl_vc.setStyleSheet("color:#e0a060;font-weight:bold;")
            return
        if vc.level == "ok":
            self.lbl_vc.setText(f"状态: 正常  ·  {vc.display}")
            self.lbl_vc.setStyleSheet("color:#6dcea0;font-weight:bold;")
        elif vc.level == "warn":
            self.lbl_vc.setText(
                f"状态: 建议升级  ·  {vc.display}\n"
                "可点击「下载 VC++ x64」安装最新运行库后重启。"
            )
            self.lbl_vc.setStyleSheet("color:#e0a060;font-weight:bold;")
        else:
            self.lbl_vc.setText(
                f"状态: 异常  ·  {vc.display}\n"
                "请点击「下载 VC++ x64」安装/修复，重启后再创建模型环境。"
                "此问题与网络无关，反复「创建/修复环境」无法解决。"
            )
            self.lbl_vc.setStyleSheet("color:#e07070;font-weight:bold;")

    # ── 模型区刷新（轻量同步 + 可选后台校验）──
    def _schedule_model_refresh(self, force_env: bool = False):
        ok, _ = self._update_dev_ready_flag()
        self._apply_matting_gate()
        if not ok:
            return
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

        self._update_device_req_hint()

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
        self._refresh_gpu_match_label()

        self.btn_setup_env.setEnabled(not self._rt.is_busy)
        self.btn_recreate_env.setEnabled(not self._rt.is_busy)
        self.btn_ai_setup.setEnabled(True)
        self.btn_download.setEnabled(not mgr.is_downloading(mid))
        self.btn_download.setText(
            "下载中…" if mgr.is_downloading(mid) else "下载权重"
        )
        self._model_loaded = True

    def _refresh_gpu_match_label(self):
        """刷新 GPU 系列匹配一行摘要。"""
        try:
            m = match_local_gpu(force_detect=False)
        except Exception as e:
            self.lbl_gpu_match.setText(f"GPU 匹配: 检测失败（{e}）")
            self.lbl_gpu_match.setStyleSheet("color:#e07070;font-size:12px;")
            return
        if not m.has_gpu:
            self.lbl_gpu_match.setText(
                "GPU 匹配: 未检测到 NVIDIA · 将装 CPU 版 PyTorch"
            )
            self.lbl_gpu_match.setStyleSheet("color:#8a90b0;font-size:12px;")
            self.lbl_gpu_match.setToolTip("")
            return
        text = m.summary or m.series_name
        if m.driver_hint and not m.driver_ok_for_series:
            text = f"⚠ {text}"
            self.lbl_gpu_match.setStyleSheet("color:#e0a060;font-size:12px;font-weight:bold;")
        elif m.driver_hint:
            self.lbl_gpu_match.setStyleSheet("color:#e0c060;font-size:12px;")
        else:
            self.lbl_gpu_match.setStyleSheet("color:#6dcea0;font-size:12px;")
        self.lbl_gpu_match.setText(f"GPU 匹配: {text}")
        tip_parts = [m.summary or ""]
        if m.notes:
            tip_parts.append(m.notes)
        if m.driver_hint:
            tip_parts.append(m.driver_hint)
        if m.candidate_tags:
            tip_parts.append("候选: " + " → ".join(m.candidate_tags))
        self.lbl_gpu_match.setToolTip("\n".join(p for p in tip_parts if p))

    def _schedule_env_check(self):
        mid = self._current_model_id()
        # 已有 worker：若校验的就是当前模型则跳过；否则等它结束后再由回调/下次触发
        if self._env_worker is not None and self._env_worker.isRunning():
            if getattr(self._env_worker, "model_id", None) == mid:
                return
            # 目标模型已变：标记待检，当前 worker 完成后会再启一次
            self._pending_env_check_id = mid
            self.lbl_env_msg.setText(f"等待切换校验模型 {mid} …")
            return
        self._pending_env_check_id = None
        self.lbl_env_msg.setText("正在后台校验完整依赖（torch 及模型包等）…")
        # 切换模型时重置「已校验」标记，确保每个模型单独检查
        self._env_verified_once = False
        worker = _EnvCheckWorker(mid, None, parent=self)
        self._env_worker = worker
        worker.done.connect(self._on_env_check_done)
        worker.start()

    @Slot(str, object)
    def _on_env_check_done(self, model_id: str, st: ModelEnvStatus):
        # 若用户已切到别的模型，丢弃结果并立刻校验当前模型
        pending = getattr(self, "_pending_env_check_id", None)
        current = self._current_model_id()
        if model_id != current or (pending and pending != model_id):
            self._pending_env_check_id = None
            QTimer.singleShot(0, self._schedule_env_check)
            return
        self._pending_env_check_id = None
        self._env_verified_once = True
        self._apply_env_status(st, pending_verify=False)
        if st.ready:
            self.lbl_env_msg.setText(st.detail or "依赖校验通过")
        else:
            # detail 已含分类建议；DLL 问题不再引导「再装依赖」
            msg = (st.detail or "").strip()
            if st.fail_kind == "dll" or "DLL" in msg or "WinError 1114" in msg or "c10.dll" in msg:
                self.lbl_env_msg.setText(msg or "DLL/运行库问题，请到「开发环境」检查 VC++")
            elif st.missing_packages and st.fail_kind in ("", "missing"):
                miss = ", ".join(st.missing_packages)
                self.lbl_env_msg.setText(
                    f"依赖未齐: {miss}。请点击「创建/修复环境」安装完整依赖。"
                    + (f"\n{msg}" if msg and miss not in msg else "")
                )
            else:
                self.lbl_env_msg.setText(msg or "环境未就绪")

    def _apply_env_status(self, st: ModelEnvStatus, pending_verify: bool = False):
        # 清理可能的脏字符
        detail = (st.detail or "").replace("\x1b", "").strip()
        if st.ready:
            ver = f"  ·  Python {st.python_version}" if st.python_version else ""
            torch_bit = ""
            if getattr(st, "torch_version", ""):
                build = (st.torch_build or "?").upper()
                if st.torch_cuda_available:
                    cu = st.torch_cuda_version or "OK"
                    torch_bit = f"  ·  torch {st.torch_version} [{build}/可用 {cu}]"
                else:
                    torch_bit = f"  ·  torch {st.torch_version} [{build}]"
            self.lbl_env_status.setText(f"环境: 就绪{ver}{torch_bit}")
            # CPU 构建但用户可能期望 GPU：用偏黄提示构建类型
            if getattr(st, "torch_build", "") == "cpu":
                self.lbl_env_status.setStyleSheet("color:#e0c060;font-weight:bold;")
                self.lbl_env_status.setToolTip(
                    "当前隔离环境为 CPU 版 PyTorch。"
                    "若本机有 NVIDIA 独显，请点「强制重建环境」以安装 CUDA 版；"
                    "CUDA 版也可在配置中强制使用 CPU。"
                )
            elif getattr(st, "torch_build", "") == "cuda" and not st.torch_cuda_available:
                self.lbl_env_status.setStyleSheet("color:#e0c060;font-weight:bold;")
                self.lbl_env_status.setToolTip(
                    "已安装 CUDA 版 PyTorch，但 torch.cuda 当前不可用。"
                    "请检查 NVIDIA 驱动是否正常，或查看开发环境诊断。"
                )
            else:
                self.lbl_env_status.setStyleSheet("color:#6dcea0;font-weight:bold;")
                self.lbl_env_status.setToolTip(detail or "")
        elif st.python_path:
            if pending_verify and "缺少" not in detail:
                self.lbl_env_status.setText("环境: 已创建 · 依赖校验中…")
                self.lbl_env_status.setStyleSheet("color:#8a90b0;font-weight:bold;")
            else:
                # DLL/运行库问题与「缺包」区分，避免误导
                if getattr(st, "fail_kind", "") == "dll" or "WinError 1114" in detail or "c10.dll" in detail:
                    text = "环境: DLL/运行库异常"
                    self.lbl_env_status.setStyleSheet("color:#e07070;font-weight:bold;")
                elif st.missing_packages:
                    miss = ", ".join(
                        m for m in st.missing_packages
                        if m.isidentifier() or all(c.isalnum() or c in "-_." for c in m)
                    )
                    text = f"环境: 缺少 {miss}" if miss else f"环境: {detail or '不完整'}"
                    self.lbl_env_status.setStyleSheet("color:#e0c060;font-weight:bold;")
                else:
                    text = f"环境: {detail.splitlines()[0] if detail else '已创建'}"
                    self.lbl_env_status.setStyleSheet("color:#e0c060;font-weight:bold;")
                # 截断，避免撑宽
                if len(text) > 48:
                    text = text[:45] + "…"
                self.lbl_env_status.setText(text)
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

    def _load_pip_index_ui(self):
        """从 runtime_settings 回填镜像源控件。"""
        if not hasattr(self, "edit_pip_index"):
            return
        url = self._rt.get_pip_index_url()
        self.edit_pip_index.blockSignals(True)
        self.edit_pip_index.setText(url)
        self.edit_pip_index.blockSignals(False)

        self.combo_pip_preset.blockSignals(True)
        matched = False
        for i in range(self.combo_pip_preset.count()):
            if (self.combo_pip_preset.itemData(i) or "") == url:
                self.combo_pip_preset.setCurrentIndex(i)
                matched = True
                break
        if not matched:
            # 自定义 URL：选中「自定义」项
            for i in range(self.combo_pip_preset.count()):
                if self.combo_pip_preset.itemData(i) == "":
                    self.combo_pip_preset.setCurrentIndex(i)
                    break
        self.combo_pip_preset.blockSignals(False)
        self._update_pip_index_hint()

    def _on_pip_preset_changed(self, _idx: int = 0):
        url = self.combo_pip_preset.currentData()
        # 选预设时写入输入框；选「自定义」不覆盖用户正在编辑的内容（除非当前为空）
        if url:
            self.edit_pip_index.setText(str(url))
        self._update_pip_index_hint()

    def _update_pip_index_hint(self):
        if not hasattr(self, "lbl_pip_index_hint"):
            return
        url = (self.edit_pip_index.text() or "").strip()
        if url:
            self.lbl_pip_index_hint.setText(
                f"安装依赖时将附加: uv pip install -i {url} …"
            )
        else:
            self.lbl_pip_index_hint.setText(
                "当前未指定镜像：安装时不附加 -i，使用 uv/pip 默认源。"
            )

    def _save_pip_index(self):
        url = (self.edit_pip_index.text() or "").strip()
        if url and not (
            url.startswith("http://") or url.startswith("https://")
        ):
            QMessageBox.warning(
                self, "无效地址",
                "镜像地址需以 http:// 或 https:// 开头，或留空表示不使用 -i。",
            )
            return
        self._rt.set_pip_index_url(url)
        self._load_pip_index_ui()
        if url:
            QMessageBox.information(
                self, "已保存",
                f"已保存依赖安装镜像源:\n{url}\n\n"
                "下次「创建/修复环境」时将自动使用 -i 该地址。",
            )
        else:
            QMessageBox.information(
                self, "已保存",
                "已清空镜像源：安装依赖时不附加 -i。",
            )
        # 刷新诊断文本中的镜像行
        if hasattr(self, "txt_diag"):
            self.txt_diag.setPlainText(self._rt.diagnose_text())

    def _reset_pip_index(self):
        self._rt.set_pip_index_url(DEFAULT_PIP_INDEX_URL)
        self._load_pip_index_ui()
        if hasattr(self, "txt_diag"):
            self.txt_diag.setPlainText(self._rt.diagnose_text())
        QMessageBox.information(
            self, "已恢复默认",
            f"已恢复为清华大学源:\n{DEFAULT_PIP_INDEX_URL}",
        )

    def _install_uv(self):
        if self._rt.is_busy:
            QMessageBox.information(self, "请稍候", "已有任务进行中")
            return
        self._async_kind = "uv"
        self.progress_dev.setValue(0)
        self.lbl_dev_msg.setText("开始安装 uv…")
        self.btn_install_uv.setEnabled(False)
        self._emit_log_begin("settings_runtime", "配置 · 安装 / 修复 uv")
        self._emit_log("开始安装 uv 到 runtime/uv/ …")
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
            self.open_menu(0)
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
            self.open_menu(0)
            return

        # Windows：VC++ 异常时提前提示（不强制阻断，允许用户仍尝试安装）
        if sys.platform == "win32":
            try:
                vc = self._rt.get_vc_redist(force=True)
            except Exception:
                vc = None
            if vc is not None and not vc.ok:
                ret = QMessageBox.warning(
                    self,
                    "VC++ 运行库异常",
                    f"{vc.detail}\n\n"
                    "在此状态下即使依赖下载成功，import torch 也很可能失败"
                    "（WinError 1114 / c10.dll），与网络无关。\n\n"
                    "建议先到「开发环境」下载并修复 VC++ x64，重启后再创建环境。\n"
                    "仍要继续安装依赖吗？",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if ret != QMessageBox.Yes:
                    self.open_menu(0)
                    self._refresh_vc_status()
                    return

        # 预检 torch 安装方案（主流系列清单）
        try:
            torch_plan = plan_torch_install(force_detect=True)
        except Exception:
            torch_plan = None

        # 驱动不足以支撑本系列最低 CUDA 时，先提示升级
        if (
            torch_plan is not None
            and torch_plan.flavor == "cuda"
            and not torch_plan.driver_ok
            and torch_plan.driver_hint
        ):
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle("建议先升级显卡驱动")
            box.setText(
                "当前 NVIDIA 驱动可能无法安装本机 GPU 所需的 CUDA 版 PyTorch。\n\n"
                f"{torch_plan.driver_hint}"
            )
            box.setInformativeText(
                f"驱动下载：{NVIDIA_DRIVER_URL}\n\n"
                "建议：升级驱动并重启 → 再点「创建/修复环境」。\n"
                "也可打开「AI 帮装」复制指令，交给其它 AI 工具处理。\n"
                "仍要继续尝试安装吗？"
            )
            btn_cont = box.addButton("仍继续安装", QMessageBox.AcceptRole)
            btn_ai = box.addButton("打开 AI 帮装", QMessageBox.ActionRole)
            btn_cancel = box.addButton("取消", QMessageBox.RejectRole)
            box.setDefaultButton(btn_cancel)
            box.exec()
            clicked = box.clickedButton()
            if clicked is btn_ai:
                self._show_ai_setup_dialog()
                return
            if clicked is not btn_cont:
                return

        if force:
            plan_tip = ""
            if torch_plan is not None:
                plan_tip = (
                    f"\n\nPyTorch 将安装: {torch_plan.display}\n"
                    f"{torch_plan.reason}"
                )
                if torch_plan.candidate_tags:
                    plan_tip += "\n候选: " + " → ".join(torch_plan.candidate_tags)
            ret = QMessageBox.question(
                self,
                "强制重建",
                f"将删除并重建模型 {meta.name} 的隔离环境，是否继续？{plan_tip}",
            )
            if ret != QMessageBox.Yes:
                return

        self._async_kind = "env"
        self.progress_env.setValue(0)
        self.lbl_env_msg.setText("准备创建环境…")
        self.btn_setup_env.setEnabled(False)
        self.btn_recreate_env.setEnabled(False)
        action = "强制重建" if force else "创建/修复"
        pkgs = list(meta.env_packages)
        self._emit_log_begin(
            f"settings_matting_{mid}",
            f"配置 · {action}模型环境 — {meta.name} ({mid})",
        )
        self._emit_log(f"目标 Python: {meta.python_version}")
        self._emit_log(f"依赖包数: {len(pkgs)}")
        if pkgs:
            self._emit_log("依赖列表: " + ", ".join(pkgs[:12]) + ("…" if len(pkgs) > 12 else ""))
        if torch_plan is not None:
            self._emit_log(f"PyTorch 方案: {torch_plan.display}")
            self._emit_log(f"方案说明: {torch_plan.reason}")
            if torch_plan.candidate_tags:
                self._emit_log(
                    "CUDA 优选→备选: " + " → ".join(torch_plan.candidate_tags)
                )
            if torch_plan.driver_hint:
                self._emit_log(f"驱动提示: {torch_plan.driver_hint}")
            if torch_plan.flavor == "cuda" and torch_plan.index_url:
                self._emit_log(f"CUDA 索引: {torch_plan.index_url}")
            if torch_plan.match is not None:
                self._emit_log(
                    f"系列匹配: {torch_plan.match.series_name} "
                    f"[{torch_plan.match.match_method}]"
                )
        if base:
            self._emit_log(f"基础解释器: {base.display}")
        if uv.found:
            self._emit_log(f"uv: {uv.display}")
        bridge = self._bridge

        def prog(stage, pct, msg):
            bridge.progress.emit("env", float(pct), msg)

        def fin(ok, msg, st):
            bridge.finished.emit("env", ok, msg)

        self._rt.ensure_model_env_async(
            mid,
            python_version=meta.python_version,
            packages=pkgs,
            progress=prog,
            finished=fin,
            force_recreate=force,
        )

    def _show_ai_setup_dialog(self):
        """弹出 AI 帮装指令窗口，供复制到 Codex / CodeBuddy 等。"""
        mid = self._current_model_id()
        meta = get_model_info(mid)
        try:
            prompt = build_ai_setup_prompt(mid, force_recreate=True)
        except Exception as e:
            QMessageBox.warning(self, "生成失败", f"无法生成帮装指令:\n{e}")
            return

        try:
            matched = match_local_gpu(force_detect=True)
        except Exception:
            matched = None

        dlg = QDialog(self)
        dlg.setWindowTitle(
            f"AI 帮装 — {(meta.name if meta else mid)} 环境指令"
        )
        dlg.resize(780, 620)
        dlg.setMinimumSize(560, 420)
        root = QVBoxLayout(dlg)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        tip = QLabel(
            "将下方任务说明复制到 <b>Codex / CodeBuddy / Cursor</b> 等 AI 工具。"
            "正文是<strong>通用流程</strong>（先复核环境与最新 PyTorch 兼容性，再安装），"
            "不写死某一代显卡；文末附录附带本机探测快照，供 AI 参考，可少做重复检测。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#a8b0d0;font-size:12px;")
        root.addWidget(tip)

        # 匹配摘要条
        summary = QLabel("")
        summary.setWordWrap(True)
        summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        if matched is not None and matched.has_gpu:
            sum_txt = matched.summary or matched.series_name
            if matched.driver_hint:
                sum_txt += f"\n{matched.driver_hint}"
            summary.setText(sum_txt)
            if not matched.driver_ok_for_series:
                summary.setStyleSheet(
                    "color:#e0a060;font-size:12px;font-weight:bold;"
                    "padding:8px;background:rgba(80,50,20,120);border-radius:8px;"
                )
            else:
                summary.setStyleSheet(
                    "color:#b8d0a0;font-size:12px;"
                    "padding:8px;background:rgba(30,50,40,120);border-radius:8px;"
                )
        elif matched is not None:
            summary.setText("未检测到 NVIDIA GPU → 指令将引导安装 CPU 版 PyTorch")
            summary.setStyleSheet("color:#a8b0d0;font-size:12px;")
        else:
            summary.setText("硬件匹配信息不可用，指令内仍含通用步骤")
            summary.setStyleSheet("color:#a8b0d0;font-size:12px;")
        root.addWidget(summary)

        editor = QTextEdit()
        editor.setReadOnly(True)
        editor.setPlainText(prompt)
        editor.setStyleSheet(
            "QTextEdit {"
            "background-color: rgba(14, 14, 28, 200);"
            "color: #e0e4f0;"
            "border: 1px solid rgba(100, 110, 170, 0.25);"
            "border-radius: 8px;"
            "font-family: Consolas, 'Courier New', monospace;"
            "font-size: 12px;"
            "padding: 8px;"
            "}"
        )
        root.addWidget(editor, 1)

        btn_row = QHBoxLayout()
        btn_copy = QPushButton("复制全部指令")
        btn_copy.setObjectName("btn_start")
        btn_copy.setMinimumHeight(36)
        btn_copy.setMinimumWidth(140)

        def _do_copy():
            QGuiApplication.clipboard().setText(prompt)
            btn_copy.setText("已复制")
            QTimer.singleShot(1600, lambda: btn_copy.setText("复制全部指令"))

        btn_copy.clicked.connect(_do_copy)
        btn_row.addWidget(btn_copy)

        btn_driver = QPushButton("打开驱动下载页")
        btn_driver.setMinimumHeight(36)
        btn_driver.clicked.connect(
            lambda: self._open_url(NVIDIA_DRIVER_URL)
        )
        btn_row.addWidget(btn_driver)

        btn_row.addStretch()
        btn_close = QPushButton("关闭")
        btn_close.setMinimumHeight(36)
        btn_close.clicked.connect(dlg.accept)
        btn_row.addWidget(btn_close)
        root.addLayout(btn_row)

        # 深色对话框底
        dlg.setStyleSheet(
            "QDialog { background-color: rgba(22, 22, 40, 245); color: #e0e4f0; }"
            "QLabel { color: #e0e4f0; }"
            "QPushButton {"
            "background-color: rgba(38, 38, 62, 180);"
            "color: #e0e4f0;"
            "border: 1px solid rgba(100, 110, 170, 0.25);"
            "border-radius: 8px;"
            "padding: 6px 14px;"
            "}"
            "QPushButton:hover {"
            "border: 1px solid rgba(100, 150, 255, 0.55);"
            "}"
            "QPushButton#btn_start {"
            "background: qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 #5b8af5, stop:1 #7c6cf5);"
            "border: 1px solid rgba(120, 150, 255, 0.5);"
            "font-weight: bold;"
            "}"
        )
        dlg.exec()

    def _open_url(self, url: str):
        try:
            from PySide6.QtGui import QDesktopServices
            from PySide6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl(url))
        except Exception:
            try:
                if sys.platform == "win32":
                    os_start = getattr(__import__("os"), "startfile", None)
                    if os_start:
                        os_start(url)
                    else:
                        subprocess.Popen(["cmd", "/c", "start", url], shell=True)
                else:
                    subprocess.Popen(["xdg-open", url])
            except Exception as e:
                QMessageBox.information(self, "打开链接", f"请手动打开:\n{url}\n\n{e}")

    def _open_env_folder(self):
        d = self._rt.model_env_dir(self._current_model_id())
        d.mkdir(parents=True, exist_ok=True)
        self._open_path(d)

    # ── 模型事件 ──
    def _on_model_combo_changed(self, _idx: int):
        mid = self._current_model_id()
        self._mgr.set_default_model_id(mid)
        clear_model_cache()
        # 每个模型有独立环境/权重，切换后强制重新校验
        self._env_verified_once = False
        self._schedule_model_refresh(force_env=True)
        self._update_compat_label()

    def _on_device_changed(self, _idx: int):
        dev = self.combo_device.currentData() or "auto"
        self._mgr.set_device_preference(dev)
        clear_model_cache()
        self._update_device_req_hint()

    @staticmethod
    def _fmt_gb(value: float) -> str:
        """8.0 → 8；4.5 → 4.5"""
        try:
            v = float(value)
        except (TypeError, ValueError):
            return "?"
        if abs(v - round(v)) < 1e-6:
            return str(int(round(v)))
        return f"{v:.1f}"

    def _update_device_req_hint(self):
        """按当前推理设备，只显示对应模式的最低配置（一行简短提示）。"""
        mid = self._current_model_id()
        meta = get_model_info(mid)
        dev = self.combo_device.currentData() or "auto"
        if meta is None:
            self.lbl_device_req.setText("")
            self.lbl_device_req.setToolTip("")
            return

        ram_min = self._fmt_gb(meta.min_ram_gb)
        ram_rec = self._fmt_gb(meta.recommend_ram_gb)
        # GPU 侧以推荐显存为主（min_vram 可能为 0 表示允许 CPU）
        vram_show = meta.recommend_vram_gb if meta.recommend_vram_gb > 0 else meta.min_vram_gb
        vram_txt = self._fmt_gb(vram_show)

        if dev == "cpu":
            text = f"最低配置：内存 ≥ {ram_min}GB"
            tip = (
                f"CPU 推理：系统内存建议 ≥ {ram_min}GB（推荐 {ram_rec}GB）。"
                "速度较慢，适合无独显或显存不足时使用。"
            )
        elif dev == "cuda":
            text = f"最低配置：显存 ≥ {vram_txt}GB"
            tip = (
                f"GPU 推理：建议 NVIDIA 显存 ≥ {vram_txt}GB；"
                f"系统内存建议 ≥ {ram_min}GB。"
                "需隔离环境中安装支持 CUDA 的 PyTorch。"
            )
        else:
            # auto：优先 GPU，只提示主路径，避免同时堆 CPU/GPU 两套说明
            text = f"最低配置：显存 ≥ {vram_txt}GB（优先 GPU）"
            tip = (
                f"自动模式优先使用 GPU（建议显存 ≥ {vram_txt}GB）；"
                f"无可用 GPU 时回退 CPU（内存 ≥ {ram_min}GB）。"
            )

        self.lbl_device_req.setText(text)
        self.lbl_device_req.setToolTip(tip)

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
        self._emit_log_begin(
            f"settings_matting_{mid}",
            f"配置 · 下载模型权重 — {meta.name} ({mid})",
        )
        self._emit_log(f"来源: {meta.source}  ·  {meta.repo_id}")
        self._emit_log(f"保存目录: {self._mgr.default_local_dir(mid)}")
        bridge = self._bridge

        def on_prog(model_id, percent, message):
            bridge.progress.emit("download", float(percent), message)

        def on_fin(model_id, ok, message):
            bridge.finished.emit("download", ok, message)

        self._mgr.download_async(mid, progress=on_prog, finished=on_fin)

    # ── 异步回调 ──
    @Slot(str, float, str)
    def _on_async_progress(self, key: str, percent: float, message: str):
        # 进度写入后台日志：按文案去重（百分比变化不刷屏），阶段切换才记一行
        lines = [ln.strip() for ln in (message or "").splitlines() if ln.strip()]
        body = lines[0] if lines else (message or "").strip()
        if body and body != self._last_log_msg:
            pct = ""
            if percent is not None and percent >= 0:
                pct = f"[{int(min(100, max(0, percent))):3d}%] "
            self._emit_log(f"{pct}{body}")
            self._last_log_msg = body
            # 额外行（如 URL）一并写入
            for ln in lines[1:]:
                self._emit_log(f"      {ln}")

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
        if ok:
            self._emit_log(f"✓ 完成: {message}")
        else:
            self._emit_log(f"✗ 失败: {message}")
        self._emit_log("─" * 50)

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
            self._refresh_gpu_match_label()
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
