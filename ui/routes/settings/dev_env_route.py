"""DevEnvRoute —— 配置中心「开发环境」页（P5：自 settings_panel 拆出）。

职责：系统 Python / uv / Git / VC++ / 依赖镜像 / GitHub 代理 / 环境诊断。
后台任务：
- _DevScanWorker（QThread）：Python / uv / Git 扫描
- uv 安装（threading.Thread + _AsyncBridge 回 UI；owner 失效后丢弃回调）

跨页协作通过信号完成，不持有抠图模型页引用：
- dev_state_changed：开发环境状态变化（门禁需重估）
"""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QProgressBar, QPushButton, QTextEdit, QVBoxLayout, QWidget,
)
from shiboken6 import isValid

import config
from services.common.runtime_facade import (
    DEFAULT_GITHUB_PROXY,
    DEFAULT_PIP_INDEX_URL,
    GITHUB_PROXY_PRESETS,
    GIT_DOWNLOAD_URL,
    PIP_INDEX_PRESETS,
    RuntimeFacade,
    VC_REDIST_HELP_URL,
    VC_REDIST_X64_URL,
    get_runtime_facade,
    rewrite_github_url,
)

from .common import browse_python_exe, open_path, open_url, wrap_scroll


class _AsyncBridge(QObject):
    """后台线程回调 → UI 线程（uv 安装）。"""
    progress = Signal(str, float, str)   # key, percent, message
    finished = Signal(str, bool, str)    # key, ok, message


class _DevScanWorker(QThread):
    """后台扫描 Python / uv / git，避免阻塞 UI"""
    done = Signal(object, object, object, str)  # pys, uv, git, diagnose

    def __init__(self, facade: RuntimeFacade, force: bool = False, parent=None):
        super().__init__(parent)
        self._facade = facade
        self.force = force

    def run(self):
        rt = self._facade
        if self.force:
            rt.invalidate_caches(pythons=True, uv=True, vc=True, git=True)
        pys = rt.discover_pythons(force=self.force)
        uv = rt.resolve_uv(force=self.force)
        git = rt.resolve_git(force=self.force)
        # 强制刷新时同步重检 VC++，写入诊断文本
        if self.force:
            rt.get_vc_redist(force=True)
        diag = rt.diagnose_text()
        self.done.emit(pys, uv, git, diag)


