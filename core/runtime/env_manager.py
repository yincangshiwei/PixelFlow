"""
运行时环境管理器

设计要点：
- 主程序（含打包 exe）保持轻量，不内嵌 torch / ben2
- 每个抠图模型使用独立的 uv 虚拟环境，避免依赖版本冲突
- 推理时通过子进程调用对应环境的 python + worker 脚本
- 可自动检测系统 Python；可自动下载安装 uv（到 runtime/uv/）
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import config


ProgressCb = Callable[[str, float, str], None]  # stage, percent, message

# 默认 PyPI 镜像（仅用于本应用 `uv pip install -i …`，不改用户全局 pip/uv 配置）
DEFAULT_PIP_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"

# 常用镜像快捷项（UI 下拉用）
PIP_INDEX_PRESETS: tuple[tuple[str, str], ...] = (
    ("清华大学", "https://pypi.tuna.tsinghua.edu.cn/simple"),
    ("阿里云", "https://mirrors.aliyun.com/pypi/simple"),
    ("中科大", "https://pypi.mirrors.ustc.edu.cn/simple"),
    ("豆瓣", "https://pypi.douban.com/simple"),
    ("官方 PyPI", "https://pypi.org/simple"),
)

# GitHub 访问代理（前缀模式：proxy + 原始 https URL）
# 用于 git+https://github.com/... 依赖与从 GitHub 下载资源；不改用户全局 git 配置
DEFAULT_GITHUB_PROXY = "https://ghfast.top/"

GITHUB_PROXY_PRESETS: tuple[tuple[str, str], ...] = (
    ("ghfast（推荐）", "https://ghfast.top/"),
    ("ghproxy", "https://mirror.ghproxy.com/"),
    ("gitclone", "https://gitclone.com/"),
    ("不使用代理（直连 GitHub）", ""),
)

# Git for Windows 官方下载页
GIT_DOWNLOAD_URL = "https://git-scm.com/downloads/win"

# PyTorch 官方 wheel / GPU 系列清单（见 gpu_catalog）
from core.runtime.gpu_catalog import (  # noqa: E402
    PYTORCH_WHL_BASE,
    DEFAULT_CUDA_TAG as DEFAULT_TORCH_CUDA_TAG,
    GpuMatchResult,
    catalog_markdown,
    match_gpu,
)

# 需要走 PyTorch 专用索引的包名（小写）
_TORCH_INDEX_PACKAGES = frozenset({"torch", "torchvision", "torchaudio"})
# 驱动下载页（提示用户升级）
NVIDIA_DRIVER_URL = "https://www.nvidia.com/Download/index.aspx"

# Microsoft VC++ 2015–2022 可再发行组件（x64）官方说明页
VC_REDIST_HELP_URL = (
    "https://learn.microsoft.com/zh-cn/cpp/windows/latest-supported-vc-redist"
)
# 直接下载（x64）；安装后建议重启
VC_REDIST_X64_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"

# System32 中 msvcp140/vcruntime140 的最低建议版本（VS 2022 运行库量级）
# 低于此版本时，PyTorch 等原生扩展常见 WinError 1114 / c10.dll 加载失败
_VC_MIN_OK: tuple[int, int, int] = (14, 40, 0)
# 能凑合但偏旧
_VC_MIN_WARN: tuple[int, int, int] = (14, 30, 0)


# ── 数据结构 ──

@dataclass
class PythonInfo:
    path: str = ""
    version: str = ""
    major: int = 0
    minor: int = 0
    executable_ok: bool = False
    is_64bit: bool = True
    note: str = ""

    @property
    def version_tuple(self) -> tuple[int, int]:
        return self.major, self.minor

    @property
    def display(self) -> str:
        if not self.path:
            return "未检测到"
        bit = "64-bit" if self.is_64bit else "32-bit"
        return f"{self.version} ({bit})  ·  {self.path}"


@dataclass
class UvInfo:
    path: str = ""
    version: str = ""
    found: bool = False
    source: str = ""  # path / bundled / not_found

    @property
    def display(self) -> str:
        if not self.found:
            return "未安装"
        return f"{self.version or 'ok'}  ·  {self.path}  [{self.source}]"


@dataclass
class GitInfo:
    """本机 Git 客户端检测结果（安装 git+https 依赖必需）。"""
    path: str = ""
    version: str = ""
    found: bool = False

    @property
    def display(self) -> str:
        if not self.found:
            return "未安装"
        ver = self.version or "ok"
        return f"{ver}  ·  {self.path}" if self.path else ver


@dataclass
class ModelEnvStatus:
    model_id: str
    env_dir: str
    python_path: str = ""
    ready: bool = False
    python_version: str = ""
    missing_packages: list[str] = field(default_factory=list)
    detail: str = ""
    # 失败原因分类："" / missing / dll / probe / other
    fail_kind: str = ""
    # 隔离环境内 torch 构建信息（校验后填充）
    torch_version: str = ""
    torch_cuda_available: bool = False
    torch_cuda_version: str = ""
    torch_build: str = ""  # cuda / cpu / unknown / ""


@dataclass
class NvidiaGpuEntry:
    """单张 NVIDIA GPU 的探测结果（多卡机器逐张记录）。"""
    index: int = 0
    name: str = ""
    driver_version: str = ""
    compute_cap: str = ""
    compute_major: int = 0
    compute_minor: int = 0
    memory_mb: int = 0

    @property
    def sm_tag(self) -> str:
        if self.compute_major <= 0:
            return ""
        return f"sm_{self.compute_major}{self.compute_minor}"

    @property
    def memory_gb(self) -> float:
        return round(self.memory_mb / 1024.0, 2) if self.memory_mb > 0 else 0.0


@dataclass
class NvidiaGpuInfo:
    """本机 NVIDIA 驱动/GPU 探测（不依赖 torch，供安装策略使用）。"""
    available: bool = False
    gpu_names: list[str] = field(default_factory=list)
    driver_version: str = ""
    # nvidia-smi 报告的「最高支持 CUDA」版本，如 "12.6"
    cuda_version: str = ""
    cuda_major: int = 0
    cuda_minor: int = 0
    # 计算能力，如 12.0 → Blackwell sm_120
    # 注意：多卡机器上这是「能力最高的一张」，不是 nvidia-smi 的第一行
    compute_cap: str = ""
    compute_major: int = 0
    compute_minor: int = 0
    detail: str = ""
    # 逐卡明细（多卡机器：核显+独显 / 新旧卡混插时用于正确判定能力）
    gpus: list[NvidiaGpuEntry] = field(default_factory=list)

    @property
    def primary_name(self) -> str:
        return self.gpu_names[0] if self.gpu_names else ""

    @property
    def best_gpu(self) -> NvidiaGpuEntry | None:
        """计算能力最高的一张卡（与 compute_cap 字段一致）。"""
        best: NvidiaGpuEntry | None = None
        for g in self.gpus:
            if best is None or (g.compute_major, g.compute_minor) > (
                best.compute_major, best.compute_minor
            ):
                best = g
        return best

    @property
    def max_memory_mb(self) -> int:
        return max((g.memory_mb for g in self.gpus), default=0)

    @property
    def sm_tag(self) -> str:
        """如 sm_120、sm_89；未知则空。"""
        if self.compute_major <= 0:
            return ""
        return f"sm_{self.compute_major}{self.compute_minor}"

    @property
    def display(self) -> str:
        if not self.available:
            return self.detail or "未检测到 NVIDIA GPU"
        name = self.primary_name or "NVIDIA GPU"
        bits = [name]
        if self.driver_version:
            bits.append(f"驱动 {self.driver_version}")
        if self.cuda_version:
            bits.append(f"CUDA≤{self.cuda_version}")
        if self.sm_tag:
            bits.append(self.sm_tag)
        return " · ".join(bits)


@dataclass
class TorchInstallPlan:
    """创建/修复环境时的 torch 安装方案。"""
    flavor: str = "cpu"           # cuda / cpu
    cuda_tag: str = ""            # cu124 / cu126 / …
    index_url: str = ""           # 空=走普通 PyPI 镜像
    reason: str = ""
    nvidia: NvidiaGpuInfo | None = None
    # 主流系列匹配
    match: GpuMatchResult | None = None
    candidate_tags: list[str] = field(default_factory=list)
    driver_ok: bool = True
    driver_hint: str = ""

    @property
    def display(self) -> str:
        if self.flavor == "cuda":
            return f"CUDA 版 ({self.cuda_tag})"
        return "CPU 版"


# nvidia-smi 探测缓存
_nvidia_cache: NvidiaGpuInfo | None = None
_nvidia_cache_lock = threading.Lock()


def detect_nvidia_gpu(*, force: bool = False) -> NvidiaGpuInfo:
    """
    通过 nvidia-smi 检测本机 NVIDIA GPU（不依赖 torch）。
    用于决定创建模型环境时安装 CUDA 版还是 CPU 版 PyTorch。
    """
    global _nvidia_cache
    with _nvidia_cache_lock:
        if not force and _nvidia_cache is not None:
            return _nvidia_cache

    info = NvidiaGpuInfo()
    smi = shutil.which("nvidia-smi")
    if not smi:
        info.detail = "未找到 nvidia-smi（无 NVIDIA 驱动或不在 PATH）"
        with _nvidia_cache_lock:
            _nvidia_cache = info
        return info

    # 查询 GPU 名称 + 驱动 + 计算能力 + 显存
    # 逐级降级：老驱动的 nvidia-smi 可能不支持 compute_cap / memory.total，
    # 此时仍要拿到 GPU 名称，让上层可用名称正则兜底判定架构（高清放大门禁依赖此路径）
    field_sets = (
        "name,driver_version,compute_cap,memory.total",
        "name,driver_version,compute_cap",
        "name,driver_version",
    )
    r = None
    last_err = ""
    for fields in field_sets:
        try:
            cand = _run(
                [smi, f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
                timeout=15,
            )
        except Exception as e:
            info.detail = f"nvidia-smi 执行失败: {e}"
            with _nvidia_cache_lock:
                _nvidia_cache = info
            return info
        if cand.returncode == 0:
            r = cand
            break
        last_err = _strip_ansi((cand.stderr or cand.stdout or "")[:300])

    if r is None:
        info.detail = f"nvidia-smi 返回错误: {last_err or '未知'}"
        with _nvidia_cache_lock:
            _nvidia_cache = info
        return info

    names: list[str] = []
    entries: list[NvidiaGpuEntry] = []
    driver = ""
    for idx, line in enumerate((r.stdout or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if not parts or not parts[0]:
            continue
        entry = NvidiaGpuEntry(index=idx, name=parts[0])
        if len(parts) > 1 and parts[1]:
            entry.driver_version = parts[1]
            if not driver:
                driver = parts[1]
        if len(parts) > 2 and parts[2]:
            entry.compute_cap = parts[2]
            m_cc = re.match(r"(\d+)\.(\d+)", parts[2])
            if m_cc:
                entry.compute_major = int(m_cc.group(1))
                entry.compute_minor = int(m_cc.group(2))
        if len(parts) > 3 and parts[3]:
            try:
                entry.memory_mb = int(float(parts[3]))
            except (TypeError, ValueError):
                entry.memory_mb = 0
        names.append(entry.name)
        entries.append(entry)

    if not names:
        info.detail = "nvidia-smi 未列出 GPU"
        with _nvidia_cache_lock:
            _nvidia_cache = info
        return info

    info.available = True
    info.gpu_names = names
    info.gpus = entries
    info.driver_version = driver

    # 多卡机器（核显+独显 / 新旧卡混插）：以「计算能力最高的一张」为准，
    # 否则第一张是旧卡时会把整机误判为旧架构（影响 CUDA 标签选择与高清放大门禁）
    best: NvidiaGpuEntry | None = None
    for g in entries:
        if best is None or (g.compute_major, g.compute_minor) > (
            best.compute_major, best.compute_minor
        ):
            best = g
    if best is not None and best.compute_cap:
        info.compute_cap = best.compute_cap
        info.compute_major = best.compute_major
        info.compute_minor = best.compute_minor

    # 头部 CUDA Version（驱动支持的最高 CUDA）
    try:
        r2 = _run([smi], timeout=15)
        text = (r2.stdout or "") + "\n" + (r2.stderr or "")
        m = re.search(r"CUDA\s+Version\s*:\s*(\d+)\.(\d+)", text, re.I)
        if m:
            info.cuda_major = int(m.group(1))
            info.cuda_minor = int(m.group(2))
            info.cuda_version = f"{info.cuda_major}.{info.cuda_minor}"
    except Exception:
        pass

    info.detail = info.display
    with _nvidia_cache_lock:
        _nvidia_cache = info
    return info


def match_local_gpu(*, force_detect: bool = False) -> GpuMatchResult:
    """检测本机 GPU 并套用主流系列匹配清单。"""
    nv = detect_nvidia_gpu(force=force_detect)
    return match_gpu(
        has_gpu=bool(nv.available),
        gpu_name=nv.primary_name,
        compute_cap=nv.compute_cap,
        compute_major=nv.compute_major,
        compute_minor=nv.compute_minor,
        driver_version=nv.driver_version,
        driver_cuda=nv.cuda_version,
        driver_cuda_major=nv.cuda_major,
        driver_cuda_minor=nv.cuda_minor,
    )


def resolve_torch_cuda_tag(
    cuda_major: int,
    cuda_minor: int,
    *,
    compute_major: int = 0,
    compute_minor: int = 0,
    gpu_name: str = "",
) -> str:
    """按主流系列清单选择最优 CUDA 标签。"""
    m = match_gpu(
        has_gpu=True,
        gpu_name=gpu_name,
        compute_major=compute_major,
        compute_minor=compute_minor,
        driver_cuda_major=cuda_major,
        driver_cuda_minor=cuda_minor,
    )
    return m.primary_tag or DEFAULT_TORCH_CUDA_TAG


def resolve_torch_cuda_tag_candidates(
    cuda_major: int,
    cuda_minor: int,
    *,
    compute_major: int = 0,
    compute_minor: int = 0,
    gpu_name: str = "",
) -> list[str]:
    """返回按优先级排列的 CUDA 标签候选（主选 + 备选）。"""
    m = match_gpu(
        has_gpu=True,
        gpu_name=gpu_name,
        compute_major=compute_major,
        compute_minor=compute_minor,
        driver_cuda_major=cuda_major,
        driver_cuda_minor=cuda_minor,
    )
    tags = list(m.candidate_tags or [])
    if m.primary_tag and m.primary_tag not in tags:
        tags.insert(0, m.primary_tag)
    return tags or [DEFAULT_TORCH_CUDA_TAG]


def plan_torch_install(*, force_detect: bool = False) -> TorchInstallPlan:
    """
    决定安装 CUDA 版还是 CPU 版 torch。
    有 NVIDIA 驱动 → 按主流系列清单选最优/备选 CUDA 标签；
    否则 → CPU 版。一份 CUDA 版即可，配置里仍可强制用 CPU。
    """
    nv = detect_nvidia_gpu(force=force_detect)
    matched = match_gpu(
        has_gpu=bool(nv.available),
        gpu_name=nv.primary_name,
        compute_cap=nv.compute_cap,
        compute_major=nv.compute_major,
        compute_minor=nv.compute_minor,
        driver_version=nv.driver_version,
        driver_cuda=nv.cuda_version,
        driver_cuda_major=nv.cuda_major,
        driver_cuda_minor=nv.cuda_minor,
    )
    if not nv.available:
        return TorchInstallPlan(
            flavor="cpu",
            reason=nv.detail or "未检测到 NVIDIA GPU，安装 CPU 版 PyTorch",
            nvidia=nv,
            match=matched,
            driver_ok=True,
        )

    tag = matched.primary_tag or DEFAULT_TORCH_CUDA_TAG
    reason = matched.summary or f"安装 CUDA 版 PyTorch（{tag}）"
    if matched.notes:
        reason = f"{reason}。{matched.notes}"
    return TorchInstallPlan(
        flavor="cuda",
        cuda_tag=tag,
        index_url=f"{PYTORCH_WHL_BASE}/{tag}",
        reason=reason,
        nvidia=nv,
        match=matched,
        candidate_tags=list(matched.candidate_tags or [tag]),
        driver_ok=bool(matched.driver_ok_for_series),
        driver_hint=matched.driver_hint or "",
    )


def _load_matting_model_info(model_id: str):
    """
    加载模型注册表元数据，避免 `import core.matting` 连带拉取 PIL/torch 等重依赖。
    （打包主程序可能无 PIL；AI 指令生成只需注册表。）
    """
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "matting" / "model_registry.py"
    spec = importlib.util.spec_from_file_location(
        "pixelflow_matting_model_registry_lite", path
    )
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    getter = getattr(mod, "get_model_info", None)
    if getter is None:
        return None
    return getter(model_id)


def build_ai_setup_prompt(
    model_id: str,
    *,
    force_recreate: bool = True,
) -> str:
    """
    生成可复制给 Codex / CodeBuddy 等 AI 工具的「通用」环境安装任务说明。

    结构原则：
    1. 先写目标、原则、决策流程（不写死某台机器的 CUDA 标签）
    2. 要求 AI 自行复核本机环境与最新 PyTorch 兼容性（应用内清单可能过时）
    3. 附录附上 PixelFlow 探测到的本机快照，供参考，可跳过重复探测
    """
    rt = get_runtime_manager()
    try:
        meta = _load_matting_model_info(model_id)
    except Exception:
        meta = None
    plan = plan_torch_install(force_detect=True)
    matched = plan.match or match_local_gpu(force_detect=True)
    uv = rt.resolve_uv()
    base = rt.resolve_base_python()
    env_dir = rt.model_env_dir(model_id)
    venv_dir = rt.model_venv_dir(model_id)
    py_venv = rt.model_python(model_id)
    py_venv_str = str(py_venv) if py_venv else str(
        venv_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    )
    pkgs = list(meta.env_packages) if meta else ["torch", "torchvision"]
    torch_pkgs, rest_pkgs = split_torch_packages(pkgs)
    pypi = rt.get_pip_index_url() or DEFAULT_PIP_INDEX_URL
    try:
        project_root = Path(getattr(config, "DATA_DIR", None) or config.BASE_DIR)
    except Exception:
        project_root = Path.cwd()

    model_name = meta.name if meta else model_id
    py_ver = meta.python_version if meta else "3.12"
    check_mods = list(meta.env_check_packages) if meta else ["torch"]
    uv_bin = (uv.path if uv and uv.found else "") or "uv"
    base_py = (base.path if base else "") or "python"
    is_win = sys.platform == "win32"
    shell_name = "PowerShell" if is_win else "bash"

    L: list[str] = []
    a = L.append

    # ── 通用任务（主体）──
    a("# PixelFlow AI 抠图 — 隔离环境帮装任务")
    a("")
    a("你是协助用户配置 **PixelFlow** 桌面应用 AI 抠图依赖的工程师。")
    a("请阅读全文后：**先复核环境 → 再决策 CUDA/CPU → 再安装 → 再验收**。")
    a("不要只机械执行固定命令；应用内的 GPU/CUDA 清单可能滞后于新显卡或新 PyTorch 版本。")
    a("")
    a("## 1. 你要达成什么")
    a("")
    a("为指定抠图模型创建（或修复）**独立 uv 虚拟环境**，使 PixelFlow 能通过子进程调用该环境做推理。")
    a("")
    a("成功标准（全部满足）：")
    a("1. 虚拟环境位于项目 `runtime/envs/<model_id>/.venv`，**不要**装进系统 Python 或主程序 venv。")
    a("2. 环境内可 import 模型所需依赖（含 torch 及模型包）。")
    a("3. 若本机有可用 NVIDIA GPU：安装的是 **CUDA 版** torch，且 `torch.cuda.is_available()` 为 True；")
    a("   `torch.cuda.get_arch_list()`（或等价信息）**包含本机 GPU 的 sm/compute 架构**，")
    a("   不会出现 `no kernel image is available for execution on the device`。")
    a("4. 若无 NVIDIA / 驱动过旧且用户同意：可安装 CPU 版 torch，并明确告知只能 CPU 推理。")
    a("5. 向用户用中文简要说明：装了什么、为何选该 CUDA 标签、如何在 PixelFlow 里验证。")
    a("")
    a("## 2. 架构背景（必读）")
    a("")
    a("- PixelFlow 主程序**不内嵌** torch；每个抠图模型一个隔离环境：`runtime/envs/<model_id>/`。")
    a("- 推理：主程序启动该环境的 `python` + `core/matting/workers/*_worker.py`。")
    a("- **一份** CUDA 版 torch 即可；应用配置里仍可强制用 CPU。不必 CPU+CUDA 各装一套。")
    a("- CUDA 版 wheel **自带** CUDA 运行时，一般**不必**本机再装 CUDA Toolkit；需要的是匹配的 **NVIDIA 驱动**。")
    a("- `torch` / `torchvision` 必须从 **PyTorch 官方 CUDA 索引**安装，例如：")
    a(f"  `{PYTORCH_WHL_BASE}/cuXXX`")
    a("  **禁止**仅用清华/阿里等普通 PyPI 镜像装 torch（容易变成 `+cpu`，或缺少新架构内核）。")
    a("- 其它依赖（numpy、模型包等）可用用户配置的 PyPI 镜像加速。")
    a("- Windows 上 torch 还依赖 **VC++ 2015–2022 x64**；若 `WinError 1114` / `c10.dll`，先修运行库再装包。")
    a(f"  下载: {VC_REDIST_X64_URL}")
    a("")
    a("## 3. 决策原则（通用，勿写死某一代显卡）")
    a("")
    a("### 3.1 有没有 NVIDIA GPU？")
    a("- 有 `nvidia-smi` 且能列出 GPU → 走 CUDA 路径（目标）。")
    a("- 否则 → CPU 版 torch。")
    a("")
    a("### 3.2 选哪个 CUDA 构建标签（cu118 / cu121 / cu124 / cu126 / cu129 / cu130 / cu132 / 更新）？")
    a("按下面顺序思考，**以你查到的最新官方信息为准**：")
    a("1. 读本机：`nvidia-smi` → GPU 名称、驱动版本、右上角 CUDA Version、`compute_cap`（计算能力）。")
    a("2. 查 **https://pytorch.org/get-started/locally/** 当前推荐的 pip/uv 安装命令与可用 `cuXXX` 索引。")
    a("3. 确认该 wheel 是否支持本机 **compute capability / sm_xx**（尤其新架构如 Blackwell sm_120 等）。")
    a("   - 旧构建常见报错：`no kernel image is available` / `not compatible with the current PyTorch installation`。")
    a("   - 处理：换**更新的** `cuXXX` 索引重装 torch，或提示用户升级驱动后再装。")
    a("4. 驱动 CUDA Version 必须 ≥ 该 wheel 要求；不够则：**先升级 NVIDIA 驱动并重启**，不要硬装必失败的组合。")
    a("   驱动下载: " + NVIDIA_DRIVER_URL)
    a("5. 优选：在「驱动允许 + 架构支持」的标签里选**尽量新且稳定**的官方构建。")
    a("6. 备选：同系列次新标签逐个试；全部 CUDA 失败再 CPU 保底，并说明原因。")
    a("")
    a("### 3.3 关于附录里的「应用内建议」")
    a("- 文末附录是 PixelFlow **内置清单 + 本机快照**，可能过时或未收录最新 GPU。")
    a("- **可以参考，不能当唯一真理。** 若与你复核结果冲突，以你的实测 + PyTorch 官网为准，并告诉用户差异。")
    a("- 若附录硬件信息看起来可信，可减少重复探测，但仍建议做一次快速校验（`nvidia-smi`、装后 `import torch`）。")
    a("")
    a("## 4. 推荐工作流")
    a("")
    a(f"以下路径/命令针对**当前用户这次导出的任务**；请在 {shell_name} 中执行，并可按复核结果调整。")
    a("")
    a("### 步骤 A — 复核环境（请先做）")
    a("1. 确认工作目录为项目根（见附录）。")
    a("2. 检查：`uv --version`；没有则安装 uv，或使用附录中的 uv 路径。")
    a("3. 检查 Python：**必须** 3.10–3.12 **64-bit**（推荐精确 " + py_ver + "）。")
    a("   - **禁止** 用 3.13/3.14 建环境：当前 PyTorch 官方 wheel 无 cp313/cp314，会报 ABI 不匹配并误退 CPU。")
    a("   - 本机没有 3.10–3.12 时，优先：`uv venv --python " + py_ver + "`（让 uv 托管下载）。")
    a("4. Windows：关注 VC++ 是否正常（见上）。")
    a("5. GPU：运行 `nvidia-smi`（及可选 `--query-gpu=name,driver_version,compute_cap --format=csv`）。")
    a("6. 对照附录快照：若一致可沿用；若不一致或 GPU 为清单未覆盖的新系列，按第 3 节重新决策。")
    a("7. 打开 PyTorch 官网确认当前推荐的 `cuXXX` 与安装命令。")
    a("")
    a("### 步骤 B — 准备虚拟环境")
    a(f"- 模型 ID: `{model_id}`（显示名: {model_name}）")
    a(f"- 环境目录: 附录中的 env_dir / venv_dir")
    a(f"- 目标 Python: {py_ver}（64-bit，严格 ≤3.12）")
    if force_recreate:
        a("- 用户选择了倾向「重建」：若旧 venv 存在且依赖混乱，可删除 `.venv` 后重建；")
        a("  若仅需换 torch 构建，也可只 uninstall/reinstall torch，不必强删全环境。")
    else:
        a("- 以修复/升级为主：尽量保留环境，重装缺失或错误的包。")
    a("- 创建示例（**优先版本号**，路径以附录为准）：")
    a("```")
    a(f"\"{uv_bin}\" venv --python {py_ver} \"<venv_dir>\"")
    a(f"# 本机已有精确 {py_ver} 时也可: \"{uv_bin}\" venv --python \"{base_py}\" \"<venv_dir>\"")
    a("```")
    a("- 创建后务必确认：`\"<venv_python>\" -c \"import sys; print(sys.version)\"` 为 3.10–3.12。")
    a("- 之后所有 pip 都通过：`uv pip install --python \"<venv_python>\" ...`")
    a("")
    a("### 步骤 C — 安装 PyTorch")
    a("- CUDA 路径模板：")
    a("```")
    a(
        f"\"{uv_bin}\" pip install --python \"<venv_python>\" --upgrade "
        f"--index-url {PYTORCH_WHL_BASE}/<cuXXX> "
        + " ".join(torch_pkgs or ["torch", "torchvision"])
    )
    a("```")
    a("  将 `<cuXXX>` 换成你在步骤 A 选定的标签（如 cu126、cu129、cu132…）。")
    a("- CPU 路径模板（无 GPU 或 CUDA 全失败时）：")
    a("```")
    a(
        f"\"{uv_bin}\" pip install --python \"<venv_python>\" --upgrade "
        f"-i {pypi} "
        + " ".join(torch_pkgs or ["torch", "torchvision"])
    )
    a("```")
    a("- **每装完一个 CUDA 候选立刻验收**，不要连装多个再查：")
    a("```")
    a(
        "\"<venv_python>\" -c \"import torch; print(torch.__version__); "
        "print('cuda', torch.cuda.is_available()); "
        "print('cuda_ver', getattr(torch.version,'cuda',None)); "
        "print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'n/a'); "
        "print(torch.cuda.get_device_capability(0) if torch.cuda.is_available() else 'n/a'); "
        "print(torch.cuda.get_arch_list() if torch.cuda.is_available() else [])\""
    )
    a("```")
    a("- 验收要点：")
    a("  - 版本应类似 `x.y.z+cuXXX`（CUDA 构建）或明确的 cuda 构建，而不是误装的纯 `+cpu`（若目标是 GPU）。")
    a("  - `cuda.is_available()` 为 True（GPU 目标时）。")
    a("  - arch 列表覆盖本机 compute capability；有不兼容警告则换标签或升级驱动。")
    a("- 切换标签前先卸载：")
    a("```")
    a(f"\"{uv_bin}\" pip uninstall --python \"<venv_python>\" -y torch torchvision")
    a("```")
    a("")
    a("### 步骤 D — 安装其余模型依赖")
    a("- 依赖列表见附录「模型依赖」。")
    a("- 使用 PyPI 镜像安装非 torch 包，例如：")
    a("```")
    a(f"\"{uv_bin}\" pip install --python \"<venv_python>\" --upgrade -i {pypi} <packages...>")
    a("```")
    a("- 含 `git+https://...` 的包需要本机已安装 Git，且能访问对应 git 托管。")
    gh_proxy = rt.get_github_proxy()
    if gh_proxy:
        a(f"- 当前 GitHub 代理: `{gh_proxy}`（安装时会改写 github.com 地址，不改用户全局 git 配置）。")
        a("- 也可手动把 `git+https://github.com/...` 改成 `git+{proxy}https://github.com/...` 再装。")
    else:
        a("- 当前未启用 GitHub 代理（直连）；国内网络失败时可改用 ghfast 等前缀代理。")
    a("- 失败时说明原因并给替代方案（换代理 / 检查 Git / 确认仓库地址）。")
    a("")
    a("### 步骤 E — 最终验收与收尾")
    a("- 批量 import 检查（模块名见附录 env_check_packages）。")
    a("- 再次打印 torch 版本与 CUDA 状态。")
    a("- 告诉用户回到 PixelFlow：")
    a("  「配置 → 抠图模型」→ 刷新状态 → 推理设备选「自动 (优先 GPU)」→ 试跑一张图。")
    a("  成功日志应出现实际设备 `CUDA`（或用户强制时的 `CPU`）。")
    a("- 若应用内状态仍显示旧构建，可再点一次应用内「创建/修复环境」同步 meta，或重启应用。")
    a("")
    a("## 5. 约束与禁止事项")
    a("")
    a("- 不要污染系统 Python / conda base / 主程序环境。")
    a("- 不要把「普通 PyPI 镜像」当作 torch CUDA 轮子的唯一来源。")
    a("- 不要在未经验收时声称 GPU 已可用。")
    a("- 不要忽略新 GPU 架构；清单没有的系列必须查官网与实测。")
    a("- 修改范围仅限：uv、该模型 `runtime/envs/<model_id>`、必要时提示用户装驱动/VC++。")
    a("")
    a("---")
    a("")
    a("## 附录 A — 本次任务定位（路径与模型）")
    a("")
    a("> 以下由 PixelFlow 导出，便于你定位目录；**CUDA 标签仍以你复核为准**。")
    a("")
    a(f"- 项目/数据根目录: `{project_root}`")
    a(f"- 模型: {model_name} (`{model_id}`)")
    a(f"- 环境目录 env_dir: `{env_dir}`")
    a(f"- 虚拟环境 venv_dir: `{venv_dir}`")
    a(f"- 预期 venv python: `{py_venv_str}`")
    a(f"- 目标 Python 版本: {py_ver}")
    a(f"- 平台: {sys.platform}")
    a(f"- 用户倾向: {'重建环境' if force_recreate else '创建/修复'}")
    a("")
    a("## 附录 B — 本机快照（PixelFlow 探测，供参考）")
    a("")
    a("> 导出时刻的探测结果。请快速核对；过时或失败时以你现场命令为准。")
    a("")
    if matched.has_gpu:
        a(f"- NVIDIA GPU: {matched.gpu_name or '（已检测到，名称未知）'}")
        if matched.driver_version:
            a(f"- 驱动版本: {matched.driver_version}")
        if matched.driver_cuda:
            a(f"- 驱动报告 CUDA Version ≤: {matched.driver_cuda}")
        if matched.compute_cap or matched.sm_tag:
            a(
                f"- 计算能力: {matched.compute_cap or '?'} "
                f"({matched.sm_tag or 'sm_?'})"
            )
        a(f"- 应用内系列匹配: {matched.series_name} [{matched.match_method}]")
        if matched.primary_tag:
            a(f"- 应用内建议优选标签: {matched.primary_tag}（仅供参考）")
        if matched.candidate_tags:
            a("- 应用内建议备选: " + " → ".join(matched.candidate_tags) + "（仅供参考）")
        if matched.blocked_by_driver:
            a("- 应用内认为驱动不足的标签: " + ", ".join(matched.blocked_by_driver))
        if matched.driver_hint:
            a(f"- 应用内驱动提示: {matched.driver_hint}")
        if matched.notes:
            a(f"- 应用内系列备注: {matched.notes}")
        a(f"- 应用内安装倾向: {plan.display}")
    else:
        a("- 应用内未检测到 NVIDIA GPU → 倾向 CPU 版 torch（请仍用 nvidia-smi 复核）")
    a("")
    a(f"- 应用检测到的 uv: {uv.display if uv else '未检测'}")
    a(f"- 应用检测到的基础 Python: {base.display if base else '未检测'}")
    a(f"- 非 torch 依赖建议镜像: {pypi}")
    a("")
    a("## 附录 C — 模型依赖清单")
    a("")
    a("### 需走 PyTorch 官方索引的包")
    a("- " + (", ".join(torch_pkgs) if torch_pkgs else "torch, torchvision"))
    a("")
    a("### 其余包（可用 PyPI 镜像）")
    if rest_pkgs:
        for p in rest_pkgs:
            a(f"- `{p}`")
    else:
        a("- （无）")
    a("")
    a("### 验收时应能 import 的名称")
    a("- " + ", ".join(check_mods))
    a("")
    a("## 附录 D — 应用内置 GPU↔CUDA 清单（可能滞后）")
    a("")
    a("以下是 PixelFlow 源码中的静态表，**新系列显卡可能未收录**。")
    a("仅作历史参考；最终以 PyTorch 官网与本机实测为准。")
    a("")
    a(catalog_markdown())
    a("")
    a("## 附录 E — 常用命令速查")
    a("")
    a("```")
    a("# 工作目录")
    a(f"cd \"{project_root}\"")
    a("")
    a("# GPU 信息")
    a("nvidia-smi")
    a(
        "nvidia-smi --query-gpu=name,driver_version,compute_cap "
        "--format=csv,noheader"
    )
    a("")
    a("# 创建 venv（示例）")
    a(f"\"{uv_bin}\" venv --python \"{base_py}\" \"{venv_dir}\"")
    a("")
    a("# CUDA torch（把 cuXXX 换成你选定的标签）")
    a(
        f"\"{uv_bin}\" pip install --python \"{py_venv_str}\" --upgrade "
        f"--index-url {PYTORCH_WHL_BASE}/cuXXX "
        + " ".join(torch_pkgs or ["torch", "torchvision"])
    )
    a("")
    a("# CPU torch")
    a(
        f"\"{uv_bin}\" pip install --python \"{py_venv_str}\" --upgrade "
        f"-i {pypi} "
        + " ".join(torch_pkgs or ["torch", "torchvision"])
    )
    a("```")
    a("")
    a("---")
    a("")
    a("**请开始：先做步骤 A 复核，再给出你的 CUDA/CPU 决策与将要执行的命令，然后安装并验收。**")
    a("")
    return "\n".join(L)


def _package_base_name(spec: str) -> str:
    """从 pip 规格提取包名：'torch==2.x' / 'torchvision>=0.1' → torch/torchvision。"""
    s = (spec or "").strip()
    if not s or s.startswith("git+") or "://" in s:
        return ""
    # 去掉 extras: package[extra]
    s = s.split("[", 1)[0]
    for sep in ("===", "==", ">=", "<=", "~=", "!=", ">", "<"):
        if sep in s:
            s = s.split(sep, 1)[0]
            break
    return s.strip().lower()


def split_torch_packages(packages: list[str]) -> tuple[list[str], list[str]]:
    """
    将依赖列表拆成 (torch 相关, 其余)。
    torch/torchvision/torchaudio 需走 PyTorch 官方 CUDA/CPU 索引。
    """
    torch_pkgs: list[str] = []
    rest: list[str] = []
    for p in packages:
        base = _package_base_name(p)
        if base in _TORCH_INDEX_PACKAGES:
            torch_pkgs.append(p)
        else:
            rest.append(p)
    return torch_pkgs, rest


@dataclass
class VcRedistInfo:
    """Windows VC++ 运行库检测结果（PyTorch 等原生扩展强依赖）。"""
    applicable: bool = False          # 非 Windows 为 False
    ok: bool = True                   # 是否足以加载常见原生扩展
    level: str = "ok"                 # ok / warn / bad / n/a
    msvcp140_version: str = ""
    vcruntime140_version: str = ""
    registry_version: str = ""
    detail: str = ""
    fix_hint: str = ""

    @property
    def display(self) -> str:
        if not self.applicable:
            return "无需检测（非 Windows）"
        parts = []
        if self.msvcp140_version:
            parts.append(f"msvcp140={self.msvcp140_version}")
        if self.vcruntime140_version:
            parts.append(f"vcruntime140={self.vcruntime140_version}")
        if self.registry_version:
            parts.append(f"注册表={self.registry_version}")
        ver = "  ·  ".join(parts) if parts else "未找到"
        return f"{self.detail}  ·  {ver}" if self.detail else ver


# ── 工具函数 ──

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_VER_NUM_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _strip_ansi(text: str) -> str:
    if not text:
        return ""
    return _ANSI_RE.sub("", text)


def _parse_version_tuple(text: str) -> tuple[int, int, int] | None:
    """从 '14.51.36247.0' / '14.00.24215.1 built by: …' 解析 (major, minor, build)。"""
    if not text:
        return None
    m = _VER_NUM_RE.search(str(text))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _file_version(path: Path) -> str:
    """读取 Windows PE 文件版本字符串；失败返回空。优先 VS_FIXEDFILEINFO（与语言无关）。"""
    if not path.is_file():
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        class VS_FIXEDFILEINFO(ctypes.Structure):
            _fields_ = [
                ("dwSignature", wintypes.DWORD),
                ("dwStrucVersion", wintypes.DWORD),
                ("dwFileVersionMS", wintypes.DWORD),
                ("dwFileVersionLS", wintypes.DWORD),
                ("dwProductVersionMS", wintypes.DWORD),
                ("dwProductVersionLS", wintypes.DWORD),
                ("dwFileFlagsMask", wintypes.DWORD),
                ("dwFileFlags", wintypes.DWORD),
                ("dwFileOS", wintypes.DWORD),
                ("dwFileType", wintypes.DWORD),
                ("dwFileSubtype", wintypes.DWORD),
                ("dwFileDateMS", wintypes.DWORD),
                ("dwFileDateLS", wintypes.DWORD),
            ]

        size = ctypes.windll.version.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(str(path), 0, size, buf):
            return ""
        lptr = ctypes.c_void_p()
        llen = wintypes.UINT()
        if ctypes.windll.version.VerQueryValueW(
            buf, "\\", ctypes.byref(lptr), ctypes.byref(llen),
        ):
            info = ctypes.cast(lptr, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
            major = (info.dwFileVersionMS >> 16) & 0xFFFF
            minor = info.dwFileVersionMS & 0xFFFF
            build = (info.dwFileVersionLS >> 16) & 0xFFFF
            rev = info.dwFileVersionLS & 0xFFFF
            return f"{major}.{minor}.{build}.{rev}"
    except Exception:
        pass
    return ""


def _vc_registry_version() -> str:
    """读取已安装 VC++ 2015–2022 x64 运行库注册表版本。"""
    if sys.platform != "win32":
        return ""
    try:
        import winreg
        for root, sub in (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64"),
        ):
            try:
                key = winreg.OpenKey(root, sub)
            except OSError:
                continue
            try:
                installed, _ = winreg.QueryValueEx(key, "Installed")
                if int(installed) != 1:
                    continue
                ver, _ = winreg.QueryValueEx(key, "Version")
                return str(ver).strip()
            finally:
                winreg.CloseKey(key)
    except Exception:
        pass
    return ""


def detect_vc_redist(*, force: bool = False) -> VcRedistInfo:
    """
    检测 Windows 系统 VC++ 运行库是否足以加载 PyTorch 等原生扩展。

    常见故障：注册表显示已装 14.4x/14.5x，但 System32 下 msvcp140.dll
    仍是 14.00.24215（VS2015 旧文件）→ import torch 报 WinError 1114 / c10.dll。
    """
    global _vc_redist_cache
    if not force and _vc_redist_cache is not None:
        return _vc_redist_cache

    info = VcRedistInfo()
    if sys.platform != "win32":
        info.applicable = False
        info.ok = True
        info.level = "n/a"
        info.detail = "非 Windows，跳过 VC++ 检测"
        _vc_redist_cache = info
        return info

    info.applicable = True
    sys32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    msvcp = sys32 / "msvcp140.dll"
    vcrun = sys32 / "vcruntime140.dll"
    info.msvcp140_version = _file_version(msvcp) or ("(缺失)" if not msvcp.is_file() else "(无法读取版本)")
    info.vcruntime140_version = _file_version(vcrun) or ("(缺失)" if not vcrun.is_file() else "(无法读取版本)")
    info.registry_version = _vc_registry_version()

    msvcp_t = _parse_version_tuple(info.msvcp140_version)
    vcrun_t = _parse_version_tuple(info.vcruntime140_version)
    reg_t = _parse_version_tuple(info.registry_version)

    fix = (
        "请安装或「修复」最新 Microsoft Visual C++ 2015–2022 Redistributable (x64)，"
        f"然后重启电脑。下载: {VC_REDIST_X64_URL}\n"
        f"说明: {VC_REDIST_HELP_URL}"
    )
    info.fix_hint = fix

    if not msvcp.is_file() or not vcrun.is_file() or msvcp_t is None or vcrun_t is None:
        info.ok = False
        info.level = "bad"
        info.detail = "未找到完整的 VC++ 运行库（msvcp140 / vcruntime140）"
        _vc_redist_cache = info
        return info

    # 注册表声称较新，但 System32 主 DLL 明显偏旧 → 典型「装了但文件被旧版覆盖/损坏」
    if reg_t and msvcp_t < _VC_MIN_WARN and reg_t >= _VC_MIN_OK:
        info.ok = False
        info.level = "bad"
        info.detail = (
            f"VC++ 运行库文件异常：注册表为 {info.registry_version}，"
            f"但 System32/msvcp140.dll 仅为 {info.msvcp140_version}（过旧/损坏）。"
            "会导致 PyTorch 加载 c10.dll 失败（WinError 1114）"
        )
        _vc_redist_cache = info
        return info

    lowest = min(msvcp_t, vcrun_t)
    if lowest < _VC_MIN_WARN:
        info.ok = False
        info.level = "bad"
        info.detail = (
            f"VC++ 运行库过旧（msvcp140={info.msvcp140_version}，"
            f"vcruntime140={info.vcruntime140_version}）。"
            "PyTorch 等原生扩展无法加载"
        )
        _vc_redist_cache = info
        return info

    if lowest < _VC_MIN_OK:
        info.ok = True  # 仍允许尝试，但提示升级
        info.level = "warn"
        info.detail = (
            f"VC++ 运行库偏旧（{info.msvcp140_version}），建议升级到最新 "
            "VC++ 2015–2022 x64 以避免 torch DLL 加载失败"
        )
        _vc_redist_cache = info
        return info

    info.ok = True
    info.level = "ok"
    info.detail = "VC++ 运行库正常"
    info.fix_hint = ""
    _vc_redist_cache = info
    return info


def classify_import_errors(error_text: str) -> str:
    """
    根据依赖 import 错误文本分类：
    dll / missing / network / other
    """
    t = (error_text or "").lower()
    if not t.strip():
        return "other"
    dll_markers = (
        "winerror 1114",
        "winerror 126",
        "winerror 127",
        "dll",
        "c10.dll",
        "dynamic link library",
        "动态链接库",
        "initialization routine",
        "初始化例程",
        "error loading",
        "not find specified module",
        "找不到指定的模块",
        "importerror: dll",
        "oserror: [winerror",
    )
    if any(m in t for m in dll_markers):
        return "dll"
    if "modulenotfounderror" in t or "no module named" in t:
        return "missing"
    net_markers = (
        "timed out",
        "timeout",
        "connection",
        "network",
        "name or service not known",
        "temporary failure",
        "proxy",
        "ssl",
        "403",
        "404",
        "http",
    )
    if any(m in t for m in net_markers):
        return "network"
    return "other"


def format_env_failure_hint(
    *,
    fail_kind: str = "",
    detail: str = "",
    missing: list[str] | None = None,
    vc: VcRedistInfo | None = None,
) -> str:
    """生成面向用户的失败后续建议（避免一律「请检查网络」）。"""
    kind = (fail_kind or "").lower()
    detail_l = (detail or "").lower()
    if not kind:
        kind = classify_import_errors(detail)
    if vc is None and sys.platform == "win32":
        try:
            vc = detect_vc_redist()
        except Exception:
            vc = None

    # 系统运行库 / DLL 加载问题（不是缺包、也不优先归咎网络）
    if kind == "dll" or (vc and vc.applicable and not vc.ok):
        lines = [
            "原因更像是 DLL / 系统运行库加载失败，而不是网络或包装失败。",
        ]
        if "c10" in detail_l or "torch" in detail_l or kind == "dll":
            lines.append(
                "常见表现：依赖已安装，但 import torch 时 c10.dll 初始化失败（WinError 1114）。"
            )
        if vc and vc.applicable and not vc.ok:
            lines.append(vc.detail)
            lines.append(
                "处理：安装或「修复」Microsoft Visual C++ 2015–2022 Redistributable (x64)，"
                "重启电脑后，再点「创建/修复环境」做校验（通常无需重新下载依赖）。"
            )
            lines.append(f"下载: {VC_REDIST_X64_URL}")
        else:
            if vc and vc.applicable and vc.ok:
                lines.append(
                    f"当前 VC++ 检测为正常（{vc.msvcp140_version or '已安装'}）。"
                    "若仍失败，可尝试：1) 再修复一次 VC++ 并重启；"
                    "2) 暂时排除杀毒对 runtime/envs 的拦截；"
                    "3) 「强制重建环境」重装 torch。"
                )
            else:
                lines.append(
                    "处理：修复 VC++ 2015–2022 x64 运行库并重启；"
                    "仍失败可强制重建环境或检查杀毒软件。"
                )
                lines.append(f"下载: {VC_REDIST_X64_URL}")
        return "\n".join(lines)

    if kind == "network":
        return (
            "原因更像是网络或镜像源不可达。\n"
            "请检查网络，或到「开发环境」更换 PyPI 镜像后再点「创建/修复环境」。"
        )

    if kind == "missing" or missing:
        miss = ", ".join(missing or [])
        extra = f"（缺少: {miss}）" if miss else ""
        return (
            f"依赖包未安装完整{extra}。\n"
            "请检查网络/镜像后点击「创建/修复环境」重试；"
            "若反复失败，可尝试「强制重建环境」。"
        )

    return (
        "环境未就绪。请根据上方错误详情处理：\n"
        "· 若含 WinError 1114 / c10.dll / DLL → 修复 VC++ 运行库并重启\n"
        "· 若含网络/超时/HTTP → 检查网络或更换镜像\n"
        "· 若含 No module named → 再点「创建/修复环境」补装依赖"
    )


def refresh_process_path() -> str:
    """
    将系统/用户 PATH（Windows 注册表）合并回当前进程 os.environ['PATH']。

    用户在 PixelFlow 运行期间安装 Git / Python / uv 后，安装器会更新注册表 PATH，
    但本进程仍持有启动时的旧 PATH，导致 shutil.which 与子进程找不到新装工具。
    重新检测 / 创建环境前调用本函数，无需重启应用。
    """
    current = os.environ.get("PATH", "") or ""
    blocks: list[str] = []
    if sys.platform == "win32":
        try:
            import winreg
            for root, subkey in (
                (
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
                ),
                (winreg.HKEY_CURRENT_USER, r"Environment"),
            ):
                try:
                    with winreg.OpenKey(root, subkey) as key:
                        val, _ = winreg.QueryValueEx(key, "Path")
                        if val:
                            blocks.append(str(val))
                except OSError:
                    pass
        except Exception:
            pass
    # 注册表在前（含新装路径），当前进程 PATH 在后（保留运行期注入）
    blocks.append(current)
    merged: list[str] = []
    seen: set[str] = set()
    for block in blocks:
        for part in str(block).split(os.pathsep):
            p = part.strip().strip('"')
            if not p:
                continue
            key = p.lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append(p)
    new_path = os.pathsep.join(merged)
    os.environ["PATH"] = new_path
    return new_path


def ensure_exe_dir_on_path(exe: str | Path | None) -> str:
    """
    将可执行文件所在目录前置到进程 PATH。
    用于经固定路径找到 git/uv 后，让后续 shutil.which / uv 子进程也能解析到同名命令。
    """
    if not exe:
        return os.environ.get("PATH", "") or ""
    try:
        d = str(Path(exe).resolve().parent)
    except Exception:
        try:
            d = str(Path(str(exe)).parent)
        except Exception:
            return os.environ.get("PATH", "") or ""
    if not d or not Path(d).is_dir():
        return os.environ.get("PATH", "") or ""
    cur = os.environ.get("PATH", "") or ""
    parts = [p for p in cur.split(os.pathsep) if p.strip()]
    if any(p.lower() == d.lower() for p in parts):
        return cur
    new_path = d + os.pathsep + cur
    os.environ["PATH"] = new_path
    return new_path


def _run(
    args: list[str],
    *,
    timeout: int | None = 120,
    cwd: str | Path | None = None,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    full_env = os.environ.copy()
    if env:
        # 调用方 env 覆盖同名键；但 PATH 若调用方未提供，保留已刷新的进程 PATH
        full_env.update(env)
    # 避免 uv/python 输出被系统代码页/颜色码搞乱
    full_env.setdefault("PYTHONUTF8", "1")
    full_env.setdefault("PYTHONIOENCODING", "utf-8")
    full_env.setdefault("PYTHONWARNINGS", "ignore")
    full_env.setdefault("NO_COLOR", "1")
    full_env.setdefault("TERM", "dumb")
    full_env.setdefault("CLICOLOR", "0")
    full_env.setdefault("CLICOLOR_FORCE", "0")
    # 减少用户 site-packages / conda hook 干扰隔离环境探测
    full_env.setdefault("PYTHONNOUSERSITE", "1")
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
        env=full_env,
        creationflags=creationflags,
    )


def _rmtree_retry(path: Path, attempts: int = 5, delay: float = 0.8) -> bool:
    """
    稳健删除目录：Windows 下 venv 文件常被杀软实时扫描 / 残留子进程 /
    资源管理器短暂占用，shutil.rmtree(ignore_errors=True) 会静默留下残目录，
    导致后续 `uv venv` 报 "A directory already exists"。
    这里多次重试并清除只读属性；全部失败返回 False（不抛异常）。
    """
    def _on_error(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass

    kwargs: dict = {}
    if sys.version_info >= (3, 12):
        kwargs["onexc"] = _on_error
    else:
        kwargs["onerror"] = _on_error

    for i in range(max(1, attempts)):
        try:
            shutil.rmtree(path, **kwargs)
        except Exception:
            pass
        if not path.exists():
            return True
        if i < attempts - 1:
            time.sleep(delay)
    return not path.exists()


def _normalize_check_names(packages: list[str] | None) -> list[str]:
    """把注册表/安装规格统一成用于检测的逻辑名。"""
    out: list[str] = []
    seen: set[str] = set()
    for pkg in packages or []:
        raw = (pkg or "").strip()
        if not raw:
            continue
        if raw.startswith("git+") or "github.com" in raw or raw.endswith(".git"):
            # git+https://.../BEN2.git → ben2
            name = "ben2" if "ben2" in raw.lower() else Path(raw.rstrip("/")).stem
            name = name.replace(".git", "")
        else:
            name = raw.split("[")[0].split("==")[0].split(">=")[0].split("<=")[0]
            name = name.split("@")[0].split(";")[0].strip()
        key = name.lower()
        if key in ("pillow", "pil"):
            name = "pillow"
            key = "pillow"
        if not name or key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def _import_name_for(pkg: str) -> str:
    import_map = {
        "pillow": "PIL",
        "pil": "PIL",
        "opencv-python": "cv2",
        "opencv-python-headless": "cv2",
        "cv2": "cv2",
        "pytorch": "torch",
        "ben2": "ben2",
        "huggingface_hub": "huggingface_hub",
        "huggingface-hub": "huggingface_hub",
        "safetensors": "safetensors",
        "modelscope": "modelscope",
        "torchvision": "torchvision",
        "torch": "torch",
        "einops": "einops",
        "timm": "timm",
        "numpy": "numpy",
        "kornia": "kornia",
        "transformers": "transformers",
    }
    return import_map.get(pkg.lower(), pkg.replace("-", "_"))


# 进程内探测缓存，避免 UI 反复起子进程
_python_probe_cache: dict[str, PythonInfo | None] = {}
_uv_probe_cache: dict[str, UvInfo | None] = {}
_vc_redist_cache: VcRedistInfo | None = None


def _probe_python(exe: str | Path, *, use_cache: bool = True) -> PythonInfo | None:
    exe = str(exe)
    if not exe:
        return None
    try:
        key = str(Path(exe).resolve()).lower()
    except Exception:
        key = exe.lower()
    if use_cache and key in _python_probe_cache:
        return _python_probe_cache[key]
    if not Path(exe).exists():
        _python_probe_cache[key] = None
        return None
    code = (
        "import sys,struct;"
        "print(sys.version.split()[0]);"
        "print(sys.version_info.major);"
        "print(sys.version_info.minor);"
        "print(struct.calcsize('P')*8)"
    )
    info = None
    try:
        r = _run([exe, "-c", code], timeout=8)
        if r.returncode == 0:
            lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
            if len(lines) >= 4:
                major, minor, bits = int(lines[1]), int(lines[2]), int(lines[3])
                info = PythonInfo(
                    path=str(Path(exe).resolve()),
                    version=lines[0],
                    major=major,
                    minor=minor,
                    executable_ok=True,
                    is_64bit=bits >= 64,
                )
    except Exception:
        info = None
    _python_probe_cache[key] = info
    return info


def _probe_uv(exe: str | Path, *, use_cache: bool = True) -> UvInfo | None:
    exe = str(exe)
    if not exe:
        return None
    try:
        key = str(Path(exe).resolve()).lower()
    except Exception:
        key = exe.lower()
    if use_cache and key in _uv_probe_cache:
        return _uv_probe_cache[key]
    if not Path(exe).exists():
        _uv_probe_cache[key] = None
        return None
    info = None
    try:
        r = _run([exe, "--version"], timeout=8)
        if r.returncode == 0:
            ver = (r.stdout or r.stderr or "").strip()
            info = UvInfo(path=str(Path(exe).resolve()), version=ver, found=True)
    except Exception:
        info = None
    _uv_probe_cache[key] = info
    return info


_git_cache: GitInfo | None = None
_git_probe_cache: dict[str, GitInfo | None] = {}


def _normalize_github_proxy(proxy: str) -> str:
    """规范化代理前缀：去空白；非空时保证以 / 结尾。"""
    p = (proxy or "").strip()
    if not p:
        return ""
    if not p.endswith("/"):
        p += "/"
    return p


def rewrite_github_url(url: str, proxy: str = "") -> str:
    """
    将 GitHub URL 改写为经代理访问的形式（前缀模式）。

    - 空 proxy：原样返回
    - ghfast / ghproxy 等：``{proxy}https://github.com/...``
    - gitclone：``https://gitclone.com/github.com/...``（特判）
    - 已带当前代理前缀：不重复叠加
    """
    raw = (url or "").strip()
    if not raw:
        return raw
    proxy = _normalize_github_proxy(proxy)
    if not proxy:
        return raw

    # 拆分 pip 的 git+ 前缀
    git_prefix = ""
    body = raw
    if body.lower().startswith("git+"):
        git_prefix = body[:4]  # git+
        body = body[4:]

    lower = body.lower()
    # 已是经代理改写过的地址
    if lower.startswith(proxy.lower()):
        return raw
    # 已含常见代理前缀（防止重复套娃）
    for known in (
        "https://ghfast.top/",
        "http://ghfast.top/",
        "https://mirror.ghproxy.com/",
        "https://ghproxy.com/",
        "https://ghproxy.net/",
        "https://gitclone.com/",
    ):
        if lower.startswith(known):
            return raw

    # gitclone：https://gitclone.com/github.com/user/repo.git
    if "gitclone.com" in proxy.lower():
        m = re.match(
            r"^(https?://)(github\.com/)(.+)$",
            body,
            flags=re.IGNORECASE,
        )
        if not m:
            return raw
        rewritten = f"https://gitclone.com/github.com/{m.group(3)}"
        return f"{git_prefix}{rewritten}"

    # 通用前缀：proxy + 完整 https URL
    if re.match(r"^https?://(www\.)?github\.com/", body, flags=re.IGNORECASE):
        # 统一成 https://github.com/...
        body_norm = re.sub(
            r"^https?://(www\.)?github\.com/",
            "https://github.com/",
            body,
            count=1,
            flags=re.IGNORECASE,
        )
        return f"{git_prefix}{proxy}{body_norm}"

    return raw


def rewrite_git_package_specs(packages: list[str], proxy: str = "") -> list[str]:
    """对依赖规格中的 GitHub git+/https 地址应用代理改写。"""
    proxy = _normalize_github_proxy(proxy)
    if not proxy:
        return list(packages)
    out: list[str] = []
    for p in packages:
        s = (p or "").strip()
        if not s:
            continue
        if s.lower().startswith("git+") or "github.com" in s.lower():
            out.append(rewrite_github_url(s, proxy))
        else:
            out.append(s)
    return out


def packages_need_git(packages: list[str] | None) -> bool:
    """依赖列表是否包含需 git 客户端克隆的规格。"""
    for p in packages or []:
        s = (p or "").strip().lower()
        if s.startswith("git+") or s.endswith(".git"):
            return True
    return False


def github_proxy_env(proxy: str = "") -> dict[str, str]:
    """
    为子进程构造临时 git insteadOf 环境变量，不修改用户全局 ~/.gitconfig。
    使 git clone https://github.com/... 自动走代理前缀。
    """
    proxy = _normalize_github_proxy(proxy)
    if not proxy:
        return {}
    # gitclone 已在 URL 层特判，insteadOf 用通用前缀即可覆盖直连
    if "gitclone.com" in proxy.lower():
        # url.https://gitclone.com/github.com/.insteadOf = https://github.com/
        key = "url.https://gitclone.com/github.com/.insteadOf"
        val = "https://github.com/"
    else:
        # url.https://ghfast.top/https://github.com/.insteadOf = https://github.com/
        key = f"url.{proxy}https://github.com/.insteadOf"
        val = "https://github.com/"
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": key,
        "GIT_CONFIG_VALUE_0": val,
    }


def _probe_git(exe: str | Path, *, use_cache: bool = True) -> GitInfo | None:
    exe = str(exe or "").strip()
    if not exe:
        return None
    try:
        key = str(Path(exe).resolve()).lower()
    except Exception:
        key = exe.lower()
    if use_cache and key in _git_probe_cache:
        return _git_probe_cache[key]
    # which 结果可能无完整路径存在性；仍尝试 --version
    info = None
    try:
        r = _run([exe, "--version"], timeout=8)
        if r.returncode == 0:
            ver = (r.stdout or r.stderr or "").strip()
            # "git version 2.45.1.windows.1" → 保留整行，便于诊断
            try:
                resolved = str(Path(exe).resolve()) if Path(exe).exists() else exe
            except Exception:
                resolved = exe
            info = GitInfo(path=resolved, version=ver, found=True)
    except Exception:
        info = None
    _git_probe_cache[key] = info
    return info


def detect_git(*, force: bool = False) -> GitInfo:
    """
    检测本机 git 客户端。
    force=True 时先刷新进程 PATH（拾取刚安装的 Git），并跳过 probe 缓存。
    找到后会把 git 目录注入 PATH，供后续 uv/git 子进程使用。
    """
    global _git_cache
    if not force and _git_cache is not None:
        return _git_cache

    if force:
        refresh_process_path()
        _git_probe_cache.clear()

    candidates: list[str] = []
    which = shutil.which("git")
    if which:
        candidates.append(which)
    # Windows 上 where.exe 有时比 shutil.which 更能反映刷新后的 PATH
    if sys.platform == "win32":
        try:
            r = _run(
                ["where.exe", "git"],
                timeout=5,
            )
            if r.returncode == 0:
                for line in (r.stdout or "").splitlines():
                    p = line.strip().strip('"')
                    if p and p.lower().endswith("git.exe") and p not in candidates:
                        candidates.append(p)
        except Exception:
            pass

    if sys.platform == "win32":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        user = os.environ.get("USERPROFILE", "")
        for base in (pf, pf86, local, user):
            if not base:
                continue
            for rel in (
                r"Git\cmd\git.exe",
                r"Git\bin\git.exe",
                r"Programs\Git\cmd\git.exe",
                r"Programs\Git\bin\git.exe",
                r"AppData\Local\Programs\Git\cmd\git.exe",
                r"AppData\Local\Programs\Git\bin\git.exe",
            ):
                p = str(Path(base) / rel)
                if p not in candidates:
                    candidates.append(p)

    for c in candidates:
        try:
            cp = Path(c)
            # 绝对路径且文件不存在则跳过；命令名（git）仍交给 _probe_git
            if cp.is_absolute() and not cp.is_file():
                continue
        except Exception:
            pass
        info = _probe_git(c, use_cache=not force)
        if info and info.found:
            # 注入 PATH，保证 uv pip install git+https 能找到 git 命令
            ensure_exe_dir_on_path(info.path)
            try:
                parent = Path(info.path).resolve().parent
                for sib in (
                    parent.parent / "bin" / "git.exe",
                    parent.parent / "cmd" / "git.exe",
                ):
                    if sib.is_file():
                        ensure_exe_dir_on_path(sib)
            except Exception:
                pass
            _git_cache = info
            return info

    info = GitInfo(found=False)
    _git_cache = info
    return info


def _settings_path() -> Path:
    p = Path(config.RUNTIME_DIR)
    p.mkdir(parents=True, exist_ok=True)
    return p / "runtime_settings.json"


class RuntimeManager:
    """
    管理：
    - 系统可用的基础 Python（用于 uv venv）
    - uv 可执行文件（PATH / 项目内 runtime/uv）
    - 每个模型独立的 venv：runtime/envs/<model_id>/.venv
    """

    # 模型环境最低 Python（与注册表可再收紧）
    DEFAULT_PY_MIN = (3, 10)
    DEFAULT_PY_MAX = (3, 12)  # 含 3.12；3.13 部分 AI 轮子可能未就绪

    def __init__(self):
        self._lock = threading.Lock()
        self._settings = self._load_settings()
        self._busy_lock = threading.Lock()
        self._busy = False
        # UI 友好缓存
        self._py_list_cache: list[PythonInfo] | None = None
        self._uv_cache: UvInfo | None = None
        self._git_cache: GitInfo | None = None
        self._env_status_cache: dict[str, ModelEnvStatus] = {}
        # 最近一次 ensure_model_env 的 torch 安装方案（写入 env_meta）
        self._last_torch_plan: TorchInstallPlan | None = None

    # ── 配置 ──
    def _load_settings(self) -> dict:
        path = _settings_path()
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {
            "python_path": "",
            "uv_path": "",
            "pip_index_url": DEFAULT_PIP_INDEX_URL,
            "github_proxy": DEFAULT_GITHUB_PROXY,
        }

    def save_settings(self):
        path = _settings_path()
        path.write_text(
            json.dumps(self._settings, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_saved_python_path(self) -> str:
        return str(self._settings.get("python_path", "") or "")

    def set_python_path(self, path: str):
        self._settings["python_path"] = path.strip()
        self.save_settings()
        self.invalidate_caches(pythons=True)

    def get_saved_uv_path(self) -> str:
        return str(self._settings.get("uv_path", "") or "")

    def set_uv_path(self, path: str):
        self._settings["uv_path"] = path.strip()
        self.save_settings()
        self.invalidate_caches(uv=True)

    def get_pip_index_url(self) -> str:
        """
        返回安装依赖时使用的 PyPI 索引 URL。
        空字符串表示不附加 -i（走 uv/pip 默认源）。
        未配置时默认清华源。
        """
        if "pip_index_url" not in self._settings:
            return DEFAULT_PIP_INDEX_URL
        return str(self._settings.get("pip_index_url", "") or "").strip()

    def set_pip_index_url(self, url: str):
        self._settings["pip_index_url"] = (url or "").strip()
        self.save_settings()

    def pip_index_args(self) -> list[str]:
        """供 `uv pip install` 使用的索引参数，如 ['-i', 'https://…/simple']。"""
        url = self.get_pip_index_url()
        if not url:
            return []
        return ["-i", url]

    def get_github_proxy(self) -> str:
        """
        返回 GitHub 访问代理前缀。
        空字符串表示直连 GitHub；键缺失时默认 ghfast。
        """
        if "github_proxy" not in self._settings:
            return DEFAULT_GITHUB_PROXY
        return _normalize_github_proxy(
            str(self._settings.get("github_proxy", "") or "")
        )

    def set_github_proxy(self, proxy: str):
        # 持久化时保留用户输入形态；读取时再规范化
        self._settings["github_proxy"] = (proxy or "").strip()
        self.save_settings()

    def rewrite_github_url(self, url: str) -> str:
        """按当前设置改写 GitHub URL。"""
        return rewrite_github_url(url, self.get_github_proxy())

    def rewrite_git_package_specs(self, packages: list[str]) -> list[str]:
        """按当前代理改写依赖中的 GitHub 地址。"""
        return rewrite_git_package_specs(packages, self.get_github_proxy())

    def git_proxy_env(self) -> dict[str, str]:
        """安装 git 依赖时注入的临时 insteadOf 环境变量。"""
        return github_proxy_env(self.get_github_proxy())

    def resolve_git(self, *, force: bool = False) -> GitInfo:
        """
        检测本机 Git（带实例缓存）。
        force=True：刷新系统 PATH + 清空缓存后重探，供「重新检测」与创建环境使用。
        """
        global _git_cache
        if force:
            self._git_cache = None
            _git_cache = None
            _git_probe_cache.clear()
        if self._git_cache is not None and not force:
            # 缓存命中时仍确保 git 目录在 PATH（供后续子进程）
            if self._git_cache.found and self._git_cache.path:
                ensure_exe_dir_on_path(self._git_cache.path)
            return self._git_cache
        info = detect_git(force=force)
        self._git_cache = info
        return info

    def invalidate_caches(
        self,
        *,
        pythons: bool = False,
        uv: bool = False,
        env: str | bool = False,
        vc: bool = False,
        git: bool = False,
        all_: bool = False,
    ):
        """清除探测缓存。env=True 清全部模型；env='ben2' 清指定模型。"""
        global _vc_redist_cache, _git_cache, _git_probe_cache, _uv_probe_cache, _python_probe_cache
        if all_ or pythons:
            self._py_list_cache = None
            _python_probe_cache.clear()
        if all_ or uv:
            self._uv_cache = None
            _uv_probe_cache.clear()
        if all_ or git:
            self._git_cache = None
            _git_cache = None
            _git_probe_cache.clear()
        if all_ or vc:
            _vc_redist_cache = None
        if all_ or env is True:
            self._env_status_cache.clear()
        elif isinstance(env, str) and env:
            self._env_status_cache.pop(env, None)
            # 也清带 packages 的复合 key
            for k in list(self._env_status_cache.keys()):
                if k.startswith(env + "|"):
                    self._env_status_cache.pop(k, None)

    def get_vc_redist(self, *, force: bool = False) -> VcRedistInfo:
        """Windows VC++ 运行库检测（带缓存）。"""
        return detect_vc_redist(force=force)

    @property
    def is_busy(self) -> bool:
        return self._busy

    # ── 路径 ──
    def runtime_root(self) -> Path:
        root = Path(config.RUNTIME_DIR)
        root.mkdir(parents=True, exist_ok=True)
        return root

    def envs_root(self) -> Path:
        root = Path(config.RUNTIME_ENVS_DIR)
        root.mkdir(parents=True, exist_ok=True)
        return root

    def model_env_dir(self, model_id: str) -> Path:
        return self.envs_root() / model_id

    def model_venv_dir(self, model_id: str) -> Path:
        return self.model_env_dir(model_id) / ".venv"

    def model_python(self, model_id: str) -> Path | None:
        venv = self.model_venv_dir(model_id)
        if sys.platform == "win32":
            py = venv / "Scripts" / "python.exe"
        else:
            py = venv / "bin" / "python"
        return py if py.is_file() else None

    def bundled_uv_path(self) -> Path:
        name = "uv.exe" if sys.platform == "win32" else "uv"
        return Path(config.RUNTIME_UV_DIR) / name

    # ── Python 发现 ──
    def discover_pythons(self, *, force: bool = False, deep: bool = False) -> list[PythonInfo]:
        """
        扫描本机可用的 Python 解释器（去重，带缓存）。
        deep=False：快速路径（PATH + py -0p + 少量固定目录），供 UI 使用。
        deep=True：额外浅扫常见安装目录（仍避免全盘 rglob）。
        """
        if not force and self._py_list_cache is not None:
            return list(self._py_list_cache)

        candidates: list[str] = []
        saved = self.get_saved_python_path()
        if saved:
            candidates.append(saved)

        # 当前进程（开发模式有用；frozen 时是 exe 本身，通常不是 CPython）
        if not getattr(sys, "frozen", False):
            candidates.append(sys.executable)

        for name in ("python", "python3"):
            p = shutil.which(name)
            if p:
                candidates.append(p)

        if sys.platform == "win32":
            py_launcher = shutil.which("py")
            if py_launcher:
                try:
                    r = _run([py_launcher, "-0p"], timeout=8)
                    for line in (r.stdout or "").splitlines():
                        m = re.search(r"([A-Za-z]:\\[^\s]+\.exe)", line)
                        if m:
                            candidates.append(m.group(1))
                        else:
                            parts = line.strip().split()
                            if parts and parts[-1].lower().endswith(".exe"):
                                candidates.append(parts[-1])
                except Exception:
                    pass

            # 固定浅路径（不 rglob，避免扫到巨大 Anaconda 树卡死 UI）
            local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python"
            if local.is_dir():
                for child in local.iterdir():
                    pe = child / "python.exe"
                    if pe.is_file():
                        candidates.append(str(pe))
            for pe in (
                Path(r"C:\Python312\python.exe"),
                Path(r"C:\Python311\python.exe"),
                Path(r"C:\Python310\python.exe"),
                Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Python312" / "python.exe",
            ):
                if pe.is_file():
                    candidates.append(str(pe))

            if deep:
                # 可选：只扫一层 conda/envs 名称，不递归整棵树
                for root in (
                    Path(r"D:\anaconda3"),
                    Path(r"C:\anaconda3"),
                    Path(os.environ.get("USERPROFILE", "")) / "anaconda3",
                    Path(os.environ.get("USERPROFILE", "")) / "miniconda3",
                ):
                    pe = root / "python.exe"
                    if pe.is_file():
                        candidates.append(str(pe))
                    envs = root / "envs"
                    if envs.is_dir():
                        try:
                            for env_dir in envs.iterdir():
                                epy = env_dir / "python.exe"
                                if epy.is_file():
                                    candidates.append(str(epy))
                        except Exception:
                            pass

        seen: set[str] = set()
        result: list[PythonInfo] = []
        for c in candidates:
            try:
                key = str(Path(c).resolve()).lower()
            except Exception:
                key = c.lower()
            if key in seen:
                continue
            seen.add(key)
            # 跳过本项目 runtime venv
            if "\\runtime\\envs\\" in key or "/runtime/envs/" in key:
                continue
            info = _probe_python(c)
            if info and info.executable_ok:
                if "runtime" in Path(info.path).parts and "envs" in Path(info.path).parts:
                    continue
                result.append(info)

        result.sort(key=lambda x: (x.major, x.minor), reverse=True)
        self._py_list_cache = list(result)
        return list(result)

    def resolve_base_python(
        self,
        min_ver: tuple[int, int] | None = None,
        max_ver: tuple[int, int] | None = None,
    ) -> PythonInfo | None:
        """选取用于创建 uv 环境的基础 Python"""
        min_ver = min_ver or self.DEFAULT_PY_MIN
        max_ver = max_ver or self.DEFAULT_PY_MAX

        saved = self.get_saved_python_path()
        if saved:
            info = _probe_python(saved)
            if info and self._version_ok(info, min_ver, max_ver):
                return info

        for info in self.discover_pythons():
            if self._version_ok(info, min_ver, max_ver):
                return info
        # 不再放宽到 3.13+：PyTorch 等 AI 轮子常无对应 ABI（如 cp314），
        # 会导致 CUDA 安装失败后被误判为「GPU 不匹配」。更高版本由 uv 托管下载目标小版本。
        return None

    @staticmethod
    def _version_ok(
        info: PythonInfo,
        min_ver: tuple[int, int],
        max_ver: tuple[int, int],
    ) -> bool:
        if not info.is_64bit:
            return False
        v = (info.major, info.minor)
        return min_ver <= v <= max_ver

    # ── uv 发现 / 安装 ──
    def resolve_uv(self, *, force: bool = False) -> UvInfo:
        """
        解析 uv 可执行文件。
        force=True：刷新 PATH、清空 probe 缓存后重探（手动安装/拷贝后无需重启）。
        """
        if force:
            self._uv_cache = None
            _uv_probe_cache.clear()
            refresh_process_path()
        if not force and self._uv_cache is not None:
            if self._uv_cache.found and self._uv_cache.path:
                ensure_exe_dir_on_path(self._uv_cache.path)
            return self._uv_cache

        saved = self.get_saved_uv_path()
        if saved:
            info = _probe_uv(saved, use_cache=not force)
            if info:
                info.source = "saved"
                ensure_exe_dir_on_path(info.path)
                self._uv_cache = info
                return info

        bundled = self.bundled_uv_path()
        info = _probe_uv(bundled, use_cache=not force)
        if info:
            info.source = "bundled"
            ensure_exe_dir_on_path(info.path)
            self._uv_cache = info
            return info

        which = shutil.which("uv")
        if which:
            info = _probe_uv(which, use_cache=not force)
            if info:
                info.source = "path"
                ensure_exe_dir_on_path(info.path)
                self._uv_cache = info
                return info

        # Windows：where + 常见用户目录兜底
        if sys.platform == "win32":
            try:
                r = _run(["where.exe", "uv"], timeout=5)
                if r.returncode == 0:
                    for line in (r.stdout or "").splitlines():
                        p = line.strip().strip('"')
                        if not p:
                            continue
                        info = _probe_uv(p, use_cache=not force)
                        if info:
                            info.source = "path"
                            ensure_exe_dir_on_path(info.path)
                            self._uv_cache = info
                            return info
            except Exception:
                pass

        info = UvInfo(found=False, source="not_found")
        self._uv_cache = info
        return info

    def install_uv(self, progress: ProgressCb | None = None) -> UvInfo:
        """
        将 uv 安装到 runtime/uv/（不依赖用户全局环境）。
        Windows: 从 GitHub releases 下载 uv-x86_64-pc-windows-msvc.zip
        其它: 使用官方 install 脚本到 RUNTIME_UV_DIR
        """
        if progress:
            progress("uv", 5, "准备安装 uv…")

        dest_dir = Path(config.RUNTIME_UV_DIR)
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = self.bundled_uv_path()

        if sys.platform == "win32":
            self._install_uv_windows(dest_dir, target, progress)
        else:
            self._install_uv_unix(dest_dir, progress)

        info = _probe_uv(target if target.exists() else shutil.which("uv") or "")
        if not info or not info.found:
            # unix 脚本可能装到 ~/.local/bin
            which = shutil.which("uv")
            if which:
                # 复制到项目目录便于便携
                try:
                    shutil.copy2(which, target)
                    info = _probe_uv(target)
                except Exception:
                    info = _probe_uv(which)
        if not info or not info.found:
            raise RuntimeError(
                "uv 安装失败。请手动从 https://github.com/astral-sh/uv/releases 下载，"
                f"将 uv 放到: {dest_dir}"
            )
        info.source = "bundled"
        self.set_uv_path(info.path)
        if progress:
            progress("uv", 100, f"uv 已就绪: {info.version}")
        return info

    def _install_uv_windows(self, dest_dir: Path, target: Path, progress: ProgressCb | None):
        import tempfile
        import zipfile

        # 固定使用较新的稳定版；优先走用户配置的 GitHub 代理，再回退直连与常见镜像
        official = (
            "https://github.com/astral-sh/uv/releases/latest/download/"
            "uv-x86_64-pc-windows-msvc.zip"
        )
        urls: list[str] = []
        proxied = self.rewrite_github_url(official)
        if proxied and proxied != official:
            urls.append(proxied)
        urls.append(official)
        # 额外兜底（与默认代理不同时才加）
        fallback = rewrite_github_url(official, DEFAULT_GITHUB_PROXY)
        if fallback not in urls:
            urls.append(fallback)

        last_err = None
        for url in urls:
            try:
                if progress:
                    progress("uv", 15, f"下载 uv…\n{url}")
                with tempfile.TemporaryDirectory() as td:
                    zpath = Path(td) / "uv.zip"
                    urllib.request.urlretrieve(url, zpath)
                    if progress:
                        progress("uv", 60, "解压 uv…")
                    with zipfile.ZipFile(zpath, "r") as zf:
                        zf.extractall(td)
                    # 查找 uv.exe
                    found = list(Path(td).rglob("uv.exe"))
                    if not found:
                        raise RuntimeError("压缩包中未找到 uv.exe")
                    shutil.copy2(found[0], target)
                    # 可选 uvw
                    for extra in Path(td).rglob("uvw.exe"):
                        shutil.copy2(extra, dest_dir / "uvw.exe")
                return
            except Exception as e:
                last_err = e
                continue

        # 回退：powershell 官方安装脚本，再复制
        if progress:
            progress("uv", 30, "尝试官方安装脚本…")
        try:
            ps = (
                "irm https://astral.sh/uv/install.ps1 | iex"
            )
            r = _run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                timeout=300,
            )
            if r.returncode != 0:
                raise RuntimeError(r.stderr or r.stdout or "install.ps1 失败")
            # 常见位置
            candidates = [
                Path(os.environ.get("USERPROFILE", "")) / ".local" / "bin" / "uv.exe",
                Path(os.environ.get("USERPROFILE", "")) / ".cargo" / "bin" / "uv.exe",
            ]
            which = shutil.which("uv")
            if which:
                candidates.insert(0, Path(which))
            for c in candidates:
                if c.is_file():
                    shutil.copy2(c, target)
                    return
        except Exception as e:
            last_err = e

        raise RuntimeError(f"无法安装 uv: {last_err}")

    def _install_uv_unix(self, dest_dir: Path, progress: ProgressCb | None):
        if progress:
            progress("uv", 20, "通过官方脚本安装 uv…")
        # UV_INSTALL_DIR 指定安装目录
        env = {"UV_INSTALL_DIR": str(dest_dir)}
        r = _run(
            ["sh", "-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"],
            timeout=300,
            env=env,
        )
        if r.returncode != 0:
            raise RuntimeError(r.stderr or r.stdout or "uv install.sh 失败")

    # ── 模型环境 ──
    def resolve_required_packages(self, model_id: str, packages: list[str] | None = None) -> list[str]:
        """优先用调用方列表；否则从模型注册表 env_check_packages / env_packages 推导。"""
        if packages:
            return _normalize_check_names(packages)
        try:
            from core.matting.model_registry import get_model_info
            meta = get_model_info(model_id)
        except Exception:
            meta = None
        if meta is None:
            return []
        if meta.env_check_packages:
            return _normalize_check_names(list(meta.env_check_packages))
        return _normalize_check_names(list(meta.env_packages))

    def check_packages_in_python(
        self,
        python_exe: str | Path,
        packages: list[str],
    ) -> tuple[list[str], list[str], str]:
        """
        在指定 python 中检测依赖。
        返回 (missing, present, error_message)。
        使用 JSON 标记行，避免警告/ANSI 污染解析。
        """
        names = _normalize_check_names(packages)
        if not names:
            return [], [], ""
        mods = [_import_name_for(n) for n in names]
        # 子进程只打印一行 PF_DEPS_JSON={...}
        code = r"""
