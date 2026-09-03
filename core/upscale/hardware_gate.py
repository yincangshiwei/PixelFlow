"""高清放大引擎的硬件/系统门禁。

DLSS5 的硬性要求（用户指定）：**必须是 NVIDIA RTX 40 系及以上显卡**，否则不可用。
社区实践虽把 RTX 30（Ampere）列为「实验路径」，但：
- 官方 NVIDIA DLSS 5 只声明支持 RTX 50 系，40 系靠社区 add-on 放开
- 参考实现的 runtime 层专门为 30 系写了 ``0xC0000005`` 访问违例诊断分支
- 社区已知案例「RTX 3060 渲染图片失败」
因此 30 系及以下一律阻断。

判定优先级：``compute_cap`` → 名称中的架构关键词 → GeForce ``RTX x0xx`` 数字 → 未知。
未知架构默认阻断（可在配置页打开「跳过显卡架构校验」应急放行）。
"""
from __future__ import annotations

import platform
import re
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.runtime.env_manager import detect_nvidia_gpu
from core.upscale.engine_registry import UpscaleEngineInfo, get_engine
from core.upscale.runtime_bundle import (
    EXPECTED_NR_VERSION,
    BundleStatus,
    inspect_bundle,
)

LEVEL_READY = "ready"
LEVEL_WARN = "warn"
LEVEL_BLOCKED = "blocked"

# 计算能力 → (架构代号, RTX 世代)
CC_TO_ARCH: dict[tuple[int, int], tuple[str, int]] = {
    (7, 5): ("Turing", 20),
    (8, 0): ("Ampere", 30),
    (8, 6): ("Ampere", 30),
    (8, 7): ("Ampere", 30),
    (8, 8): ("Ampere", 30),
    (8, 9): ("Ada", 40),
    (9, 0): ("Hopper", 0),          # H100 等数据中心卡，非 RTX、不支持 DLSS
    (10, 0): ("Blackwell", 50),
    (10, 3): ("Blackwell", 50),
    (11, 0): ("Blackwell", 50),
    (12, 0): ("Blackwell", 50),
    (12, 1): ("Blackwell", 50),
}

# 名称中的架构关键词（compute_cap 缺失时兜底）
_ARCH_KEYWORDS: tuple[tuple[str, str, int], ...] = (
    ("blackwell", "Blackwell", 50),
    ("ada lovelace", "Ada", 40),
    ("ada", "Ada", 40),
    ("ampere", "Ampere", 30),
    ("turing", "Turing", 20),
    ("hopper", "Hopper", 0),
)

# GeForce 命名：RTX 4090 / RTX 3060 Ti / RTX 2080 → 世代 40 / 30 / 20
_GEFORCE_RE = re.compile(r"\bRTX\s*(\d)\d{2,3}\b", re.IGNORECASE)

# Windows 11 起始内部版本号
WIN11_BUILD = 22000


@dataclass(frozen=True)
class UpscaleGpu:
    """单张 GPU 的架构判定结果。"""
    index: int = 0
    name: str = ""
    driver_version: str = ""
    compute_cap: str = ""
    compute_major: int = 0
    compute_minor: int = 0
    memory_mb: int = 0
    architecture: str = ""        # Turing / Ampere / Ada / Blackwell / ""
    generation: int = 0           # 20 / 30 / 40 / 50 / 0（未知）
    is_rtx: bool = False
    method: str = "none"          # cc / arch_keyword / name / none

    @property
    def memory_gb(self) -> float:
        return round(self.memory_mb / 1024.0, 1) if self.memory_mb > 0 else 0.0

    @property
    def sm_tag(self) -> str:
        return f"sm_{self.compute_major}{self.compute_minor}" if self.compute_major > 0 else ""

    @property
    def display(self) -> str:
        bits = [self.name or "NVIDIA GPU"]
        if self.architecture:
            bits.append(f"{self.architecture}" + (f" ({self.generation}系)" if self.generation else ""))
        if self.sm_tag:
            bits.append(self.sm_tag)
        if self.memory_gb:
            bits.append(f"{self.memory_gb:g} GB")
        if self.driver_version:
            bits.append(f"驱动 {self.driver_version}")
        return " · ".join(bits)


@dataclass(frozen=True)
class WindowsInfo:
    """操作系统门禁信息。"""
    is_windows: bool = False
    is_64bit: bool = False
    build: int = 0
    is_win11: bool = False
    version_text: str = ""


