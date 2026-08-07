"""MattingModelRoute —— 配置中心「抠图模型配置」页（P5：自 settings_panel 拆出）。

职责：模型选择 / 隔离环境（uv）/ 权重下载 / 电脑配置检测；
开发环境未就绪时显示门禁引导。

后台任务（owner=本路由；owner 失效后回调经 isValid 守卫丢弃）：
- _EnvCheckWorker（QThread）：真实依赖校验
- _HwDetectWorker（QThread）：硬件检测
- 环境创建 / 权重下载：runtime / matting manager 内部线程 + _AsyncBridge

跨页协作只发信号，不持有开发环境页引用：
- goto_dev_requested / install_uv_requested / uv_status_resolved /
  git_status_resolved / vc_recheck_requested
"""
from __future__ import annotations

import sys

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QProgressBar, QPushButton, QSizePolicy,
    QStackedWidget, QTextEdit, QVBoxLayout, QWidget,
)
from shiboken6 import isValid

import config
from services.common.matting_config_service import (
    MattingConfigService,
    format_gb,
    get_matting_config_service,
)
from services.common.runtime_facade import (
    GIT_DOWNLOAD_URL,
    NVIDIA_DRIVER_URL,
    RuntimeFacade,
    build_ai_setup_prompt,
    get_runtime_facade,
    match_local_gpu,
    packages_need_git,
    plan_torch_install,
)

from .common import get_desktop_path, open_path, open_url, wrap_scroll


class _AsyncBridge(QObject):
    """后台线程回调 → UI 线程（环境创建 / 权重下载）。"""
    progress = Signal(str, float, str)   # key, percent, message
    finished = Signal(str, bool, str)    # key, ok, message


class _EnvCheckWorker(QThread):
    """后台校验模型环境依赖（真实 import）"""
    done = Signal(str, object)

    def __init__(self, facade: RuntimeFacade, model_id: str,
                 packages: list[str] | None = None, parent=None):
        super().__init__(parent)
        self._facade = facade
        self.model_id = model_id
        self.packages = packages

    def run(self):
        # 使用注册表完整检测列表；写回 meta，纠正历史脏数据
        st = self._facade.get_model_env_status(
            self.model_id, self.packages, force=True
        )
        try:
            from core.matting.model_registry import get_model_info
            meta = get_model_info(self.model_id)
            pkgs = list(meta.env_packages) if meta else []
            self._facade.write_env_meta(self.model_id, packages=pkgs, status=st)
        except Exception:
            pass
        self.done.emit(self.model_id, st)


class _HwDetectWorker(QThread):
    done = Signal(object)

    def __init__(self, service: MattingConfigService, parent=None):
        super().__init__(parent)
        self._service = service

    def run(self):
        self.done.emit(self._service.detect_hardware())