import json, sys
mods = %s
names = %s
miss, ok, errs = [], [], {}
for m, n in zip(mods, names):
    try:
        __import__(m)
        ok.append(n)
    except Exception as e:
        miss.append(n)
        errs[n] = type(e).__name__ + ': ' + str(e)[:160]
sys.stdout.write('PF_DEPS_JSON=' + json.dumps(
    {'missing': miss, 'ok': ok, 'errors': errs}, ensure_ascii=False
) + '\n')
sys.stdout.flush()
""" % (repr(mods), repr(names))
        try:
            # torch 冷启动 import 较慢；超时过短会在批处理/杀毒扫描时误报缺依赖
            r = _run([str(python_exe), "-c", code], timeout=120)
        except subprocess.TimeoutExpired:
            return [], [], "探测失败: timeout（依赖探测超时，环境未必损坏）"
        except Exception as e:
            return [], [], f"探测失败: {e}"

        stdout = _strip_ansi(r.stdout or "")
        stderr = _strip_ansi(r.stderr or "")
        payload = None
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("PF_DEPS_JSON="):
                raw = line[len("PF_DEPS_JSON="):]
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    payload = None
                break

        if payload is None:
            # 解析失败时不要把全部包名标成 missing（会误导成「环境未配置」）
            hint = (stderr or stdout or f"exit={r.returncode}")[-500:]
            hint = _strip_ansi(hint).replace("\x1b", "")
            return [], [], f"依赖探测输出异常: {hint}"

        missing = [str(x) for x in (payload.get("missing") or []) if str(x).strip()]
        # 过滤明显不是包名的噪声
        missing = [
            m for m in missing
            if m.isidentifier() or all(c.isalnum() or c in "-_." for c in m)
        ]
        present = [str(x) for x in (payload.get("ok") or []) if str(x).strip()]
        err = ""
        if missing and payload.get("errors"):
            parts = [f"{k}: {v}" for k, v in list(payload["errors"].items())[:4]]
            err = "; ".join(parts)
        return missing, present, err

    def get_model_env_status(
        self,
        model_id: str,
        required_packages: list[str] | None = None,
        *,
        quick: bool = False,
        force: bool = False,
    ) -> ModelEnvStatus:
        """
        quick=True：只判断 venv / meta（不 import），供 UI 轻量展示。
        quick=False：子进程真实 import 校验依赖（带缓存）。
        """
        env_dir = self.model_env_dir(model_id)
        pkgs = self.resolve_required_packages(model_id, required_packages)
        cache_key = f"{model_id}|{'q' if quick else 'f'}|{'+'.join(pkgs)}"
        if not force and cache_key in self._env_status_cache:
            return self._env_status_cache[cache_key]

        py = self.model_python(model_id)
        st = ModelEnvStatus(model_id=model_id, env_dir=str(env_dir))
        if py is None:
            st.detail = "尚未创建隔离环境"
            self._env_status_cache[cache_key] = st
            return st

        st.python_path = str(py)
        meta_path = env_dir / "env_meta.json"
        meta_data: dict = {}
        if meta_path.is_file():
            try:
                meta_data = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta_data = {}

        if quick:
            st.ready = False
            st.detail = "已创建（未校验依赖）"
            st.python_version = str(meta_data.get("python_version") or "")
            st.torch_version = str(meta_data.get("torch_version") or "")
            st.torch_build = str(meta_data.get("torch_build") or "")
            st.torch_cuda_available = bool(meta_data.get("torch_cuda_available"))
            st.torch_cuda_version = str(meta_data.get("torch_cuda_version") or "")
            # quick 只信任 meta.ready==True，且缺失列表为空
            if meta_data.get("ready") is True and not meta_data.get("missing"):
                st.ready = True
                st.detail = "环境就绪（缓存）"
            elif meta_data.get("missing"):
                miss = [str(x) for x in meta_data.get("missing") or []]
                # 过滤历史脏数据（ANSI 等）
                miss = [m for m in miss if m.isidentifier() or all(c.isalnum() or c in "-_." for c in m)]
                if miss:
                    st.missing_packages = miss
                    st.detail = f"缺少: {', '.join(miss)}"
            self._env_status_cache[cache_key] = st
            return st

        info = _probe_python(py)
        if not info:
            st.detail = "环境内 Python 无法运行"
            self._env_status_cache[cache_key] = st
            return st
        st.python_version = info.version

        if not pkgs:
            st.ready = True
            st.detail = "环境就绪"
            self._fill_torch_status(st, py)
            self._env_status_cache[cache_key] = st
            return st

        missing, present, err = self.check_packages_in_python(py, pkgs)
        st.missing_packages = missing
        # 瞬时探测失败（超时/输出异常）不写死 ready=False 进长期缓存，
        # 避免批处理下一张图继续吃到错误结论
        probe_transient = bool(err) and (
            "探测失败" in err or "依赖探测输出异常" in err or "timeout" in err.lower()
        )
        if not missing and not probe_transient:
            st.ready = True
            st.fail_kind = ""
            st.detail = f"环境就绪（{len(present)} 项依赖）"
            self._fill_torch_status(st, py)
            # 就绪详情附带 torch 构建，便于配置页一眼区分 CPU/CUDA
            if st.torch_version:
                build = st.torch_build or "?"
                cu = "CUDA可用" if st.torch_cuda_available else "CUDA不可用"
                st.detail = (
                    f"环境就绪（{len(present)} 项依赖）  ·  "
                    f"torch {st.torch_version} [{build}]  ·  {cu}"
                )
        elif not missing and probe_transient:
            st.ready = False
            st.fail_kind = "probe"
            st.detail = err
            # 不缓存瞬时失败
            return st
        else:
            st.ready = False
            kind = classify_import_errors(err or "")
            st.fail_kind = kind
            # DLL 加载失败时包往往已装上，避免只显示「缺少 xxx」造成误导
            if kind == "dll":
                vc = detect_vc_redist()
                head = "依赖已安装但无法加载（DLL/运行库问题）"
                if missing:
                    head += f"；受影响: {', '.join(missing)}"
                st.detail = head
                if err:
                    st.detail += f"  [{err[:220]}]"
                if vc.applicable and not vc.ok:
                    st.detail += f"\n{vc.detail}"
                st.detail += "\n" + format_env_failure_hint(
                    fail_kind="dll", detail=err or st.detail, missing=missing, vc=vc
                )
            else:
                st.detail = f"缺少: {', '.join(missing)}" if missing else "依赖未就绪"
                if err:
                    st.detail += f"  [{err[:200]}]"
                st.detail += "\n" + format_env_failure_hint(
                    fail_kind=kind, detail=err or st.detail, missing=missing
                )
            # 即使缺包也尽量带上已装 torch 信息
            if "torch" not in {m.lower() for m in missing}:
                self._fill_torch_status(st, py)
        self._env_status_cache[cache_key] = st
        return st

    def _fill_torch_status(self, st: ModelEnvStatus, python_exe: str | Path) -> None:
        """填充 ModelEnvStatus 的 torch 字段（失败静默）。"""
        try:
            tb = self.probe_torch_build(python_exe)
        except Exception:
            return
        st.torch_version = str(tb.get("version") or "")
        st.torch_cuda_available = bool(tb.get("cuda_available"))
        st.torch_cuda_version = str(tb.get("cuda_version") or "")
        st.torch_build = str(tb.get("build") or "")

    def env_exists(self, model_id: str) -> bool:
        """纯路径判断，零子进程。"""
        return self.model_python(model_id) is not None

    def probe_torch_build(self, python_exe: str | Path) -> dict:
        """
        在指定解释器中探测 torch 版本与 CUDA 可用性。
        返回 dict: version / cuda_available / cuda_version / build /
                   device_capability / arch_list / gpu_arch_ok
        """
        code = r"""