@dataclass(frozen=True)
class EngineAvailability:
    """一个放大引擎在本机的可用性判定。"""
    engine_id: str
    usable: bool
    level: str = LEVEL_BLOCKED          # ready / warn / blocked
    summary: str = ""
    reasons: tuple[str, ...] = ()       # 阻断原因
    warnings: tuple[str, ...] = ()      # 可用但需注意
    details: tuple[str, ...] = ()       # 展示用明细
    gpu: Optional[UpscaleGpu] = None    # 参与判定的最佳 GPU
    all_gpus: tuple[UpscaleGpu, ...] = ()
    windows: WindowsInfo = field(default_factory=WindowsInfo)
    bundle: Optional[BundleStatus] = None

    @property
    def reason_text(self) -> str:
        return "\n".join(self.reasons)


# ── 架构分类 ──

def classify_gpu(
    *,
    name: str,
    compute_major: int = 0,
    compute_minor: int = 0,
) -> tuple[str, int, bool, str]:
    """返回 (架构, 世代, 是否 RTX, 判定方式)。"""
    upper = (name or "").upper()
    is_rtx = "RTX" in upper

    if compute_major > 0:
        hit = CC_TO_ARCH.get((compute_major, compute_minor))
        if hit is not None:
            return hit[0], hit[1], is_rtx, "cc"

    for keyword, arch, gen in _ARCH_KEYWORDS:
        if keyword.upper() in upper:
            return arch, gen, is_rtx, "arch_keyword"

    m = _GEFORCE_RE.search(name or "")
    if m is not None:
        return {2: "Turing", 3: "Ampere", 4: "Ada", 5: "Blackwell"}.get(
            int(m.group(1)), "Unknown"
        ), int(m.group(1)) * 10, True, "name"

    return "", 0, is_rtx, "none"


def detect_upscale_gpus(*, force: bool = False) -> tuple[UpscaleGpu, ...]:
    """枚举本机 NVIDIA GPU 并逐张判定架构（复用 env_manager 的 nvidia-smi 探测）。"""
    nv = detect_nvidia_gpu(force=force)
    out: list[UpscaleGpu] = []
    for entry in (nv.gpus or []):
        arch, gen, is_rtx, method = classify_gpu(
            name=entry.name,
            compute_major=entry.compute_major,
            compute_minor=entry.compute_minor,
        )
        out.append(
            UpscaleGpu(
                index=entry.index,
                name=entry.name,
                driver_version=entry.driver_version or nv.driver_version,
                compute_cap=entry.compute_cap,
                compute_major=entry.compute_major,
                compute_minor=entry.compute_minor,
                memory_mb=entry.memory_mb,
                architecture=arch,
                generation=gen,
                is_rtx=is_rtx,
                method=method,
            )
        )
    if not out:
        # env_manager 未拿到逐卡明细（老驱动降级查询）时，用汇总字段兜一条
        for i, name in enumerate(nv.gpu_names or []):
            arch, gen, is_rtx, method = classify_gpu(
                name=name,
                compute_major=nv.compute_major if i == 0 else 0,
                compute_minor=nv.compute_minor if i == 0 else 0,
            )
            out.append(
                UpscaleGpu(
                    index=i, name=name, driver_version=nv.driver_version,
                    compute_cap=nv.compute_cap if i == 0 else "",
                    compute_major=nv.compute_major if i == 0 else 0,
                    compute_minor=nv.compute_minor if i == 0 else 0,
                    architecture=arch, generation=gen, is_rtx=is_rtx, method=method,
                )
            )
    return tuple(out)


def pick_best_gpu(gpus: tuple[UpscaleGpu, ...]) -> Optional[UpscaleGpu]:
    """挑出世代最高的一张（世代相同取显存更大者）。"""
    best: Optional[UpscaleGpu] = None
    for g in gpus:
        if best is None or (g.generation, g.memory_mb) > (best.generation, best.memory_mb):
            best = g
    return best