class DevEnvRoute(QWidget):
    """配置中心「开发环境」子页。"""

    # 开发环境状态变化（扫描完成 / 重检 / uv 安装完成），门禁需重估
    dev_state_changed = Signal()
    # 后台日志：由 SettingsRoute 转发给壳，写入「后台日志」页
    log_begin = Signal(str, str)   # feature_id, title
    log_line = Signal(str)         # 一行日志

    def __init__(self, *, facade: RuntimeFacade | None = None, parent=None):
        super().__init__(parent)
        self._facade = facade if facade is not None else get_runtime_facade()
        self._bridge = _AsyncBridge(self)
        self._bridge.progress.connect(self._on_async_progress)
        self._bridge.finished.connect(self._on_async_finished)
        self._async_feature = ""   # 当前任务日志 feature_id
        self._last_log_msg = ""    # 去重进度文案
        self._dev_loaded = False
        self._scan_worker: _DevScanWorker | None = None
        self._build_ui()
        self.apply_placeholder()

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

    @property
    def dev_loaded(self) -> bool:
        return self._dev_loaded

    # ═══ UI 构建 ═══

    def _build_ui(self):
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
        self.btn_refresh_py.clicked.connect(lambda: self.schedule_refresh(force=True))
        py_btn.addWidget(self.btn_refresh_py)
        self.btn_open_python_org = QPushButton("打开 Python 下载页")
        self.btn_open_python_org.clicked.connect(
            lambda: open_url(self, "https://www.python.org/downloads/")
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
        self.btn_install_uv.clicked.connect(self.install_uv)
        uv_row.addWidget(self.btn_install_uv)
        self.btn_refresh_uv = QPushButton("重新检测 uv")
        self.btn_refresh_uv.setToolTip(
            "刷新系统 PATH 并重新探测 uv（手动安装/拷贝到 runtime/uv 后无需重启）"
        )
        self.btn_refresh_uv.clicked.connect(self.refresh_uv_status)
        uv_row.addWidget(self.btn_refresh_uv)
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

        # Git（git+https 依赖 / BEN2 等）
        grp_git = QGroupBox("Git 客户端")
        gg = QVBoxLayout(grp_git)
        gg.setSpacing(8)
        git_tip = QLabel(
            "部分抠图模型（如 <b>BEN2</b>）通过 <code>git+https://github.com/…</code> 安装代码包，"
            "需要本机已安装 <b>Git</b> 且可在 PATH 中调用。"
            "Git 不参与主程序运行，仅在创建/修复模型环境时使用。"
        )
        git_tip.setWordWrap(True)
        git_tip.setStyleSheet("color:#8a90b0;font-size:12px;")
        gg.addWidget(git_tip)
        self.lbl_git = QLabel("状态: —")
        self.lbl_git.setWordWrap(True)
        self.lbl_git.setStyleSheet("color:#c0c6d8;")
        gg.addWidget(self.lbl_git)
        git_row = QHBoxLayout()
        self.btn_refresh_git = QPushButton("重新检测 Git")
        self.btn_refresh_git.clicked.connect(self.refresh_git_status)
        git_row.addWidget(self.btn_refresh_git)
        self.btn_open_git_download = QPushButton("打开 Git 下载页")
        self.btn_open_git_download.setToolTip(GIT_DOWNLOAD_URL)
        self.btn_open_git_download.clicked.connect(
            lambda: open_url(self, GIT_DOWNLOAD_URL)
        )
        git_row.addWidget(self.btn_open_git_download)
        git_row.addStretch()
        gg.addLayout(git_row)
        lay.addWidget(grp_git)

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
        self.btn_refresh_vc.clicked.connect(self.refresh_vc_status)
        vc_row.addWidget(self.btn_refresh_vc)
        self.btn_open_vc_download = QPushButton("下载 VC++ x64")
        self.btn_open_vc_download.setToolTip(VC_REDIST_X64_URL)
        self.btn_open_vc_download.clicked.connect(
            lambda: open_url(self, VC_REDIST_X64_URL)
        )
        vc_row.addWidget(self.btn_open_vc_download)
        self.btn_open_vc_help = QPushButton("官方说明")
        self.btn_open_vc_help.clicked.connect(
            lambda: open_url(self, VC_REDIST_HELP_URL)
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

        # GitHub 代理（git+https / 从 GitHub 下载资源）
        grp_gh = QGroupBox("GitHub 访问代理")
        ggh = QVBoxLayout(grp_gh)
        ggh.setSpacing(8)
        gh_tip = QLabel(
            "国内网络直连 GitHub 常不稳定。<b>BEN2</b> 等 <code>git+https://github.com/…</code> "
            "依赖会按此处配置改写地址（前缀代理，如 "
            "<code>https://ghfast.top/https://github.com/…</code>）。"
            "仅影响本应用安装过程，<b>不</b>修改系统 git 全局配置。"
            "下载 uv 时也会优先尝试该代理。"
        )
        gh_tip.setWordWrap(True)
        gh_tip.setStyleSheet("color:#8a90b0;font-size:12px;")
        ggh.addWidget(gh_tip)

        gh_row = QHBoxLayout()
        gh_row.addWidget(QLabel("快捷选择:"))
        self.combo_gh_preset = QComboBox()
        self.combo_gh_preset.setStyleSheet(config.COMBOBOX_STYLE)
        self.combo_gh_preset.setMinimumWidth(160)
        for name, url in GITHUB_PROXY_PRESETS:
            self.combo_gh_preset.addItem(name, url)
        self.combo_gh_preset.addItem("自定义", "__custom__")
        self.combo_gh_preset.currentIndexChanged.connect(self._on_gh_preset_changed)
        gh_row.addWidget(self.combo_gh_preset)
        gh_row.addWidget(QLabel("代理前缀:"))
        self.edit_github_proxy = QLineEdit()
        self.edit_github_proxy.setPlaceholderText(
            "例如 https://ghfast.top/ ；留空=直连 GitHub"
        )
        self.edit_github_proxy.setMinimumWidth(260)
        gh_row.addWidget(self.edit_github_proxy, 1)
        self.btn_save_github_proxy = QPushButton("保存代理")
        self.btn_save_github_proxy.clicked.connect(self._save_github_proxy)
        gh_row.addWidget(self.btn_save_github_proxy)
        self.btn_reset_github_proxy = QPushButton("恢复默认")
        self.btn_reset_github_proxy.setToolTip(
            f"恢复为 ghfast\n{DEFAULT_GITHUB_PROXY}"
        )
        self.btn_reset_github_proxy.clicked.connect(self._reset_github_proxy)
        gh_row.addWidget(self.btn_reset_github_proxy)
        ggh.addLayout(gh_row)

        self.lbl_github_proxy_hint = QLabel("")
        self.lbl_github_proxy_hint.setWordWrap(True)
        self.lbl_github_proxy_hint.setStyleSheet("color:#8a90b0;font-size:12px;")
        ggh.addWidget(self.lbl_github_proxy_hint)
        lay.addWidget(grp_gh)
        self._load_github_proxy_ui()

        # 诊断
        grp_diag = QGroupBox("环境诊断")
        gd = QVBoxLayout(grp_diag)
        self.txt_diag = QTextEdit()
        self.txt_diag.setReadOnly(True)
        self.txt_diag.setMinimumHeight(200)
        gd.addWidget(self.txt_diag)
        lay.addWidget(grp_diag)
        lay.addStretch()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(wrap_scroll(body), 1)

    # ═══ 占位 / 刷新 ═══

    def apply_placeholder(self):
        self.lbl_py_hint.setText("检测将在后台进行…")
        self.lbl_py_hint.setStyleSheet("color:#8a90b0;font-size:12px;")
        self.lbl_uv.setText("状态: 检测中…")
        self.lbl_uv.setStyleSheet("color:#8a90b0;")
        self.lbl_git.setText("状态: 检测中…")
        self.lbl_git.setStyleSheet("color:#8a90b0;")
        self.txt_diag.setPlainText("正在后台扫描开发环境…")

    def schedule_refresh(self, force: bool = False):
        # 有缓存且非强制：直接画 UI，零等待
        if not force and self._facade.has_scan_cache():
            self.apply_dev_snapshot(
                self._facade.discover_pythons(),
                self._facade.resolve_uv(),
                self._facade.resolve_git(),
                self._facade.diagnose_text(),
            )
            return
        if self._scan_worker is not None and self._scan_worker.isRunning():
            return
        self.lbl_dev_msg.setText("正在后台检测 Python / uv / Git…")
        self.btn_refresh_py.setEnabled(False)
        self.btn_refresh_uv.setEnabled(False)
        self.btn_refresh_git.setEnabled(False)
        worker = _DevScanWorker(self._facade, force=force, parent=self)
        self._scan_worker = worker
        worker.done.connect(self._on_dev_scan_done)
        worker.finished.connect(self._on_dev_scan_finished)
        worker.start()

    def _on_dev_scan_finished(self):
        self.btn_refresh_py.setEnabled(True)
        self.btn_refresh_uv.setEnabled(True)
        self.btn_refresh_git.setEnabled(True)

    @Slot(object, object, object, str)
    def _on_dev_scan_done(self, pys, uv, git, diag: str):
        self.apply_dev_snapshot(pys, uv, git, diag)
        self.lbl_dev_msg.setText("")

    def apply_dev_snapshot(self, pys, uv, git, diag: str):
        self._dev_loaded = True
        self._load_pip_index_ui()
        self._load_github_proxy_ui()
        saved = self._facade.get_saved_python_path()
        self.combo_python.blockSignals(True)
        self.combo_python.clear()
        sel = 0
        from pathlib import Path
        for i, p in enumerate(pys or []):
            label = p.display
            if self._facade.version_ok(p):
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

        self.apply_uv_status(uv)
        self.apply_git_status(git)
        self.apply_vc_status()
        self.txt_diag.setPlainText(diag or "")
        # 开发环境状态变化后，同步模型页门禁
        self.dev_state_changed.emit()

    # ═══ uv / Git / VC++ 状态 ═══

    def refresh_uv_status(self):
        """手动重检 uv（刷新 PATH + 清缓存），无需重启。"""
        self.lbl_uv.setText("状态: 检测中…")
        self.lbl_uv.setStyleSheet("color:#8a90b0;")
        self.btn_refresh_uv.setEnabled(False)
        try:
            self._facade.invalidate_caches(uv=True)
            uv = self._facade.resolve_uv(force=True)
            self.apply_uv_status(uv)
            # 同步门禁（模型页依赖 uv 就绪）
            self.dev_state_changed.emit()
            try:
                self.txt_diag.setPlainText(self._facade.diagnose_text())
            except Exception:
                pass
            if uv and uv.found:
                self.lbl_dev_msg.setText(f"uv 已重新检测到: {uv.display}")
            else:
                self.lbl_dev_msg.setText(
                    "仍未检测到 uv。可将 uv.exe 放到 runtime/uv/ 后再点重新检测，"
                    "或使用「安装 / 修复 uv」。"
                )
        finally:
            self.btn_refresh_uv.setEnabled(True)

    def apply_uv_status(self, uv=None):
        """更新开发环境页 uv 状态标签。"""
        if uv is None:
            try:
                uv = self._facade.resolve_uv()
            except Exception as e:
                self.lbl_uv.setText(f"状态: 检测失败 — {e}")
                self.lbl_uv.setStyleSheet("color:#e0a060;font-weight:bold;")
                return
        if uv and uv.found:
            self.lbl_uv.setText(f"状态: 已就绪  ·  {uv.display}")
            self.lbl_uv.setStyleSheet("color:#6dcea0;font-weight:bold;")
            self.btn_install_uv.setText("重新安装 uv")
        else:
            self.lbl_uv.setText(
                "状态: 未安装 — 可一键下载到 runtime/uv/\n"
                "手动放置 uv.exe 后点「重新检测 uv」即可，无需重启。"
            )
            self.lbl_uv.setStyleSheet("color:#e0a060;font-weight:bold;")
            self.btn_install_uv.setText("安装 uv")

    def refresh_git_status(self):
        """手动重检 Git（刷新系统 PATH + 清缓存），无需重启。"""
        self.lbl_git.setText("状态: 检测中…")
        self.lbl_git.setStyleSheet("color:#8a90b0;")
        self.btn_refresh_git.setEnabled(False)
        try:
            self._facade.invalidate_caches(git=True)
            git = self._facade.resolve_git(force=True)
            self.apply_git_status(git)
            # 同步门禁/诊断，保证模型配置页创建环境读到同一缓存
            self.dev_state_changed.emit()
            try:
                self.txt_diag.setPlainText(self._facade.diagnose_text())
            except Exception:
                pass
            if git and git.found:
                self.lbl_dev_msg.setText(f"Git 已重新检测到: {git.display}")
            else:
                self.lbl_dev_msg.setText(
                    "仍未检测到 Git。请确认已安装 Git for Windows，"
                    "然后再次点「重新检测 Git」（一般无需重启软件）。"
                )
        finally:
            self.btn_refresh_git.setEnabled(True)

    def apply_git_status(self, git=None):
        """更新开发环境页 Git 状态标签。"""
        if git is None:
            try:
                git = self._facade.resolve_git()
            except Exception as e:
                self.lbl_git.setText(f"状态: 检测失败 — {e}")
                self.lbl_git.setStyleSheet("color:#e0a060;font-weight:bold;")
                return
        if git and git.found:
            self.lbl_git.setText(f"状态: 已就绪  ·  {git.display}")
            self.lbl_git.setStyleSheet("color:#6dcea0;font-weight:bold;")
        else:
            self.lbl_git.setText(
                "状态: 未安装 — BEN2 等 git+https 依赖将无法安装\n"
                "请安装 Git for Windows（建议勾选加入 PATH），"
                "装完后点「重新检测 Git」即可，一般无需重启软件。"
            )
            self.lbl_git.setStyleSheet("color:#e0a060;font-weight:bold;")

    def refresh_vc_status(self):
        """手动重检 VC++ 并刷新标签 + 诊断区。"""
        self._facade.invalidate_caches(vc=True)
        self.apply_vc_status()
        # 诊断文本含 VC++ 段落，一并刷新（不重扫 Python，避免卡顿）
        try:
            self.txt_diag.setPlainText(self._facade.diagnose_text())
        except Exception:
            pass

    def apply_vc_status(self):
        """更新开发环境页 VC++ 状态标签。"""
        import sys
        if sys.platform != "win32":
            self.lbl_vc.setText("状态: 非 Windows，无需检测")
            self.lbl_vc.setStyleSheet("color:#8a90b0;")
            return
        try:
            vc = self._facade.get_vc_redist()
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

    # ═══ Python 选择 ═══

    def _browse_python(self):
        path = browse_python_exe(self)
        if path:
            self._facade.set_python_path(path)
            self.schedule_refresh(force=True)

    def _apply_python(self):
        path = self.combo_python.currentData() or ""
        if not path:
            QMessageBox.warning(self, "无效", "请先选择或浏览一个 Python 解释器")
            return
        self._facade.set_python_path(path)
        QMessageBox.information(self, "已保存", f"已设为默认基础 Python:\n{path}")
        self.schedule_refresh(force=True)

    # ═══ PyPI 镜像 ═══

    def _load_pip_index_ui(self):
        """从 runtime_settings 回填镜像源控件。"""
        url = self._facade.get_pip_index_url()
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
        self._facade.set_pip_index_url(url)
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
        self.txt_diag.setPlainText(self._facade.diagnose_text())

    def _reset_pip_index(self):
        self._facade.set_pip_index_url(DEFAULT_PIP_INDEX_URL)
        self._load_pip_index_ui()
        self.txt_diag.setPlainText(self._facade.diagnose_text())
        QMessageBox.information(
            self, "已恢复默认",
            f"已恢复为清华大学源:\n{DEFAULT_PIP_INDEX_URL}",
        )

    # ═══ GitHub 代理 ═══

    def _load_github_proxy_ui(self):
        """从 runtime_settings 回填 GitHub 代理控件。"""
        proxy = self._facade.get_github_proxy()
        self.edit_github_proxy.blockSignals(True)
        self.edit_github_proxy.setText(proxy)
        self.edit_github_proxy.blockSignals(False)

        self.combo_gh_preset.blockSignals(True)
        matched = False
        for i in range(self.combo_gh_preset.count()):
            data = self.combo_gh_preset.itemData(i)
            if data == "__custom__":
                continue
            # 规范化比较（尾部 /）
            a = (data or "").rstrip("/")
            b = (proxy or "").rstrip("/")
            if a == b:
                self.combo_gh_preset.setCurrentIndex(i)
                matched = True
                break
        if not matched:
            for i in range(self.combo_gh_preset.count()):
                if self.combo_gh_preset.itemData(i) == "__custom__":
                    self.combo_gh_preset.setCurrentIndex(i)
                    break
        self.combo_gh_preset.blockSignals(False)
        self._update_github_proxy_hint()

    def _on_gh_preset_changed(self, _idx: int = 0):
        data = self.combo_gh_preset.currentData()
        if data == "__custom__":
            self._update_github_proxy_hint()
            return
        # 含「不使用代理」的空字符串
        self.edit_github_proxy.setText(str(data or ""))
        self._update_github_proxy_hint()

    def _update_github_proxy_hint(self):
        proxy = (self.edit_github_proxy.text() or "").strip()
        sample = "git+https://github.com/yincangshiwei/BEN2.git"
        if proxy:
            rewritten = rewrite_github_url(sample, proxy)
            self.lbl_github_proxy_hint.setText(
                f"安装 git 依赖时将改写为:\n{rewritten}"
            )
        else:
            self.lbl_github_proxy_hint.setText(
                "当前未使用代理：git 依赖将直连 github.com。"
            )

    def _save_github_proxy(self):
        proxy = (self.edit_github_proxy.text() or "").strip()
        if proxy and not (
            proxy.startswith("http://") or proxy.startswith("https://")
        ):
            QMessageBox.warning(
                self, "无效地址",
                "代理前缀需以 http:// 或 https:// 开头，或留空表示直连 GitHub。",
            )
            return
        self._facade.set_github_proxy(proxy)
        self._load_github_proxy_ui()
        if proxy:
            sample = rewrite_github_url(
                "git+https://github.com/yincangshiwei/BEN2.git", proxy
            )
            QMessageBox.information(
                self, "已保存",
                f"已保存 GitHub 代理:\n{proxy}\n\n"
                f"示例改写:\n{sample}\n\n"
                "下次「创建/修复环境」安装 git 依赖时生效。",
            )
        else:
            QMessageBox.information(
                self, "已保存",
                "已关闭 GitHub 代理：安装时直连 github.com。",
            )
        self.txt_diag.setPlainText(self._facade.diagnose_text())

    def _reset_github_proxy(self):
        self._facade.set_github_proxy(DEFAULT_GITHUB_PROXY)
        self._load_github_proxy_ui()
        self.txt_diag.setPlainText(self._facade.diagnose_text())
        QMessageBox.information(
            self, "已恢复默认",
            f"已恢复为 ghfast 代理:\n{DEFAULT_GITHUB_PROXY}",
        )

    # ═══ uv 安装（后台线程 + 桥接）═══

    def install_uv(self):
        if self._facade.is_busy:
            QMessageBox.information(self, "请稍候", "已有任务进行中")
            return
        self.progress_dev.setValue(0)
        self.lbl_dev_msg.setText("开始安装 uv…")
        self.btn_install_uv.setEnabled(False)
        self._emit_log_begin("settings_runtime", "配置 · 安装 / 修复 uv")
        self._emit_log("开始安装 uv 到 runtime/uv/ …")
        bridge = self._bridge

        def prog(stage, pct, msg):
            if isValid(bridge):
                bridge.progress.emit("uv", float(pct), msg)

        def work():
            ok, msg = False, ""
            try:
                info = self._facade.install_uv(progress=prog)
                ok, msg = True, f"安装成功: {info.display}"
            except Exception as e:
                ok, msg = False, str(e)
            if isValid(bridge):
                bridge.finished.emit("uv", ok, msg)

        threading.Thread(target=work, daemon=True).start()

    def _open_runtime_dir(self):
        d = self._facade.runtime_root()
        open_path(self, d)

    # ═══ 异步回调（uv）═══

    @Slot(str, float, str)
    def _on_async_progress(self, key: str, percent: float, message: str):
        if key != "uv":
            return
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
        if percent >= 0:
            self.progress_dev.setValue(int(min(100, max(0, percent))))
        self.lbl_dev_msg.setText(message)

    @Slot(str, bool, str)
    def _on_async_finished(self, key: str, ok: bool, message: str):
        if key != "uv":
            return
        if ok:
            self._emit_log(f"✓ 完成: {message}")
        else:
            self._emit_log(f"✗ 失败: {message}")
        self._emit_log("─" * 50)
        self.btn_install_uv.setEnabled(True)
        self.btn_refresh_uv.setEnabled(True)
        self._facade.invalidate_caches(uv=True)
        # 安装完成后强制重探并同步门禁
        try:
            self.apply_uv_status(self._facade.resolve_uv(force=True))
            self.dev_state_changed.emit()
        except Exception:
            pass
        self.schedule_refresh(force=True)
        if ok:
            self.progress_dev.setValue(100)
            self.lbl_dev_msg.setText(message)
            QMessageBox.information(self, "uv 就绪", message)
        else:
            self.lbl_dev_msg.setText(f"失败: {message}")
            QMessageBox.warning(self, "安装失败", message)

    # ═══ 关闭协调 ═══

    def wait_workers(self, wait_ms: int = 3000) -> None:
        """有界等待后台扫描线程（§5.4 规则 5）。"""
        if self._scan_worker is not None and self._scan_worker.isRunning():
            self._scan_worker.wait(wait_ms)