import json, sys
out = {
    'version': '', 'cuda_available': False, 'cuda_version': '',
    'build': 'unknown', 'device_capability': '', 'arch_list': [],
    'gpu_arch_ok': None, 'gpu_name': '',
}
try:
    import torch
    out['version'] = str(getattr(torch, '__version__', '') or '')
    out['cuda_available'] = bool(torch.cuda.is_available())
    cv = getattr(getattr(torch, 'version', None), 'cuda', None)
    out['cuda_version'] = str(cv or '') or ''
    ver = out['version'].lower()
    if '+cu' in ver or out['cuda_version']:
        out['build'] = 'cuda'
    elif '+cpu' in ver:
        out['build'] = 'cpu'
    elif out['cuda_available']:
        out['build'] = 'cuda'
    else:
        out['build'] = 'cpu'
    if out['cuda_available']:
        try:
            out['gpu_name'] = str(torch.cuda.get_device_name(0) or '')
        except Exception:
            pass
        try:
            maj, minr = torch.cuda.get_device_capability(0)
            out['device_capability'] = f'{maj}.{minr}'
        except Exception:
            pass
        try:
            arches = list(torch.cuda.get_arch_list() or [])
            out['arch_list'] = arches
        except Exception:
            arches = []
        # 判断当前 GPU sm_xx 是否在 wheel 编译架构列表中
        cap = out['device_capability']
        if cap and arches:
            try:
                maj_s, min_s = cap.split('.')
                sm = f'sm_{maj_s}{min_s}'
                # 兼容 sm_120 / compute_120 等写法
                flat = ' '.join(arches).lower()
                out['gpu_arch_ok'] = (
                    sm in flat
                    or f'compute_{maj_s}{min_s}' in flat
                    or f'sm{maj_s}{min_s}' in flat.replace('_', '')
                )
            except Exception:
                out['gpu_arch_ok'] = None