def detect_windows_info() -> WindowsInfo:
    """操作系统与位数信息。"""
    is_win = sys.platform == "win32"
    is_64 = (sys.maxsize > 2 ** 32) and platform.machine().lower() in (
        "amd64", "x86_64", "arm64", "aarch64"
    )
    build = 0
    text = platform.version() or ""
    if is_win:
        try:
            wv = sys.getwindowsversion()
            build = int(getattr(wv, "build", 0) or 0)
            text = f"{wv.major}.{wv.minor} (build {build})"
        except Exception:
            m = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
            if m:
                build = int(m.group(3))
    return WindowsInfo(
        is_windows=is_win,
        is_64bit=is_64,
        build=build,
        is_win11=is_win and build >= WIN11_BUILD,
        version_text=text,
    )


# ── 判定 ──

_cache: dict[str, EngineAvailability] = {}
_cache_lock = threading.Lock()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def check_engine(
    engine_id: str,
    *,
    force: bool = False,
    bundle_root: Optional[Path | str] = None,
    allow_unverified_gpu: Optional[bool] = None,
) -> EngineAvailability:
    """判定引擎在本机是否可用。

    :param force: 忽略缓存重新检测（配置页「重新检测」用）
    :param bundle_root: 指定外挂运行时目录；None 则读设置/默认目录
    :param allow_unverified_gpu: None 则读设置
    """
    with _cache_lock:
        if not force and engine_id in _cache:
            return _cache[engine_id]

    engine = get_engine(engine_id)
    win = detect_windows_info()
    if allow_unverified_gpu is None:
        from core.upscale.upscale_settings import get_allow_unverified_gpu
        allow_unverified_gpu = get_allow_unverified_gpu()

    if engine is None:
        av = EngineAvailability(
            engine_id=engine_id, usable=False, level=LEVEL_BLOCKED,
            summary="未知引擎", reasons=(f"引擎 {engine_id!r} 未在注册表中定义",),
            windows=win,
        )
        with _cache_lock:
            _cache[engine_id] = av
        return av

    reasons: list[str] = []
    warnings: list[str] = []
    details: list[str] = []

    # ── 1. 操作系统 ──
    if sys.platform not in engine.platforms:
        reasons.append(
            f"{engine.name} 仅支持 "
            f"{'、'.join(sorted(engine.platforms))}，当前系统为 {sys.platform}"
        )
    if not win.is_64bit:
        reasons.append("需要 64 位操作系统与 64 位 Python 运行时")
    details.append(f"系统: {platform.system()} {win.version_text}"
                   + ("（64 位）" if win.is_64bit else "（非 64 位）"))
    if win.is_windows and not win.is_win11 and not reasons:
        warnings.append(
            f"当前为 Windows 10（build {win.build}）。DLSS5 上游要求 64 位 Windows 11 + "
            "Direct3D 12，Windows 10 可能无法初始化渲染入口。"
        )

    # ── 2. 显卡 ──
    gpus = detect_upscale_gpus(force=force)
    best = pick_best_gpu(gpus)
    if not gpus:
        reasons.append(
            "未检测到 NVIDIA 显卡（nvidia-smi 不可用或无 NVIDIA 驱动）。"
            f"{engine.name} 需要 RTX {engine.min_gpu_generation} 系及以上显卡。"
        )
        details.append("显卡: 未检测到")
    elif best is not None:
        details.append(f"显卡: {best.display}")
        if len(gpus) > 1:
            details.append(f"（本机共 {len(gpus)} 张 NVIDIA 卡，按世代最高者判定）")
        if engine.gpu_vendor == "nvidia" and not best.is_rtx:
            reasons.append(
                f"检测到 {best.name}，但不是 RTX 显卡。"
                f"{engine.name} 需要 GeForce RTX / RTX PRO 系列。"
            )
        elif best.generation <= 0:
            msg = (
                f"无法识别显卡架构（{best.name}"
                + (f"，compute_cap={best.compute_cap}" if best.compute_cap else "，nvidia-smi 未报告 compute_cap")
                + f"）。{engine.name} 需要 RTX {engine.min_gpu_generation} 系及以上。"
            )
            if allow_unverified_gpu:
                warnings.append(msg + "（已开启「跳过显卡架构校验」，将尝试运行）")
            else:
                reasons.append(msg + " 若确认显卡符合要求，可在配置页开启「跳过显卡架构校验」。")
        elif best.generation < engine.min_gpu_generation:
            reasons.append(
                f"显卡为 RTX {best.generation} 系"
                + (f"（{best.architecture}）" if best.architecture else "")
                + f"，{engine.name} 需要 RTX {engine.min_gpu_generation} 系及以上。"
            )
            if best.generation == 30:
                reasons.append(
                    "RTX 30 系在上游属实验路径：官方 DLSS 5 未支持，实测常见 "
                    "0xC0000005 访问违例崩溃且速度极慢，故本软件直接禁用。"
                )
        else:
            details.append(
                f"门禁: ✅ 满足 RTX {engine.min_gpu_generation} 系及以上"
                f"（实测 {best.generation} 系 {best.architecture or ''}）".rstrip()
            )
        if best.memory_mb and best.memory_gb < 6.0 and not reasons:
            warnings.append(
                f"显存 {best.memory_gb:g} GB 偏低，大尺寸图片放大可能失败或极慢，"
                "建议先降低放大档位。"
            )
        if best.generation == 40 and not reasons:
            warnings.append(
                "NVIDIA 官方 DLSS 5 仅声明支持 RTX 50 系；RTX 40 系依靠 RenoDX 社区 "
                "add-on 放开，属非官方路径，驱动更新后可能失效。"
            )

    # ── 3. 外挂运行时 ──
    bundle: Optional[BundleStatus] = None
    if engine.runtime_kind == "external_bundle" and not reasons:
        bundle = inspect_bundle(bundle_root)
        if not bundle.found:
            reasons.append(bundle.detail or "未找到 DLSS5 运行时目录")
        else:
            if bundle.missing:
                reasons.append("DLSS5 运行时缺少必需文件：\n  " + "\n  ".join(bundle.missing))
            if not bundle.writable:
                reasons.append(
                    f"DLSS5 运行时的 host 目录不可写：{bundle.host_dir}\n"
                    "渲染入口每次运行都要重写 host/ReShade.ini，"
                    "请移到可写目录（勿放 C:\\Program Files）或以管理员身份运行。"
                )
            if bundle.ready:
                details.append(
                    f"运行时: ✅ {bundle.layout} 布局 · {bundle.total_size_mb:.0f} MB"
                    + (f" · DLSSNR {bundle.nr_version}" if bundle.nr_version else "")
                )
                if bundle.nr_version and not bundle.version_ok:
                    warnings.append(
                        f"NVIDIA DLSSNR 版本 {bundle.nr_version}，已验证版本为 "
                        f"{EXPECTED_NR_VERSION}；若渲染失败请换用上游 v5.0 便携包。"
                    )
    elif engine.runtime_kind == "external_bundle":
        # 前置门禁已失败时仍给出运行时状态，方便配置页一次性展示全部问题
        bundle = inspect_bundle(bundle_root)

    # ── 4. 通用提示 ──
    if best is not None and best.driver_version:
        details.append("提示: 建议使用最新 Game Ready / Studio 驱动")

    if reasons:
        level = LEVEL_BLOCKED
        summary = reasons[0].splitlines()[0]
    elif warnings:
        level = LEVEL_WARN
        summary = "可用（有注意事项）"
    else:
        level = LEVEL_READY
        summary = "就绪"

    av = EngineAvailability(
        engine_id=engine.id,
        usable=not reasons,
        level=level,
        summary=summary,
        reasons=tuple(reasons),
        warnings=tuple(warnings),
        details=tuple(details),
        gpu=best,
        all_gpus=gpus,
        windows=win,
        bundle=bundle,
    )
    with _cache_lock:
        _cache[engine_id] = av
    return av


def engine_choice_label(engine: UpscaleEngineInfo, av: Optional[EngineAvailability] = None) -> str:
    """引擎下拉项文本：不可用时附带原因，便于用户直接看懂。"""
    base = f"{engine.icon} {engine.name}".strip()
    if av is None:
        return base
    if av.level == LEVEL_BLOCKED:
        short = av.reasons[0].splitlines()[0] if av.reasons else "不可用"
        return f"{base}（不可用：{short}）"
    if av.level == LEVEL_WARN:
        return f"{base}（可用，有提示）"
    return f"{base}（就绪）"


__all__ = [
    "LEVEL_READY", "LEVEL_WARN", "LEVEL_BLOCKED",
    "CC_TO_ARCH",
    "WIN11_BUILD",
    "UpscaleGpu",
    "WindowsInfo",
    "EngineAvailability",
    "classify_gpu",
    "detect_upscale_gpus",
    "pick_best_gpu",
    "detect_windows_info",
    "check_engine",
    "clear_cache",
    "engine_choice_label",
]