class MattingModelRoute(QWidget):
    """配置中心「抠图模型配置」子页。"""

    # 后台日志：由 SettingsRoute 转发给壳，写入「后台日志」页
    log_begin = Signal(str, str)   # feature_id, title
    log_line = Signal(str)         # 一行日志
    # 跨页协作（由 SettingsRoute 连接到开发环境页 / 菜单切换）
    goto_dev_requested = Signal()           # 跳转到「开发环境」菜单
    install_uv_requested = Signal()         # 跳转开发环境并安装 uv
    uv_status_resolved = Signal(object)     # force 重探到的 UvInfo（刷新开发页标签）
    git_status_resolved = Signal(object)    # force 重探到的 GitInfo
    vc_recheck_requested = Signal()         # 请求开发环境页重检 VC++

    def __init__(
        self,
        *,
        facade: RuntimeFacade | None = None,
        service: MattingConfigService | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self._facade = facade if facade is not None else get_runtime_facade()
        self._service = service if service is not None else get_matting_config_service()
        self._bridge = _AsyncBridge(self)
        self._bridge.progress.connect(self._on_async_progress)
        self._bridge.finished.connect(self._on_async_finished)
        self._async_feature = ""   # 当前任务日志 feature_id
        self._last_log_msg = ""    # 去重进度文案
        self._model_loaded = False
        self._hw_loaded = False
        self._dev_ready = False
        self._env_worker: _EnvCheckWorker | None = None
        self._hw_worker: _HwDetectWorker | None = None
        self._pending_env_check_id: str | None = None
        self._env_verified_once = False
        self._build_ui()
        self.apply_placeholder()

    # ═══ UI 构建 ═══

    def _build_ui(self):
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
        self.btn_goto_dev.clicked.connect(self.goto_dev_requested.emit)
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
        for m in self._service.list_models():
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
        self.btn_setup_env.clicked.connect(lambda: self.setup_model_env(False))
        env_btn.addWidget(self.btn_setup_env)
        self.btn_recreate_env = QPushButton("强制重建环境")
        self.btn_recreate_env.clicked.connect(lambda: self.setup_model_env(True))
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
            lambda: self.schedule_model_refresh(force_env=True)
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
        self.btn_detect.clicked.connect(lambda: self.run_hardware_detect(async_=True))
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

        self._matting_stack.addWidget(wrap_scroll(body))  # 1
        # 初始先显示门禁，待开发环境检测后再切换
        self._matting_stack.setCurrentIndex(0)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._matting_stack, 1)

    def apply_placeholder(self):
        self.lbl_env_status.setText("环境: …")
        self.lbl_env_status.setStyleSheet("color:#8a90b0;")

    # ═══ 门禁 / 页面进入 ═══

    def apply_gate(self):
        """开发环境未就绪时，模型配置右侧只显示跳转引导。"""
        ok, detail = self._facade.check_dev_ready()
        self._dev_ready = ok
        if ok:
            self._matting_stack.setCurrentIndex(1)
        else:
            self.lbl_matting_gate.setText(
                f"{detail}\n\n"
                "抠图模型依赖独立的 Python + uv 环境。\n"
                "请先到「开发环境」检测/选择 Python，并安装 uv，再回来配置模型。"
            )
            self._matting_stack.setCurrentIndex(0)

    def on_page_entered(self):
        """进入模型页：先门禁，再按需刷新模型区与硬件检测。"""
        self.apply_gate()
        if self._dev_ready:
            self.schedule_model_refresh(force_env=False)
            if not self._hw_loaded:
                self.run_hardware_detect(async_=True)

    def current_model_id(self) -> str:
        mid = self.combo_model.currentData()
        return mid or "ben2"

    # ═══ 模型区刷新（轻量同步 + 可选后台校验）═══

    def schedule_model_refresh(self, force_env: bool = False):
        self.apply_gate()
        if not self._dev_ready:
            return
        self._refresh_model_section_light()
        # 进入模型页或手动刷新时，后台做一次真实依赖检测
        if force_env or not self._env_verified_once:
            self._schedule_env_check()

    def _refresh_model_section_light(self):
        """只做路径/JSON 级读取，不阻塞 UI。"""
        mid = self.current_model_id()
        meta = self._service.get_model_info(mid)
        mgr = self._service

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
        st = self._facade.get_model_env_status(mid, quick=True)
        self._apply_env_status(st, pending_verify=True)
        self._refresh_gpu_match_label()

        self.btn_setup_env.setEnabled(not self._facade.is_busy)
        self.btn_recreate_env.setEnabled(not self._facade.is_busy)
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
        mid = self.current_model_id()
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
        worker = _EnvCheckWorker(self._facade, mid, None, parent=self)
        self._env_worker = worker
        worker.done.connect(self._on_env_check_done)
        worker.start()

    @Slot(str, object)
    def _on_env_check_done(self, model_id: str, st):
        # 若用户已切到别的模型，丢弃结果并立刻校验当前模型
        pending = self._pending_env_check_id
        current = self.current_model_id()
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

    def _apply_env_status(self, st, pending_verify: bool = False):
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

    # ═══ 模型 / 设备事件 ═══

    def _on_model_combo_changed(self, _idx: int):
        mid = self.current_model_id()
        self._service.set_default_model_id(mid)
        self._service.clear_model_cache()
        # 每个模型有独立环境/权重，切换后强制重新校验
        self._env_verified_once = False
        self.schedule_model_refresh(force_env=True)
        self._update_compat_label()

    def _on_device_changed(self, _idx: int):
        dev = self.combo_device.currentData() or "auto"
        self._service.set_device_preference(dev)
        self._service.clear_model_cache()
        self._update_device_req_hint()

    def _update_device_req_hint(self):
        """按当前推理设备，只显示对应模式的最低配置（一行简短提示）。"""
        mid = self.current_model_id()
        meta = self._service.get_model_info(mid)
        dev = self.combo_device.currentData() or "auto"
        if meta is None:
            self.lbl_device_req.setText("")
            self.lbl_device_req.setToolTip("")
            return

        ram_min = format_gb(meta.min_ram_gb)
        ram_rec = format_gb(meta.recommend_ram_gb)
        # GPU 侧以推荐显存为主（min_vram 可能为 0 表示允许 CPU）
        vram_show = meta.recommend_vram_gb if meta.recommend_vram_gb > 0 else meta.min_vram_gb
        vram_txt = format_gb(vram_show)

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

    # ═══ 权重路径 / 目录 ═══

    def _browse_custom_path(self):
        start = self.txt_custom_path.text().strip() or get_desktop_path()
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
        mid = self.current_model_id()
        self._service.set_custom_path(mid, self.txt_custom_path.text().strip())
        self._service.clear_model_cache()
        self.schedule_model_refresh(force_env=False)
        QMessageBox.information(self, "已保存", "本地路径已保存。")

    def _open_model_page(self):
        meta = self._service.get_model_info(self.current_model_id())
        if meta:
            open_url(self, meta.page_url)

    def _open_model_folder(self):
        d = self._service.open_model_folder(self.current_model_id())
        open_path(self, d)

    def _open_env_folder(self):
        d = self._facade.model_env_dir(self.current_model_id())
        d.mkdir(parents=True, exist_ok=True)
        open_path(self, d)

    # ═══ 创建 / 修复模型环境 ═══

    def setup_model_env(self, force: bool):
        mid = self.current_model_id()
        meta = self._service.get_model_info(mid)
        if meta is None:
            return
        if self._facade.is_busy:
            QMessageBox.information(self, "请稍候", "已有环境任务进行中")
            return

        # 前置检查：force 刷新 PATH + 清缓存，与开发环境「重新检测」同源，避免状态不同步
        uv = self._facade.resolve_uv(force=True)
        if not uv.found:
            ret = QMessageBox.question(
                self,
                "需要 uv",
                "尚未检测到 uv。\n\n"
                "若已手动放到 runtime/uv/，请先到「开发环境」点「重新检测 uv」。\n"
                "是否现在自动安装 uv？",
            )
            if ret != QMessageBox.Yes:
                return
            self.install_uv_requested.emit()
            return
        # 刷新开发环境页标签，保持与探测结果一致
        self.uv_status_resolved.emit(uv)

        # 优先本机 3.10–3.12；若无，创建环境时由 uv 按目标版本（如 3.12）托管下载，
        # 不再因只有 3.13/3.14 而直接拦截（那些版本无 torch wheel，不能用作 venv 基座）。
        target_py = (meta.python_version or "3.12").strip() or "3.12"
        try:
            tp = target_py.split(".")
            tmaj, tmin = int(tp[0]), int(tp[1]) if len(tp) > 1 else 12
        except ValueError:
            tmaj, tmin = 3, 12
            target_py = "3.12"
        base = self._facade.resolve_base_python(
            min_ver=(tmaj, tmin), max_ver=(tmaj, tmin),
        )
        if base is None:
            base = self._facade.resolve_base_python(min_ver=(3, 10), max_ver=(3, 12))
        if base is None and not uv.found:
            QMessageBox.warning(
                self,
                "需要 Python 或 uv",
                f"未找到可用的 Python 3.10–3.12（64 位），且 uv 未就绪。\n"
                f"请到「开发环境」安装 uv（可自动下载 Python {target_py}），\n"
                "或安装本机 Python 3.10–3.12 后再试。\n"
                "请勿使用 3.13/3.14 作为模型环境（无对应 PyTorch 轮子）。",
            )
            self.goto_dev_requested.emit()
            return

        # git+https 依赖（如 BEN2）需要 Git
        pkgs = list(meta.env_packages or ())
        if packages_need_git(pkgs):
            git = self._facade.resolve_git(force=True)
            self.git_status_resolved.emit(git)
            if not git.found:
                ret = QMessageBox.warning(
                    self,
                    "需要 Git",
                    "当前模型依赖含 git+https 包（如 BEN2），但未检测到 Git 客户端。\n\n"
                    "请先安装 Git for Windows（建议勾选加入 PATH）。\n"
                    "安装完成后无需重启：到「开发环境」点「重新检测 Git」，\n"
                    "确认状态为已就绪后再点「创建/修复环境」。\n\n"
                    f"下载页: {GIT_DOWNLOAD_URL}\n\n"
                    "是否打开下载页？",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.Yes,
                )
                if ret == QMessageBox.Yes:
                    open_url(self, GIT_DOWNLOAD_URL)
                self.goto_dev_requested.emit()
                return

        # Windows：VC++ 异常时提前提示（不强制阻断，允许用户仍尝试安装）
        if sys.platform == "win32":
            try:
                vc = self._facade.get_vc_redist(force=True)
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
                    self.goto_dev_requested.emit()
                    self.vc_recheck_requested.emit()
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
        self._emit_log(f"目标 Python: {target_py}（严格 3.10–3.12，拒绝更高版本 ABI）")
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
        else:
            self._emit_log(
                f"本机无 3.10–3.12：将由 uv 按版本号托管下载 Python {target_py}"
            )
        if uv.found:
            self._emit_log(f"uv: {uv.display}")
        bridge = self._bridge

        def prog(stage, pct, msg):
            if isValid(bridge):
                bridge.progress.emit("env", float(pct), msg)

        def fin(ok, msg, st):
            if isValid(bridge):
                bridge.finished.emit("env", ok, msg)

        self._facade.ensure_model_env_async(
            mid,
            python_version=meta.python_version,
            packages=pkgs,
            progress=prog,
            finished=fin,
            force_recreate=force,
        )

    # ═══ AI 帮装 ═══

    def _show_ai_setup_dialog(self):
        """弹出 AI 帮装指令窗口，供复制到 Codex / CodeBuddy 等。"""
        mid = self.current_model_id()
        meta = self._service.get_model_info(mid)
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
            lambda: open_url(self, NVIDIA_DRIVER_URL)
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

    # ═══ 权重下载 ═══

    def _start_download(self):
        mid = self.current_model_id()
        meta = self._service.get_model_info(mid)
        if meta is None:
            return

        # 仅检查环境是否存在；完整依赖由下载时子进程报错提示
        if not self._facade.env_exists(mid):
            QMessageBox.warning(
                self,
                "请先创建环境",
                "下载权重需要模型隔离环境中的 modelscope。\n"
                "请先点击「创建 / 修复环境」。",
            )
            return

        if self._service.is_ready(mid):
            ret = QMessageBox.question(
                self,
                "重新下载",
                f"模型 {meta.name} 权重已就绪，是否仍要重新下载？",
            )
            if ret != QMessageBox.Yes:
                return

        self.progress_dl.setValue(0)
        self.lbl_dl_msg.setText("准备下载…")
        self.btn_download.setEnabled(False)
        self.btn_download.setText("下载中…")
        self._emit_log_begin(
            f"settings_matting_{mid}",
            f"配置 · 下载模型权重 — {meta.name} ({mid})",
        )
        self._emit_log(f"来源: {meta.source}  ·  {meta.repo_id}")
        self._emit_log(f"保存目录: {self._service.default_local_dir(mid)}")
        bridge = self._bridge

        def on_prog(model_id, percent, message):
            if isValid(bridge):
                bridge.progress.emit("download", float(percent), message)

        def on_fin(model_id, ok, message):
            if isValid(bridge):
                bridge.finished.emit("download", ok, message)

        self._service.download_async(mid, progress=on_prog, finished=on_fin)

    # ═══ 异步回调（env / download）═══

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

        if key == "env":
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

        if key == "env":
            self.btn_setup_env.setEnabled(True)
            self.btn_recreate_env.setEnabled(True)
            self._facade.invalidate_caches(env=self.current_model_id())
            self.schedule_model_refresh(force_env=True)
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
            self.schedule_model_refresh(force_env=False)
            if ok:
                self.progress_dl.setValue(100)
                self.lbl_dl_msg.setText(message)
                self._service.clear_model_cache()
                QMessageBox.information(self, "下载完成", f"{message}\n权重已就绪。")
            else:
                self.lbl_dl_msg.setText(f"失败: {message}")
                QMessageBox.warning(self, "下载失败", message)

    # ═══ 硬件（异步）═══

    def run_hardware_detect(self, async_: bool = True):
        if not async_:
            self._apply_hw(self._service.detect_hardware())
            return
        if self._hw_worker is not None and self._hw_worker.isRunning():
            return
        self.txt_hw.setPlainText("正在后台检测硬件…")
        worker = _HwDetectWorker(self._service, parent=self)
        self._hw_worker = worker
        worker.done.connect(self._apply_hw)
        worker.start()

    @Slot(object)
    def _apply_hw(self, hw):
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
        hw = getattr(self, "_last_hw", None)
        if hw is None:
            self.lbl_compat.setText("检测中…")
            return
        mid = self.current_model_id()
        compat = self._service.evaluate_model(mid, hw)
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

    # ═══ 关闭协调 ═══

    def wait_workers(self, wait_ms: int = 3000) -> None:
        """有界等待后台校验 / 硬件检测线程（§5.4 规则 5）。"""
        if self._env_worker is not None and self._env_worker.isRunning():
            self._env_worker.wait(wait_ms // 2)
        if self._hw_worker is not None and self._hw_worker.isRunning():
            self._hw_worker.wait(wait_ms // 2)