except Exception as e:
    out['error'] = type(e).__name__ + ': ' + str(e)[:160]
sys.stdout.write('PF_TORCH_JSON=' + json.dumps(out, ensure_ascii=False) + '\n')
"""
        try:
            r = _run([str(python_exe), "-c", code], timeout=90)
        except Exception as e:
            return {
                "version": "",
                "cuda_available": False,
                "cuda_version": "",
                "build": "unknown",
                "error": str(e),
            }
        payload = None
        # stderr 里可能有 sm 不兼容警告，stdout 才是 JSON
        for line in _strip_ansi(r.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("PF_TORCH_JSON="):
                try:
                    payload = json.loads(line[len("PF_TORCH_JSON="):])
                except json.JSONDecodeError:
                    payload = None
                break
        if not isinstance(payload, dict):
            return {
                "version": "",
                "cuda_available": False,
                "cuda_version": "",
                "build": "unknown",
                "error": "probe_failed",
            }
        return payload

    def _install_torch_packages(
        self,
        *,
        uv_path: str,
        python_exe: str,
        torch_pkgs: list[str],
        plan: TorchInstallPlan,
        cwd: Path,
        progress: ProgressCb | None,
        model_id: str,
        require_gpu_arch: bool = True,
    ) -> bool:
        """
        按 plan 安装 torch 相关包。
        CUDA：--index-url 指向 PyTorch 官方 cuXXX 索引（不用普通 PyPI，避免装成 +cpu）。
        CPU：走应用配置的 PyPI 镜像。
        成功返回 True。
        require_gpu_arch=True 时，若 wheel 不含本机 GPU 架构（如 sm_120）视为失败。
        """
        if not torch_pkgs:
            return True
        if plan.flavor == "cuda" and plan.index_url:
            # 官方 whl 索引已含依赖元数据；不混用 -i PyPI，防止解析到 cpu 轮子
            index_args = ["--index-url", plan.index_url]
            label = f"PyTorch CUDA ({plan.cuda_tag}) ← {plan.index_url}"
        else:
            index_args = self.pip_index_args()
            label = "PyTorch CPU ← " + (self.get_pip_index_url() or "default")

        if progress:
            progress(
                model_id, 34,
                f"安装 {label}: {', '.join(torch_pkgs)} …",
            )
        cmd = [
            uv_path, "pip", "install",
            "--python", python_exe,
            "--upgrade",
            *index_args,
            *torch_pkgs,
        ]
        r = _run(cmd, timeout=None, cwd=cwd)
        if r.returncode != 0:
            err = _strip_ansi((r.stderr or r.stdout or "")[-2000:])
            if progress:
                abi_hint = ""
                err_l = err.lower()
                if "abi tag" in err_l or "cp31" in err_l or "no wheels with a matching python" in err_l:
                    abi_hint = (
                        "（当前 venv 的 Python ABI 无对应 torch 轮子，"
                        "请使用 Python 3.10–3.12 重建环境）"
                    )
                progress(model_id, 40, f"PyTorch 安装失败{abi_hint}: {err[:180]}")
            return False

        # 校验是否真的装上、CUDA 构建是否符合预期
        tb = self.probe_torch_build(python_exe)
        ver = str(tb.get("version") or "")
        build = str(tb.get("build") or "unknown")
        if not ver:
            if progress:
                progress(model_id, 40, "PyTorch 安装后无法 import")
            return False
        if plan.flavor == "cuda" and build != "cuda":
            # 索引装上了但实际是 cpu 轮子 → 视为失败，触发回退
            if progress:
                progress(
                    model_id, 40,
                    f"期望 CUDA 版，实际得到 {ver}（{build}）",
                )
            return False
        # Blackwell 等新架构：wheel 能 import 但无对应 kernel → 仍算失败
        if (
            plan.flavor == "cuda"
            and require_gpu_arch
            and tb.get("gpu_arch_ok") is False
        ):
            cap = tb.get("device_capability") or "?"
            arches = tb.get("arch_list") or []
            if progress:
                progress(
                    model_id, 40,
                    f"PyTorch {ver} 不含本机 GPU 架构 (CC {cap})，"
                    f"wheel 支持: {', '.join(arches[:8]) or '?'}。"
                    "将尝试更高 CUDA 标签…",
                )
            return False
        if progress:
            cu = "CUDA可用" if tb.get("cuda_available") else "CUDA不可用"
            arch_note = ""
            if tb.get("device_capability"):
                ok = tb.get("gpu_arch_ok")
                if ok is True:
                    arch_note = f"  ·  架构匹配 CC{tb['device_capability']}"
                elif ok is False:
                    arch_note = f"  ·  架构不匹配 CC{tb['device_capability']}"
            progress(
                model_id, 46,
                f"PyTorch 已安装: {ver}  ·  构建={build}  ·  {cu}{arch_note}",
            )
        return True

    def _install_torch_with_fallbacks(
        self,
        *,
        uv_path: str,
        python_exe: str,
        torch_pkgs: list[str],
        plan: TorchInstallPlan,
        cwd: Path,
        progress: ProgressCb | None,
        model_id: str,
    ) -> TorchInstallPlan | None:
        """
        按候选 CUDA 标签依次安装；全部失败再装 CPU。
        成功返回实际采用的 plan，失败返回 None。
        """
        if not torch_pkgs:
            return plan

        if plan.flavor != "cuda":
            ok = self._install_torch_packages(
                uv_path=uv_path,
                python_exe=python_exe,
                torch_pkgs=torch_pkgs,
                plan=plan,
                cwd=cwd,
                progress=progress,
                model_id=model_id,
                require_gpu_arch=False,
            )
            return plan if ok else None

        nv = plan.nvidia
        if plan.candidate_tags:
            tags = list(plan.candidate_tags)
        else:
            tags = resolve_torch_cuda_tag_candidates(
                nv.cuda_major if nv else 0,
                nv.cuda_minor if nv else 0,
                compute_major=nv.compute_major if nv else 0,
                compute_minor=nv.compute_minor if nv else 0,
                gpu_name=nv.primary_name if nv else "",
            )
        # 确保主选在最前
        if plan.cuda_tag and plan.cuda_tag not in tags:
            tags.insert(0, plan.cuda_tag)
        elif plan.cuda_tag:
            tags = [plan.cuda_tag] + [t for t in tags if t != plan.cuda_tag]

        for i, tag in enumerate(tags):
            cand = TorchInstallPlan(
                flavor="cuda",
                cuda_tag=tag,
                index_url=f"{PYTORCH_WHL_BASE}/{tag}",
                reason=plan.reason,
                nvidia=nv,
                match=plan.match,
                candidate_tags=list(tags),
                driver_ok=plan.driver_ok,
                driver_hint=plan.driver_hint,
            )
            if progress and i > 0:
                progress(
                    model_id, 32,
                    f"尝试备选 CUDA 标签 {tag}（第 {i + 1}/{len(tags)} 个）…",
                )
            ok = self._install_torch_packages(
                uv_path=uv_path,
                python_exe=python_exe,
                torch_pkgs=torch_pkgs,
                plan=cand,
                cwd=cwd,
                progress=progress,
                model_id=model_id,
                require_gpu_arch=True,
            )
            if ok:
                return cand

        # 全部 CUDA 标签失败 → CPU
        if progress:
            progress(
                model_id, 42,
                "CUDA 版 PyTorch 安装失败（标签轮询均未成功，"
                "常见原因：Python 版本过新无 wheel / 网络 / 架构不匹配），回退 CPU 版…",
            )
        cpu_plan = TorchInstallPlan(
            flavor="cpu",
            reason="CUDA 标签均失败后的回退",
            nvidia=nv,
        )
        ok = self._install_torch_packages(
            uv_path=uv_path,
            python_exe=python_exe,
            torch_pkgs=torch_pkgs,
            plan=cpu_plan,
            cwd=cwd,
            progress=progress,
            model_id=model_id,
            require_gpu_arch=False,
        )
        return cpu_plan if ok else None

    def write_env_meta(
        self,
        model_id: str,
        *,
        base_python: str = "",
        packages: list[str] | None = None,
        status: ModelEnvStatus | None = None,
        torch_plan: TorchInstallPlan | None = None,
    ):
        env_dir = self.model_env_dir(model_id)
        env_dir.mkdir(parents=True, exist_ok=True)
        st = status or self.get_model_env_status(model_id, force=True)
        plan = torch_plan or getattr(self, "_last_torch_plan", None)
        meta = {
            "model_id": model_id,
            "base_python": base_python,
            "python_version": st.python_version,
            "packages": packages or [],
            "check_packages": self.resolve_required_packages(model_id, packages),
            "ready": bool(st.ready),
            "missing": list(st.missing_packages),
            "detail": st.detail,
            "torch_version": st.torch_version,
            "torch_build": st.torch_build,
            "torch_cuda_available": bool(st.torch_cuda_available),
            "torch_cuda_version": st.torch_cuda_version,
        }
        if plan is not None:
            meta["torch_install"] = {
                "flavor": plan.flavor,
                "cuda_tag": plan.cuda_tag,
                "index_url": plan.index_url,
                "reason": plan.reason,
            }
        marker = env_dir / "env_meta.json"
        marker.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.invalidate_caches(env=model_id)

    def ensure_model_env(
        self,
        model_id: str,
        *,
        python_version: str = "3.12",
        packages: list[str] | None = None,
        progress: ProgressCb | None = None,
        force_recreate: bool = False,
    ) -> ModelEnvStatus:
        """
        使用 uv 为模型创建隔离 venv 并安装依赖。
        packages: 传给 `uv pip install` 的规格列表。
        """
        # 若未显式传入 packages，从注册表取完整依赖列表
        if not packages:
            try:
                from core.matting.model_registry import get_model_info
                meta = get_model_info(model_id)
                if meta and meta.env_packages:
                    packages = list(meta.env_packages)
            except Exception:
                packages = packages or []
        # 原始规格（写入 meta / 校验映射）；安装规格可经 GitHub 代理改写
        packages_orig = list(packages or [])
        packages = self.rewrite_git_package_specs(packages_orig)
        required_check = self.resolve_required_packages(model_id, packages_orig)
        need_git = packages_need_git(packages_orig)
        git_env = self.git_proxy_env() if need_git else {}

        with self._busy_lock:
            if self._busy:
                raise RuntimeError("已有环境任务在进行中，请稍候")
            self._busy = True

        self._last_torch_plan = None
        try:
            # 创建环境前刷新 PATH：用户可能刚装 Git/uv 且只点了「重新检测」
            refresh_process_path()

            if progress:
                progress(model_id, 2, "检查 uv…")
            # force：拾取手动拷贝到 runtime/uv 或 PATH 新装的 uv，并清 probe 缓存
            uv = self.resolve_uv(force=True)
            if not uv.found:
                if progress:
                    progress(model_id, 5, "未找到 uv，开始自动安装…")
                uv = self.install_uv(progress=progress)
            if uv.found and uv.path:
                ensure_exe_dir_on_path(uv.path)

            # git+https 依赖（如 BEN2）需要本机 git
            if need_git:
                if progress:
                    progress(model_id, 8, "检查 Git…")
                git = self.resolve_git(force=True)
                if not git.found:
                    raise RuntimeError(
                        "未检测到 Git 客户端。\n"
                        "BEN2 等模型需通过 git+https 拉取代码包，请先安装 Git。\n"
                        "安装后无需重启：到「开发环境」点「重新检测 Git」，"
                        "确认状态为已就绪后再创建环境。\n"
                        f"下载: {GIT_DOWNLOAD_URL}"
                    )
                # 再次确保 git 在 PATH（uv 子进程继承）
                if git.path:
                    ensure_exe_dir_on_path(git.path)
                # 安装 git 依赖时把当前 PATH 并入代理 env，避免 only insteadOf 丢 PATH
                if git_env is not None:
                    git_env = {
                        **git_env,
                        "PATH": os.environ.get("PATH", ""),
                    }
                gh_proxy = self.get_github_proxy()
                if progress:
                    git_label = git.version or git.path or "ok"
                    if gh_proxy:
                        progress(
                            model_id, 9,
                            f"Git 已就绪（{git_label}）· GitHub 代理: {gh_proxy}",
                        )
                    else:
                        progress(
                            model_id, 9,
                            f"Git 已就绪（{git_label}）· 直连 GitHub（可在开发环境配置代理）",
                        )

            # 目标小版本（注册表如 3.12）；严格限制在 3.10–3.12，避免 cp313/cp314 无 torch wheel
            target_py = (python_version or "3.12").strip() or "3.12"
            parts = target_py.split(".")
            try:
                target_major = int(parts[0])
                target_minor = int(parts[1]) if len(parts) > 1 else 12
            except ValueError:
                target_major, target_minor = 3, 12
                target_py = "3.12"
            if (target_major, target_minor) < (3, 10) or (target_major, target_minor) > (3, 12):
                if progress:
                    progress(
                        model_id, 12,
                        f"目标 Python {target_py} 超出支持范围，改用 3.12",
                    )
                target_py = "3.12"
                target_major, target_minor = 3, 12

            if progress:
                progress(model_id, 12, f"解析基础 Python（目标 {target_py}）…")
            # 仅接受目标小版本；找不到本机解释器时仍可用 uv 按版本号托管下载
            base = self.resolve_base_python(
                min_ver=(target_major, target_minor),
                max_ver=(target_major, target_minor),
            )
            if base is None:
                # 同主版本内可接受的兜底：3.10–3.12 中任意已安装版本（仍不接受 3.13+）
                base = self.resolve_base_python(
                    min_ver=(3, 10),
                    max_ver=(3, 12),
                )
            if base is not None and not self.get_saved_python_path():
                self.set_python_path(base.path)

            env_dir = self.model_env_dir(model_id)
            venv_dir = self.model_venv_dir(model_id)
            env_dir.mkdir(parents=True, exist_ok=True)

            need_recreate = bool(force_recreate)
            if venv_dir.exists() and not need_recreate:
                # 已有环境若 Python 不在 3.10–3.12，自动重建（修复历史误用 3.14 的环境）
                existing_py = self.model_python(model_id)
                if existing_py is not None:
                    ex_info = _probe_python(existing_py, use_cache=False)
                    if (
                        ex_info is None
                        or not ex_info.executable_ok
                        or (ex_info.major, ex_info.minor) < (3, 10)
                        or (ex_info.major, ex_info.minor) > (3, 12)
                    ):
                        ver = ex_info.version if ex_info else "?"
                        if progress:
                            progress(
                                model_id, 14,
                                f"已有 venv Python={ver} 不在 3.10–3.12，将自动重建为 {target_py}…",
                            )
                        need_recreate = True
                else:
                    need_recreate = True

            if need_recreate and venv_dir.exists():
                if progress:
                    progress(model_id, 15, "删除旧环境…")
                # 常驻抠图 worker 可能仍持有旧 venv 的 python.exe/DLL，先尽量关闭
                try:
                    from core.matting.inference import shutdown_matting_workers
                    shutdown_matting_workers()
                except Exception:
                    pass
                if not _rmtree_retry(venv_dir):
                    raise RuntimeError(
                        f"旧环境目录删除失败（可能被杀毒软件/占用进程锁定）:\n{venv_dir}\n\n"
                        "请关闭本软件后手动删除该目录，再重新创建环境。"
                    )

            if not self.model_python(model_id):
                # 优先按版本号创建：uv 可自动下载托管 CPython，避免误用本机 3.13/3.14
                # 本机已有精确匹配时再用其路径（更快、离线友好）
                create_attempts: list[tuple[str, str]] = []
                if base is not None and (base.major, base.minor) == (target_major, target_minor):
                    create_attempts.append((base.path, f"本机 {base.version}"))
                create_attempts.append((target_py, f"uv 托管 {target_py}"))
                if base is not None and (base.major, base.minor) != (target_major, target_minor):
                    # 仅当目标版本托管失败时，才退到本机 3.10–3.12 其它小版本
                    create_attempts.append(
                        (base.path, f"本机兜底 {base.version}")
                    )

                last_err = ""
                created = False
                for py_spec, label in create_attempts:
                    if progress:
                        progress(model_id, 20, f"创建虚拟环境（{label}）…")
                    # 托管下载可能较慢
                    timeout = 300 if py_spec == target_py else 180
                    r = _run(
                        # --clear：残留/损坏的 .venv 目录直接替换，避免
                        # "A directory already exists" 报错卡死重建流程
                        [uv.path, "venv", "--clear", "--python", py_spec, str(venv_dir)],
                        timeout=timeout,
                        cwd=env_dir,
                    )
                    if r.returncode == 0 and self.model_python(model_id):
                        created = True
                        break
                    last_err = _strip_ansi(r.stderr or r.stdout or "")
                    # 失败残留目录清理后再试下一种
                    if venv_dir.exists():
                        _rmtree_retry(venv_dir)

                if not created:
                    hint = (
                        f"无法创建 Python {target_py} 虚拟环境。\n"
                        "请确认：\n"
                        "1. 已安装 uv，且网络可访问 Python 构建源（uv 可自动下载）；或\n"
                        "2. 本机已安装 64 位 Python 3.10–3.12，并在「开发环境」中选中。\n"
                        "https://www.python.org/downloads/\n"
                        "勿使用 3.13/3.14：当前 PyTorch 官方轮子无对应 ABI，会导致 CUDA 安装失败。"
                    )
                    if venv_dir.exists():
                        hint += (
                            f"\n\n环境目录仍存在且无法自动清除（可能被杀毒软件/占用进程锁定）:\n"
                            f"{venv_dir}\n"
                            "请关闭本软件后手动删除该目录，再重新创建环境。"
                        )
                    raise RuntimeError(
                        f"创建 venv 失败:\n{last_err}\n\n{hint}"
                    )

            py = self.model_python(model_id)
            if py is None:
                raise RuntimeError("venv 创建后未找到 python 可执行文件")

            # 校验 venv 实际 Python 版本（双重保险）
            venv_info = _probe_python(py, use_cache=False)
            if venv_info is None or not venv_info.executable_ok:
                raise RuntimeError(f"无法探测 venv Python: {py}")
            if (venv_info.major, venv_info.minor) > (3, 12) or (
                venv_info.major, venv_info.minor
            ) < (3, 10):
                raise RuntimeError(
                    f"模型环境 Python 为 {venv_info.version}，不在支持范围 3.10–3.12。\n"
                    f"PyTorch 等依赖需要匹配的 ABI（如 cp312），"
                    f"请使用「强制重建」指定 Python {target_py}。\n"
                    f"环境目录: {venv_dir}"
                )
            if progress:
                progress(
                    model_id, 22,
                    f"venv Python: {venv_info.version}  ·  {py}",
                )

            # Windows：创建前检测 VC++（torch 强依赖）；不阻断安装，但提前提示
            if sys.platform == "win32":
                vc = detect_vc_redist(force=True)
                if progress and vc.applicable:
                    if not vc.ok:
                        progress(
                            model_id, 22,
                            "警告: VC++ 运行库异常，torch 可能无法加载 — 详见开发环境诊断",
                        )
                    elif vc.level == "warn":
                        progress(
                            model_id, 22,
                            f"提示: {vc.detail}",
                        )

            # 始终安装完整依赖（修复环境也会重装/补齐）
            if packages:
                if progress:
                    progress(
                        model_id, 30,
                        f"安装完整依赖（{len(packages)} 项，含 torch 等，可能较久）…",
                    )
                torch_pkgs, non_torch = split_torch_packages(packages)
                # 非 torch：先核心栈再其余（便于定位失败点）
                core = [
                    p for p in non_torch
                    if any(k in p.lower() for k in ("numpy", "pillow", "opencv"))
                ]
                rest = [p for p in non_torch if p not in core]
                pypi_batches = [b for b in (core, rest) if b]
                index_args = self.pip_index_args()
                if progress and index_args:
                    progress(
                        model_id, 28,
                        f"使用 PyPI 镜像: {self.get_pip_index_url()}",
                    )

                # ① 先装 torch（按主流系列清单选最优/备选 CUDA，失败逐级回退）
                torch_plan = plan_torch_install(force_detect=True)
                if torch_pkgs:
                    if progress:
                        progress(
                            model_id, 30,
                            f"PyTorch 方案: {torch_plan.display} — {torch_plan.reason}",
                        )
                        if torch_plan.candidate_tags:
                            progress(
                                model_id, 31,
                                "CUDA 候选: " + " → ".join(torch_plan.candidate_tags),
                            )
                        if torch_plan.driver_hint:
                            progress(
                                model_id, 31,
                                f"驱动提示: {torch_plan.driver_hint}",
                            )
                    adopted = self._install_torch_with_fallbacks(
                        uv_path=uv.path,
                        python_exe=str(py),
                        torch_pkgs=torch_pkgs,
                        plan=torch_plan,
                        cwd=env_dir,
                        progress=progress,
                        model_id=model_id,
                    )
                    if adopted is None:
                        raise RuntimeError(
                            "PyTorch 安装失败（CUDA 与 CPU 均未成功）。\n"
                            "请检查网络，或到「开发环境」更换镜像后重试。\n"
                            "RTX 50 系需 cu129/cu130/cu132 等含 sm_120 的构建。"
                        )
                    torch_plan = adopted
                    # 记录实际安装方案，供 meta / UI
                    self._last_torch_plan = torch_plan

                # ② 其余依赖走 PyPI 镜像（git+ 规格已按代理改写；子进程注入 insteadOf）
                done = 0
                total = max(len(non_torch), 1)
                for batch in pypi_batches:
                    if progress:
                        pct = 48 + int(35 * done / total)
                        progress(
                            model_id, pct,
                            f"uv pip install: {', '.join(batch)[:80]}…",
                        )
                    cmd = [
                        uv.path, "pip", "install",
                        "--python", str(py),
                        "--upgrade",
                        *index_args,
                        *batch,
                    ]
                    # 仅当本批含 git 规格时注入 git 代理环境
                    batch_env = git_env if packages_need_git(batch) else None
                    r = _run(cmd, timeout=None, cwd=env_dir, env=batch_env)
                    if r.returncode != 0:
                        err = _strip_ansi((r.stderr or r.stdout or "")[-2500:])
                        hint = ""
                        if packages_need_git(batch):
                            hint = (
                                "\n\n若失败与 GitHub 访问有关，请到「开发环境」："
                                "确认已安装 Git，并配置 GitHub 代理（如 ghfast）后重试。"
                            )
                        raise RuntimeError(f"依赖安装失败:\n{err}{hint}")
                    done += len(batch)

            if progress:
                progress(model_id, 88, "校验依赖是否可导入…")
            self.invalidate_caches(env=model_id)
            st = self.get_model_env_status(
                model_id, required_check or None, force=True
            )

            # 若仍缺依赖，尝试仅补装缺失项一次
            if (not st.ready) and st.missing_packages and packages:
                if progress:
                    progress(
                        model_id, 92,
                        f"补装缺失依赖: {', '.join(st.missing_packages)}",
                    )
                # 把 missing 映射回「安装规格」（已含代理改写）
                retry_specs = []
                miss_set = {m.lower() for m in st.missing_packages}
                for p_orig, p_inst in zip(packages_orig, packages):
                    key = _normalize_check_names([p_orig])
                    if key and key[0].lower() in miss_set:
                        retry_specs.append(p_inst)
                    elif "ben2" in p_orig.lower() and "ben2" in miss_set:
                        retry_specs.append(p_inst)
                if not retry_specs:
                    # 回退：直接用缺失名
                    retry_specs = list(st.missing_packages)
                cmd = [
                    uv.path, "pip", "install",
                    "--python", str(py),
                    "--upgrade",
                    *self.pip_index_args(),
                    *retry_specs,
                ]
                retry_env = git_env if packages_need_git(retry_specs) else None
                r = _run(cmd, timeout=None, cwd=env_dir, env=retry_env)
                if r.returncode != 0:
                    err = _strip_ansi((r.stderr or r.stdout or "")[-2000:])
                    raise RuntimeError(
                        f"补装依赖失败（缺少 {', '.join(st.missing_packages)}）:\n{err}"
                    )
                self.invalidate_caches(env=model_id)
                st = self.get_model_env_status(
                    model_id, required_check or None, force=True
                )

            self.write_env_meta(
                model_id,
                base_python=base.path,
                packages=packages_orig,
                status=st,
                torch_plan=getattr(self, "_last_torch_plan", None),
            )
            # 刷新 quick 缓存
            self._env_status_cache[f"{model_id}|q|"] = ModelEnvStatus(
                model_id=model_id,
                env_dir=str(env_dir),
                python_path=st.python_path,
                ready=st.ready,
                python_version=st.python_version,
                missing_packages=list(st.missing_packages),
                detail=st.detail,
            )
            if not st.ready:
                hint = format_env_failure_hint(
                    fail_kind=st.fail_kind,
                    detail=st.detail,
                    missing=list(st.missing_packages),
                )
                # st.detail 内可能已含 hint；避免完全重复时只保留一次
                body = (st.detail or "").strip()
                if hint and hint not in body:
                    body = f"{body}\n{hint}" if body else hint
                raise RuntimeError(
                    f"环境创建完成但依赖未就绪:\n{body}"
                )
            if progress:
                progress(model_id, 100, st.detail)
            return st
        finally:
            self._busy = False

    def ensure_model_env_async(
        self,
        model_id: str,
        *,
        python_version: str = "3.12",
        packages: list[str] | None = None,
        progress: ProgressCb | None = None,
        finished: Callable[[bool, str, ModelEnvStatus | None], None] | None = None,
        force_recreate: bool = False,
    ):
        def _run_job():
            ok, msg, st = False, "", None
            try:
                st = self.ensure_model_env(
                    model_id,
                    python_version=python_version,
                    packages=packages,
                    progress=progress,
                    force_recreate=force_recreate,
                )
                ok = st.ready
                msg = st.detail
            except Exception as e:
                ok = False
                msg = str(e)
            if finished:
                finished(ok, msg, st)

        t = threading.Thread(target=_run_job, name=f"env-{model_id}", daemon=True)
        t.start()

    def run_in_model_env(
        self,
        model_id: str,
        args: list[str],
        *,
        timeout: int | None = None,
        cwd: str | Path | None = None,
    ) -> subprocess.CompletedProcess:
        """在模型隔离环境中执行: python <args...>"""
        py = self.model_python(model_id)
        if py is None:
            raise RuntimeError(
                f"模型 {model_id} 的隔离环境未就绪，请先到「配置 → 抠图模型配置」创建环境"
            )
        return _run([str(py), *args], timeout=timeout, cwd=cwd)

    def diagnose_text(self) -> str:
        """生成开发环境诊断文本"""
        lines = []
        lines.append("=== PixelFlow 开发 / AI 运行时 ===")
        lines.append(f"运行时目录: {self.runtime_root()}")
        lines.append(f"模型环境目录: {self.envs_root()}")
        lines.append("")

        lines.append("— uv —")
        uv = self.resolve_uv()
        lines.append(uv.display if uv.found else "✗ 未检测到 uv")
        if not uv.found:
            lines.append(f"  目标目录: {self.bundled_uv_path().parent}")
            lines.append(
                "  可点「安装 / 修复 uv」，或手动放置 uv.exe 后点「重新检测 uv」"
                "（刷新 PATH，无需重启）。"
            )
        lines.append("")

        lines.append("— 系统 Python（可用于创建隔离环境）—")
        pys = self.discover_pythons()
        if not pys:
            lines.append("未检测到。请安装 Python 3.10–3.12（64 位）并勾选加入 PATH。")
        else:
            for i, p in enumerate(pys, 1):
                mark = ""
                if self._version_ok(p, self.DEFAULT_PY_MIN, self.DEFAULT_PY_MAX):
                    mark = " [推荐]"
                lines.append(f"{i}. {p.display}{mark}")
        saved = self.get_saved_python_path()
        if saved:
            lines.append(f"已保存选择: {saved}")
        lines.append("")

        lines.append("— Windows VC++ 运行库（PyTorch / 原生扩展）—")
        if sys.platform == "win32":
            vc = detect_vc_redist()
            mark = {"ok": "✓", "warn": "!", "bad": "✗"}.get(vc.level, "·")
            lines.append(f"{mark} {vc.display}")
            if vc.msvcp140_version:
                lines.append(f"  System32/msvcp140.dll = {vc.msvcp140_version}")
            if vc.vcruntime140_version:
                lines.append(f"  System32/vcruntime140.dll = {vc.vcruntime140_version}")
            if vc.registry_version:
                lines.append(f"  注册表 VC++ x64 = {vc.registry_version}")
            if not vc.ok or vc.level == "warn":
                lines.append(
                    "  说明: 若 msvcp140 仍为 14.00.x（VS2015 旧文件）而注册表已是 14.4x/14.5x，"
                    "属于运行库损坏/被旧版覆盖；import torch 会报 WinError 1114 / c10.dll。"
                )
                lines.append(f"  修复下载(x64): {VC_REDIST_X64_URL}")
                lines.append(f"  官方说明: {VC_REDIST_HELP_URL}")
                lines.append("  安装或「修复」后请重启电脑，再创建/修复模型环境。")
        else:
            lines.append("非 Windows，跳过")
        lines.append("")

        lines.append("— 依赖安装镜像（仅本应用 uv pip install -i）—")
        idx = self.get_pip_index_url()
        if idx:
            lines.append(idx)
        else:
            lines.append("（未指定，使用 uv/pip 默认源）")
        lines.append(f"默认推荐: {DEFAULT_PIP_INDEX_URL}")
        lines.append("")

        lines.append("— Git（git+https 依赖 / BEN2 等）—")
        git = self.resolve_git()
        if git.found:
            lines.append(f"✓ {git.display}")
        else:
            lines.append("✗ 未检测到 Git 客户端")
            lines.append(f"  下载安装: {GIT_DOWNLOAD_URL}")
            lines.append(
                "  安装时建议勾选加入 PATH；装完后点「重新检测 Git」即可"
                "（会刷新系统 PATH，一般无需重启软件）。"
            )
        gh = self.get_github_proxy()
        if gh:
            lines.append(f"GitHub 代理: {gh}")
            sample = "git+https://github.com/yincangshiwei/BEN2.git"
            lines.append(f"改写示例: {rewrite_github_url(sample, gh)}")
        else:
            lines.append("GitHub 代理: （未启用，直连 github.com）")
            lines.append(
                "  国内网络访问 GitHub 不稳定时，建议在开发环境启用 ghfast 等代理。"
            )
        lines.append("说明: 代理仅影响本应用安装过程，不修改系统 git 全局配置。")
        lines.append("")

        lines.append("— NVIDIA / PyTorch 安装策略（主流系列清单）—")
        nv = detect_nvidia_gpu()
        plan = plan_torch_install()
        matched = plan.match
        lines.append(f"GPU 检测: {nv.display}")
        if matched is not None:
            lines.append(f"系列匹配: {matched.series_name}  [{matched.match_method}]")
            if matched.summary:
                lines.append(f"摘要: {matched.summary}")
            if matched.candidate_tags:
                lines.append("CUDA 优选→备选: " + " → ".join(matched.candidate_tags))
            if matched.blocked_by_driver:
                lines.append(
                    "驱动不足暂不可用: " + ", ".join(matched.blocked_by_driver)
                )
            if matched.driver_hint:
                lines.append(f"驱动提示: {matched.driver_hint}")
                lines.append(f"驱动下载: {NVIDIA_DRIVER_URL}")
        lines.append(f"创建环境将安装: {plan.display}")
        lines.append(f"原因: {plan.reason}")
        if plan.flavor == "cuda" and plan.index_url:
            lines.append(f"CUDA 轮子索引: {plan.index_url}")
            lines.append(
                "说明: CUDA 版 torch 自带运行时，一般无需本机另装 CUDA Toolkit；"
                "一份 CUDA 版即可，配置里仍可强制用 CPU。"
                "RTX 50 系必须 cu129+。驱动不够请先升级再「创建/修复环境」。"
            )
        else:
            lines.append(
                "说明: 无可用 NVIDIA 时安装 CPU 版；"
                "有独显却仍装 CPU 版时请检查驱动 / nvidia-smi 是否在 PATH。"
            )
        lines.append("")
        lines.append("清单详见 core/runtime/gpu_catalog.py；配置页「AI 帮装」可复制完整指令。")
        lines.append("")

        lines.append("— 当前应用进程 —")
        lines.append(f"sys.executable: {sys.executable}")
        lines.append(f"frozen: {getattr(sys, 'frozen', False)}")
        lines.append(f"sys.version: {sys.version.split()[0]}")
        lines.append("")

        lines.append(
            "说明: AI 抠图不在主程序进程内加载 torch，"
            "而是为每个模型维护独立 uv 环境，通过子进程调用 worker 脚本，"
            "这样打包 exe 体积不受影响，且不同模型依赖互不冲突。"
            "安装依赖时使用上方镜像 -i，不会修改系统 pip/uv 全局配置。"
            "git+https（如 BEN2）需要本机 Git；GitHub 代理将地址改写为镜像前缀，"
            "同样不改用户全局 gitconfig。"
            "torch/torchvision 按本机 GPU 从 PyTorch 官方索引或 PyPI 安装。"
            "Windows 上 torch 还依赖系统 VC++ 运行库，与 pip 是否成功无关。"
        )
        return "\n".join(lines)


_mgr: RuntimeManager | None = None


def get_runtime_manager() -> RuntimeManager:
    global _mgr
    if _mgr is None:
        _mgr = RuntimeManager()
    return _mgr
