"""UpscaleEngineRoute —— 配置中心「高清放大引擎」子页。

职责：
- 展示显卡架构 / 系统 / 门禁结论（DLSS5 要求 RTX 40 系及以上）
- 管理 DLSS5 外挂运行时目录：定位、逐文件校验、可写性、DLSSNR 版本
- 提供应急开关（跳过显卡架构校验）与开发调试开关（LANCZOS 占位后端）
- 展示许可与免责说明

**PixelFlow 不随安装包或源码仓库分发任何 DLSS / ReShade / RenoDX 二进制**，
运行时包由维护者上传至本项目 Releases，配置页可一键下载并只提取必需文件。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

import config
from core.upscale import upscale_settings
from core.upscale.engine_registry import get_engine, list_engines
from core.upscale.hardware_gate import (
    LEVEL_BLOCKED,
    LEVEL_READY,
    LEVEL_WARN,
    EngineAvailability,
    check_engine,
    clear_cache as clear_gate_cache,
)
from core.upscale.runtime_bundle import (
    BundleStatus,
    bundle_summary_lines,
    default_bundle_dir,
    inspect_bundle,
    resolve_bundle_dir,
)

from .common import get_desktop_path, open_path, open_url, wrap_scroll

_MONO = 'font-family:Consolas,"Courier New",monospace;font-size:11px;'

_STATUS_STYLE = {
    LEVEL_READY: "color:#8fe3a8;font-size:12px;font-weight:bold;",
    LEVEL_WARN: "color:#f0d48a;font-size:12px;font-weight:bold;",
    LEVEL_BLOCKED: "color:#f0a0a0;font-size:12px;font-weight:bold;",
}


class _GateWorker(QThread):
    """后台执行门禁 + 运行时校验（nvidia-smi 与文件探测都不该卡界面）。"""

    done = Signal(object, object)     # EngineAvailability | Exception, BundleStatus | None

    def __init__(self, engine_id: str, bundle_dir: str, force: bool = False, parent=None):
        super().__init__(parent)
        self._engine_id = engine_id
        self._bundle_dir = bundle_dir
        self._force = bool(force)

    def run(self):
        bundle = None
        try:
            if self._force:
                clear_gate_cache()
            root = Path(self._bundle_dir) if self._bundle_dir else None
            bundle = inspect_bundle(root)
            av = check_engine(self._engine_id, force=self._force, bundle_root=root)
        except Exception as e:      # noqa: BLE001
            av = e
        self.done.emit(av, bundle)


class _InstallWorker(QThread):
    """后台执行运行时一键安装（下载 zip 较慢，绝不阻塞 UI 线程）。

    进度经 signal 回 UI 线程（符合线程契约：worker 不碰任何控件）。
    """

    progress = Signal(str, float, str)      # stage, percent, message
    finished_ok = Signal(object)            # BundleStatus
    finished_err = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        from core.upscale.bundle_installer import install_bundle

        def _cb(stage, pct, msg):
            if self._cancelled:
                raise BundleInstallErrorCancelled()
            self.progress.emit(str(stage), float(pct), str(msg))

        try:
            _root, status = install_bundle(progress=_cb)
            self.finished_ok.emit(status)
        except BundleInstallErrorCancelled:
            self.finished_err.emit("已取消安装")
        except Exception as e:      # noqa: BLE001 - 把失败原因带回 UI 展示
            self.finished_err.emit(str(e))


class BundleInstallErrorCancelled(RuntimeError):
    """用户取消安装。"""


class UpscaleEngineRoute(QWidget):
    """配置中心「高清放大引擎」子页。"""

    # 后台日志：由 SettingsRoute 转发给壳，写入「后台日志」页
    log_begin = Signal(str, str)   # feature_id, title
    log_line = Signal(str)         # 一行日志

    def __init__(self, parent=None):
        super().__init__(parent)
        self._engines = list_engines()
        self._worker: Optional[_GateWorker] = None
        self._install_worker: Optional[_InstallWorker] = None
        self._loaded = False
        self._availability: Optional[EngineAvailability] = None
        self._bundle: Optional[BundleStatus] = None
        self._build_ui()

    # ══════════════════════════════════════════════════════════
    #  UI
    # ══════════════════════════════════════════════════════════

    def _build_ui(self) -> None:
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        lay.addWidget(self._build_engine_group())
        lay.addWidget(self._build_hardware_group())
        lay.addWidget(self._build_runtime_group())
        lay.addWidget(self._build_debug_group())
        lay.addWidget(self._build_license_group())
        lay.addStretch()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(wrap_scroll(body))

    def _build_engine_group(self) -> QGroupBox:
        box = QGroupBox("放大引擎")
        row = QHBoxLayout(box)
        row.setSpacing(8)
        row.addWidget(QLabel("引擎:"))
        self.combo_engine = QComboBox()
        self.combo_engine.setStyleSheet(config.COMBOBOX_STYLE)
        self.combo_engine.setMinimumWidth(240)
        for engine in self._engines:
            self.combo_engine.addItem(f"{engine.icon} {engine.name}".strip(), engine.id)
        self.combo_engine.currentIndexChanged.connect(self._on_engine_changed)
        row.addWidget(self.combo_engine)

        self.lbl_status = QLabel("未检测")
        self.lbl_status.setStyleSheet(_STATUS_STYLE[LEVEL_WARN])
        row.addWidget(self.lbl_status)

        self.btn_recheck = QPushButton("重新检测")
        self.btn_recheck.setToolTip("重新检测显卡架构、系统与运行时完整性")
        self.btn_recheck.clicked.connect(lambda: self.refresh(force=True))
        row.addWidget(self.btn_recheck)
        row.addStretch()
        return box

    def _build_hardware_group(self) -> QGroupBox:
        box = QGroupBox("硬件与系统检测")
        lay = QVBoxLayout(box)
        lay.setSpacing(6)

        row1 = QHBoxLayout()
        row1.setSpacing(8)
        row1.addWidget(QLabel("显卡:"))
        self.lbl_gpu = QLabel("—")
        self.lbl_gpu.setStyleSheet("color:#e0e4f0;font-size:12px;")
        self.lbl_gpu.setTextInteractionFlags(Qt.TextSelectableByMouse)
        row1.addWidget(self.lbl_gpu, 1)
        row1.addWidget(QLabel("系统:"))
        self.lbl_os = QLabel("—")
        self.lbl_os.setStyleSheet("color:#e0e4f0;font-size:12px;")
        row1.addWidget(self.lbl_os)
        lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(8)
        row2.addWidget(QLabel("门禁:"))
        self.lbl_gate = QLabel("—")
        self.lbl_gate.setWordWrap(True)
        self.lbl_gate.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_gate.setStyleSheet("color:#b8bcd0;font-size:12px;")
        row2.addWidget(self.lbl_gate, 1)
        lay.addLayout(row2)

        self.lbl_hw_detail = QLabel("")
        self.lbl_hw_detail.setWordWrap(True)
        self.lbl_hw_detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_hw_detail.setStyleSheet(_MONO + "color:#a8b0d0;")
        lay.addWidget(self.lbl_hw_detail)
        return box

    def _build_runtime_group(self) -> QGroupBox:
        box = QGroupBox("DLSS5 运行时（外挂，不随安装包分发）")
        lay = QVBoxLayout(box)
        lay.setSpacing(6)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("目录:"))
        self.edit_dir = QLineEdit()
        self.edit_dir.setPlaceholderText(str(default_bundle_dir()))
        self.edit_dir.setToolTip(
            "DLSS5 便携包所在目录。支持两种布局：\n"
            "· 便携包原始布局 <目录>/bin/runtime/{host,dlss}/\n"
            "· 精简布局 <目录>/{host,dlss}/\n"
            "留空则使用默认目录（安装目录下的 runtime/upscale/dlss5）。\n"
            "注意：目录必须可写，不要放在 C:\\Program Files 下。"
        )
        row.addWidget(self.edit_dir, 1)
        self.btn_browse = QPushButton("浏览…")
        self.btn_browse.clicked.connect(self._on_browse)
        row.addWidget(self.btn_browse)
        self.btn_open_dir = QPushButton("打开目录")
        self.btn_open_dir.clicked.connect(self._on_open_dir)
        row.addWidget(self.btn_open_dir)
        self.btn_reset_dir = QPushButton("恢复默认")
        self.btn_reset_dir.setToolTip("清空自定义目录，改用安装目录下的 runtime/upscale/dlss5")
        self.btn_reset_dir.clicked.connect(self._on_reset_dir)
        row.addWidget(self.btn_reset_dir)
        lay.addLayout(row)

        row2 = QHBoxLayout()
        row2.setSpacing(8)
        self.btn_download = QPushButton("一键安装 DLSS5 运行时…")
        self.btn_download.setToolTip(
            "从本项目 Releases 下载运行时包，只提取图片放大所需的 5 个文件"
            "（约 200 MB）到运行时目录。\n下载自动走「开发环境」配置的 GitHub 代理，"
            "失败会逐级回退直连。"
        )
        self.btn_download.clicked.connect(self._on_download)
        row2.addWidget(self.btn_download)
        self.btn_cancel_install = QPushButton("取消")
        self.btn_cancel_install.setToolTip("取消正在进行的下载/安装")
        self.btn_cancel_install.clicked.connect(self._on_cancel_install)
        self.btn_cancel_install.setVisible(False)
        row2.addWidget(self.btn_cancel_install)
        self.btn_open_release = QPushButton("打开下载页")
        self.btn_open_release.clicked.connect(self._on_open_release)
        row2.addWidget(self.btn_open_release)
        self.btn_hash = QPushButton("校验文件哈希")
        self.btn_hash.setToolTip(
            "计算各文件 SHA256 并与已测试构建的参考值比对。\n"
            "158 MB 的 DLSSNR 运行时哈希较慢，仅在排查问题时使用。"
        )
        self.btn_hash.clicked.connect(lambda: self._show_hashes())
        row2.addWidget(self.btn_hash)
        row2.addStretch()
        lay.addLayout(row2)

        self.lbl_files = QLabel("尚未检测")
        self.lbl_files.setWordWrap(True)
        self.lbl_files.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_files.setStyleSheet(
            _MONO + "color:#b8bcd0;padding:6px 8px;"
            "background-color:rgba(14,14,28,160);border-radius:7px;"
        )
        lay.addWidget(self.lbl_files)
        return box

    def _build_debug_group(self) -> QGroupBox:
        box = QGroupBox("应急与调试")
        lay = QVBoxLayout(box)
        lay.setSpacing(6)

        row = QHBoxLayout()
        row.setSpacing(16)
        self.chk_skip_gpu = QCheckBox("跳过显卡架构校验")
        self.chk_skip_gpu.setToolTip(
            "仅在 nvidia-smi 未报告 compute_cap、且你确认显卡是 RTX 40 系及以上时使用。\n"
            "30 系及以下即使勾选也大概率崩溃（0xC0000005），不建议尝试。"
        )
        self.chk_skip_gpu.toggled.connect(self._on_skip_gpu_toggled)
        row.addWidget(self.chk_skip_gpu)

        self.chk_placeholder = QCheckBox("开发占位后端（LANCZOS，不加载 DLSS）")
        self.chk_placeholder.setToolTip(
            "调试用：不启动 DLSS5 原生渲染入口，改用 Pillow LANCZOS 放大，\n"
            "用于在没有运行时的机器上验证「面板 → Service → Worker → 输出」链路。\n"
            "批处理日志会明确标注 backend=placeholder，不会静默冒充 DLSS 结果。"
        )
        self.chk_placeholder.toggled.connect(self._on_placeholder_toggled)
        row.addWidget(self.chk_placeholder)
        row.addStretch()
        lay.addLayout(row)
        return box

    def _build_license_group(self) -> QGroupBox:
        box = QGroupBox("许可与免责")
        lay = QVBoxLayout(box)
        lay.setSpacing(6)
        self.lbl_license = QLabel("")
        self.lbl_license.setWordWrap(True)
        self.lbl_license.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.lbl_license.setStyleSheet(
            "color:#a8b0d0;font-size:11px;padding:6px 8px;"
            "background-color:rgba(38,38,62,90);border-radius:7px;"
        )
        lay.addWidget(self.lbl_license)
        return box

    # ══════════════════════════════════════════════════════════
    #  刷新
    # ══════════════════════════════════════════════════════════

    def on_page_entered(self) -> None:
        """进入本页（SettingsRoute 菜单切换时调用）。"""
        self._load_settings()
        if not self._loaded:
            self._loaded = True
            self.refresh(force=False)

    def schedule_refresh(self, force: bool = False) -> None:
        self.refresh(force=force)

    def current_engine_id(self) -> str:
        return str(self.combo_engine.currentData() or (self._engines[0].id if self._engines else ""))

    def refresh(self, *, force: bool = True) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        eid = self.current_engine_id()
        bundle_dir = self.edit_dir.text().strip()
        self.btn_recheck.setEnabled(False)
        self.lbl_status.setText("检测中…")
        self.lbl_status.setStyleSheet(_STATUS_STYLE[LEVEL_WARN])
        self.log_begin.emit("upscale", f"高清放大引擎检测: {eid}")
        self._worker = _GateWorker(eid, bundle_dir, force=force)
        self._worker.done.connect(self._on_gate_done)
        self._worker.start()

    def _on_gate_done(self, av, bundle) -> None:
        self.btn_recheck.setEnabled(True)
        if isinstance(bundle, BundleStatus):
            self._bundle = bundle
        if not isinstance(av, EngineAvailability):
            self.lbl_status.setText("检测失败")
            self.lbl_status.setStyleSheet(_STATUS_STYLE[LEVEL_BLOCKED])
            self.lbl_gate.setText(f"检测过程发生异常: {av}")
            self.log_line.emit(f"检测失败: {av}")
            return

        self._availability = av
        self._bundle = av.bundle if av.bundle is not None else self._bundle
        engine = get_engine(av.engine_id)

        self.lbl_status.setText(
            {LEVEL_READY: "🟢 就绪", LEVEL_WARN: "🟡 可用（有提示）"}.get(av.level, "🔴 不可用")
        )
        self.lbl_status.setStyleSheet(_STATUS_STYLE.get(av.level, _STATUS_STYLE[LEVEL_BLOCKED]))

        self.lbl_gpu.setText(av.gpu.display if av.gpu is not None else "未检测到 NVIDIA 显卡")
        self.lbl_os.setText(
            f"{'Windows' if av.windows.is_windows else '非 Windows'} "
            f"{av.windows.version_text}"
            + ("（64 位）" if av.windows.is_64bit else "（非 64 位）")
        )

        gate_lines: list[str] = []
        if av.reasons:
            gate_lines.append("❌ 不可用原因：")
            gate_lines.extend(f"  · {r}" for r in av.reasons)
        if av.warnings:
            gate_lines.append("⚠ 注意事项：")
            gate_lines.extend(f"  · {w}" for w in av.warnings)
        if not gate_lines:
            gate_lines.append(
                f"✅ 满足 {engine.name if engine else av.engine_id} 的全部硬性要求"
                f"（RTX {engine.min_gpu_generation if engine else 40} 系及以上 + 运行时完整 + 目录可写）"
            )
        self.lbl_gate.setText("\n".join(gate_lines))
        self.lbl_hw_detail.setText("\n".join(av.details))

        if self._bundle is not None:
            self.lbl_files.setText("\n".join(bundle_summary_lines(self._bundle)))
        else:
            self.lbl_files.setText("该引擎不需要外挂运行时")

        if engine is not None:
            text = engine.license_note or ""
            if engine.download_page:
                text += (
                    f'<br><br>运行时获取：<a href="{engine.download_page}">'
                    f"{engine.download_page}</a>"
                )
            self.lbl_license.setText(text)

        for line in gate_lines:
            self.log_line.emit(line)
        if self._bundle is not None:
            self.log_line.emit(f"运行时目录: {self._bundle.root}（布局 {self._bundle.layout or '未识别'}）")
            for f in self._bundle.files:
                self.log_line.emit(
                    f"  {'✓' if f.exists else '✗'} {f.rel}"
                    + (f"  {f.size_mb:.1f} MB" if f.exists else "  缺失")
                    + (f"  v{f.version}" if f.version else "")
                )

    # ══════════════════════════════════════════════════════════
    #  交互
    # ══════════════════════════════════════════════════════════

    def _load_settings(self) -> None:
        data = upscale_settings.load()
        eid = str(data.get("engine") or self.current_engine_id())
        idx = self.combo_engine.findData(eid)
        if idx >= 0:
            self.combo_engine.blockSignals(True)
            self.combo_engine.setCurrentIndex(idx)
            self.combo_engine.blockSignals(False)
        self.edit_dir.blockSignals(True)
        self.edit_dir.setText(str(data.get("dlss5_dir") or ""))
        self.edit_dir.blockSignals(False)
        self.chk_skip_gpu.blockSignals(True)
        self.chk_skip_gpu.setChecked(bool(data.get("allow_unverified_gpu")))
        self.chk_skip_gpu.blockSignals(False)
        self.chk_placeholder.blockSignals(True)
        self.chk_placeholder.setChecked(bool(data.get("placeholder_backend")))
        self.chk_placeholder.blockSignals(False)

    def _on_engine_changed(self, _index: int) -> None:
        eid = self.current_engine_id()
        upscale_settings.set_engine_id(eid)
        self.refresh(force=False)

    def _on_browse(self) -> None:
        # 项目规则：QFileDialog 默认目录使用桌面路径
        path = QFileDialog.getExistingDirectory(
            self, "选择 DLSS5 运行时目录", get_desktop_path()
        )
        if not path:
            return
        self.edit_dir.setText(path)
        self._save_dir(path)

    def _on_open_dir(self) -> None:
        target = resolve_bundle_dir(self.edit_dir.text().strip())
        if not target.exists():
            QMessageBox.information(
                self, "目录不存在",
                f"运行时目录尚不存在：\n{target}\n\n"
                "请先下载并解压 DLSS5 便携包，或用「一键安装 DLSS5 运行时」。",
            )
            return
        open_path(self, target)

    def _on_reset_dir(self) -> None:
        self.edit_dir.setText("")
        self._save_dir("")

    def _save_dir(self, path: str) -> None:
        upscale_settings.set_dlss5_dir(path or "")
        self.log_line.emit(f"DLSS5 运行时目录已设置为: {path or str(default_bundle_dir())}")
        self.refresh(force=True)

    def _on_skip_gpu_toggled(self, checked: bool) -> None:
        upscale_settings.set_allow_unverified_gpu(bool(checked))
        self.log_line.emit(f"跳过显卡架构校验: {'开启' if checked else '关闭'}")
        self.refresh(force=True)

    def _on_placeholder_toggled(self, checked: bool) -> None:
        upscale_settings.set_placeholder_backend(bool(checked))
        self.log_line.emit(
            f"开发占位后端（LANCZOS）: {'开启' if checked else '关闭'}"
        )
        if checked:
            QMessageBox.information(
                self, "开发占位后端已开启",
                "此时不会加载 DLSS，改用 Pillow LANCZOS 放大，仅用于验证处理链路。\n"
                "批处理日志会标注 backend=placeholder(LANCZOS)。\n\n"
                "正式使用前请关闭此开关。",
            )

    def _on_download(self) -> None:
        """一键安装：从本项目 Releases 下载运行时包并提取必需文件。"""
        if self._install_worker is not None and self._install_worker.isRunning():
            QMessageBox.information(
                self, "安装进行中", "已有安装任务在执行，请等待完成或点「取消」。"
            )
            return
        target = resolve_bundle_dir(self.edit_dir.text().strip())
        if self.edit_dir.text().strip() and not target.exists():
            target.mkdir(parents=True, exist_ok=True)
        reply = QMessageBox.question(
            self, "一键安装 DLSS5 运行时",
            "将从本项目 GitHub Releases 下载运行时包 DLSS5.Runtime.v5.0.zip"
            "（约 147 MB），提取图片放大所需的 5 个文件到：\n\n"
            f"{target}\n\n"
            "下载自动走「开发环境」配置的 GitHub 代理。是否继续？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return

        self.btn_download.setEnabled(False)
        self.btn_cancel_install.setVisible(True)
        self.lbl_files.setText("准备下载…")
        self.log_begin.emit("upscale", "DLSS5 运行时一键安装")
        self._install_worker = _InstallWorker()
        self._install_worker.progress.connect(self._on_install_progress)
        self._install_worker.finished_ok.connect(self._on_install_done)
        self._install_worker.finished_err.connect(self._on_install_error)
        self._install_worker.start()

    def _on_cancel_install(self) -> None:
        if self._install_worker is not None and self._install_worker.isRunning():
            self._install_worker.cancel()
            self.log_line.emit("正在取消安装…")

    def _on_install_progress(self, stage: str, pct: float, msg: str) -> None:
        self.lbl_files.setText(f"[{int(pct)}%] {msg}")
        self.log_line.emit(f"[{int(pct)}%] {msg}")

    def _on_install_done(self, status) -> None:
        self._finish_install_ui()
        if isinstance(status, BundleStatus):
            self._bundle = status
            lines = bundle_summary_lines(status)
            self.lbl_files.setText("\n".join(lines))
            for line in lines:
                self.log_line.emit(line)
            if status.ready:
                QMessageBox.information(
                    self, "安装完成",
                    "DLSS5 运行时安装完成并通过校验。\n"
                    + (f"DLSSNR 版本: {status.nr_version}" if status.nr_version else ""),
                )
            else:
                QMessageBox.warning(
                    self, "安装完成（有警告）",
                    "运行时已提取，但存在警告：\n"
                    + "\n".join(status.problems or [status.detail]),
                )
        self.refresh(force=True)

    def _on_install_error(self, message: str) -> None:
        self._finish_install_ui()
        self.lbl_files.setText(f"安装失败：{message}")
        self.log_line.emit(f"安装失败: {message}")
        QMessageBox.warning(
            self, "安装失败",
            message + "\n\n可尝试：\n"
            "1. 到「开发环境」更换 GitHub 代理后重试\n"
            "2. 点「打开下载页」手动下载 zip，解压后在上方「目录」指定",
        )

    def _finish_install_ui(self) -> None:
        self.btn_download.setEnabled(True)
        self.btn_cancel_install.setVisible(False)

    def _on_open_release(self) -> None:
        engine = get_engine(self.current_engine_id())
        if engine and engine.download_page:
            open_url(self, engine.download_page)

    def _show_hashes(self) -> None:
        target = resolve_bundle_dir(self.edit_dir.text().strip())
        self.log_begin.emit("upscale", "DLSS5 运行时文件哈希校验")
        self.lbl_files.setText("正在计算 SHA256（158 MB 的 DLSSNR 较慢，请稍候）…")
        self.repaint()
        st = inspect_bundle(target, with_hashes=True)
        lines = bundle_summary_lines(st)
        for f in st.files:
            if not f.exists:
                continue
            match = f.sha256_match
            mark = {True: "✅ 与已测试构建一致", False: "⚠ 与已测试构建不同", None: "—"}[match]
            lines.append(f"  {f.rel}: {f.sha256 or '计算失败'}  [{mark}]")
        self.lbl_files.setText("\n".join(lines))
        for line in lines:
            self.log_line.emit(line)
        self._bundle = st

    # ══════════════════════════════════════════════════════════
    #  关闭协调（§5.4 规则 5：有界等待后台线程）
    # ══════════════════════════════════════════════════════════

    def wait_workers(self, wait_ms: int = 3000) -> None:
        for attr in ("_worker", "_install_worker"):
            w = getattr(self, attr, None)
            if w is not None and w.isRunning():
                try:
                    w.cancel()
                except Exception:
                    pass
                if not w.wait(wait_ms):
                    try:
                        w.terminate()
                        w.wait(500)
                    except RuntimeError:
                        pass
                setattr(self, attr, None)


__all__ = ["UpscaleEngineRoute"]
