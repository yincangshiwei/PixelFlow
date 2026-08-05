"""
主流 NVIDIA GPU 系列 ↔ PyTorch CUDA 轮子匹配清单。

用途：
- 创建/修复环境时选最优 CUDA 标签与备选
- 驱动不足时提示升级
- 生成给 Codex / CodeBuddy 等 AI 工具的「帮装」指令

说明：
- PyTorch wheel 自带 CUDA 运行时，一般无需本机安装 CUDA Toolkit
- 关键的是「驱动支持的最高 CUDA」与「wheel 内含的 GPU 架构 (sm_xx)」
- RTX 50 系 (Blackwell, sm_120) 必须用 cu129+，否则 no kernel image
"""
from __future__ import annotations

from dataclasses import dataclass, field


# ── PyTorch 官方 CUDA wheel 标签（从新到旧）──
# 文档: https://pytorch.org/get-started/locally/
TORCH_CUDA_TAGS: tuple[str, ...] = (
    "cu132",
    "cu130",
    "cu129",
    "cu126",
    "cu124",
    "cu121",
    "cu118",
)

PYTORCH_WHL_BASE = "https://download.pytorch.org/whl"

# wheel 标签 → 驱动需报告的最低「CUDA Version」（nvidia-smi 右上角）
# 数值为近似门槛；实际以 NVIDIA / PyTorch 发布说明为准
TAG_MIN_DRIVER_CUDA: dict[str, tuple[int, int]] = {
    "cu132": (13, 2),
    "cu130": (13, 0),
    "cu129": (12, 9),
    "cu126": (12, 6),
    "cu124": (12, 4),
    "cu121": (12, 1),
    "cu118": (11, 8),
}

# Windows 参考最低驱动版本（展示用，非硬校验）
TAG_MIN_DRIVER_WIN: dict[str, str] = {
    "cu132": "570+",
    "cu130": "570+",
    "cu129": "560+",
    "cu126": "560+",
    "cu124": "550+",
    "cu121": "530+",
    "cu118": "520+",
}

DEFAULT_CUDA_TAG = "cu126"
# Blackwell 及更新架构的最低标签
BLACKWELL_MIN_TAG = "cu129"


@dataclass(frozen=True)
class GpuSeriesProfile:
    """一个主流 GPU 系列的匹配档案。"""
    id: str
    name: str                         # 显示名，如 "RTX 50 系 (Blackwell)"
    # 计算能力区间 [cc_min, cc_max)，max 为 None 表示无上界
    cc_min: tuple[int, int]
    cc_max: tuple[int, int] | None
    # 名称关键词（小写匹配），辅助识别
    name_keywords: tuple[str, ...] = ()
    # 优选 → 备选（均需满足架构；再按驱动过滤）
    preferred_tags: tuple[str, ...] = ()
    # 该系列 wheel 必须达到的最低标签（防止装到无 sm_xx 的旧包）
    min_tag: str = "cu118"
    notes: str = ""
    # 架构代号，展示用
    arch: str = ""
    sm_examples: tuple[str, ...] = ()


# 主流消费级 / 专业卡系列（按 CC 从新到旧）
GPU_SERIES: tuple[GpuSeriesProfile, ...] = (
    GpuSeriesProfile(
        id="blackwell",
        name="RTX 50 系 (Blackwell)",
        cc_min=(12, 0),
        cc_max=None,
        name_keywords=(
            "rtx 50", "rtx50", "geforce rtx 50",
            "5060", "5070", "5080", "5090",
            "blackwell",
        ),
        preferred_tags=("cu132", "cu130", "cu129"),
        min_tag=BLACKWELL_MIN_TAG,
        arch="Blackwell",
        sm_examples=("sm_120",),
        notes=(
            "必须使用 cu129 及以上构建，否则会出现 "
            "「CUDA error: no kernel image is available」。"
            "cu126/cu124 不含 sm_120 内核。"
        ),
    ),
    GpuSeriesProfile(
        id="ada",
        name="RTX 40 系 (Ada Lovelace)",
        cc_min=(8, 9),
        cc_max=(12, 0),
        name_keywords=(
            "rtx 40", "rtx40", "geforce rtx 40",
            "4060", "4070", "4080", "4090",
            "ada lovelace", "ada",
        ),
        preferred_tags=("cu126", "cu124", "cu121"),
        min_tag="cu118",
        arch="Ada Lovelace",
        sm_examples=("sm_89",),
        notes="推荐 cu126/cu124；驱动较新时可直接用默认 CUDA 版。",
    ),
    GpuSeriesProfile(
        id="ampere",
        name="RTX 30 系 (Ampere)",
        cc_min=(8, 0),
        cc_max=(8, 9),
        name_keywords=(
            "rtx 30", "rtx30", "geforce rtx 30",
            "3060", "3070", "3080", "3090",
            "a100", "a10", "a40", "ampere",
        ),
        preferred_tags=("cu126", "cu124", "cu121"),
        min_tag="cu118",
        arch="Ampere",
        sm_examples=("sm_80", "sm_86"),
        notes="消费级 30 系多为 sm_86；数据中心 A100 为 sm_80。",
    ),
    GpuSeriesProfile(
        id="turing",
        name="RTX 20 / GTX 16 系 (Turing)",
        cc_min=(7, 5),
        cc_max=(8, 0),
        name_keywords=(
            "rtx 20", "rtx20", "geforce rtx 20",
            "2060", "2070", "2080",
            "gtx 16", "gtx16", "1650", "1660",
            "turing", "quadro rtx",
        ),
        preferred_tags=("cu124", "cu121", "cu118"),
        min_tag="cu118",
        arch="Turing",
        sm_examples=("sm_75",),
        notes="仍被主流 torch CUDA 轮子支持；建议驱动保持更新。",
    ),
    GpuSeriesProfile(
        id="volta",
        name="Volta (V100 等)",
        cc_min=(7, 0),
        cc_max=(7, 5),
        name_keywords=("v100", "titan v", "volta"),
        preferred_tags=("cu124", "cu121", "cu118"),
        min_tag="cu118",
        arch="Volta",
        sm_examples=("sm_70",),
        notes="以专业/数据中心卡为主。",
    ),
    GpuSeriesProfile(
        id="pascal",
        name="GTX 10 系 (Pascal)",
        cc_min=(6, 0),
        cc_max=(7, 0),
        name_keywords=(
            "gtx 10", "gtx10", "1050", "1060", "1070", "1080",
            "pascal", "titan x", "p100",
        ),
        preferred_tags=("cu121", "cu118"),
        min_tag="cu118",
        arch="Pascal",
        sm_examples=("sm_60", "sm_61"),
        notes="部分新版 torch 可能逐步减少对旧架构支持；优先较稳的 cu118/cu121。",
    ),
    GpuSeriesProfile(
        id="maxwell_older",
        name="Maxwell 及更早",
        cc_min=(5, 0),
        cc_max=(6, 0),
        name_keywords=("gtx 9", "gtx 7", "gtx 8", "maxwell", "kepler"),
        preferred_tags=("cu118",),
        min_tag="cu118",
        arch="Maxwell+",
        sm_examples=("sm_50", "sm_52"),
        notes="新版 PyTorch 支持有限，建议 CPU 推理或更换较新显卡。",
    ),
)


@dataclass
class GpuMatchResult:
    """本机 GPU 与清单的匹配结果。"""
    has_gpu: bool = False
    series: GpuSeriesProfile | None = None
    gpu_name: str = ""
    compute_cap: str = ""
    compute_major: int = 0
    compute_minor: int = 0
    sm_tag: str = ""
    driver_version: str = ""
    driver_cuda: str = ""
    driver_cuda_major: int = 0
    driver_cuda_minor: int = 0

    # 安装标签
    primary_tag: str = ""
    candidate_tags: list[str] = field(default_factory=list)
    # 驱动是否满足 primary
    driver_ok_for_primary: bool = True
    # 驱动是否至少满足系列 min_tag
    driver_ok_for_series: bool = True
    # 因驱动被滤掉的更优标签
    blocked_by_driver: list[str] = field(default_factory=list)
    # 给用户的说明
    summary: str = ""
    driver_hint: str = ""
    notes: str = ""
    match_method: str = ""  # cc / name / default / none

    @property
    def series_name(self) -> str:
        return self.series.name if self.series else ("无 NVIDIA GPU" if not self.has_gpu else "未识别系列")

    @property
    def primary_index_url(self) -> str:
        if not self.primary_tag:
            return ""
        return f"{PYTORCH_WHL_BASE}/{self.primary_tag}"


def tag_rank(tag: str) -> int:
    """标签越新 rank 越大。"""
    try:
        return len(TORCH_CUDA_TAGS) - 1 - TORCH_CUDA_TAGS.index(tag)
    except ValueError:
        return -1


def driver_supports_tag(
    driver_major: int,
    driver_minor: int,
    tag: str,
) -> bool:
    """驱动报告的最高 CUDA 是否达到该 wheel 标签门槛。"""
    need = TAG_MIN_DRIVER_CUDA.get(tag)
    if not need:
        return True
    if driver_major <= 0:
        # 未知驱动版本时不阻断，交给安装与运行时校验
        return True
    return (driver_major, driver_minor) >= need


def _cc_tuple(major: int, minor: int) -> tuple[int, int]:
    return (max(0, int(major)), max(0, int(minor)))


def _cc_in_range(
    cc: tuple[int, int],
    lo: tuple[int, int],
    hi: tuple[int, int] | None,
) -> bool:
    if cc < lo:
        return False
    if hi is not None and cc >= hi:
        return False
    return True


def match_series_by_cc(compute_major: int, compute_minor: int) -> GpuSeriesProfile | None:
    if compute_major <= 0:
        return None
    cc = _cc_tuple(compute_major, compute_minor)
    for s in GPU_SERIES:
        if _cc_in_range(cc, s.cc_min, s.cc_max):
            return s
    return None


def match_series_by_name(gpu_name: str) -> GpuSeriesProfile | None:
    name = (gpu_name or "").lower()
    if not name:
        return None
    # 按系列从新到旧，避免 "rtx 20" 误伤
    for s in GPU_SERIES:
        for kw in s.name_keywords:
            if kw and kw in name:
                return s
    return None


def resolve_series(
    *,
    gpu_name: str = "",
    compute_major: int = 0,
    compute_minor: int = 0,
) -> tuple[GpuSeriesProfile | None, str]:
    """返回 (系列, 匹配方式 cc|name|none)。优先计算能力。"""
    by_cc = match_series_by_cc(compute_major, compute_minor)
    if by_cc is not None:
        return by_cc, "cc"
    by_name = match_series_by_name(gpu_name)
    if by_name is not None:
        return by_name, "name"
    return None, "none"


def filter_tags_for_series(
    series: GpuSeriesProfile | None,
    *,
    driver_major: int = 0,
    driver_minor: int = 0,
) -> tuple[list[str], list[str], str]:
    """
    返回 (可用候选 tags 从优到劣, 被驱动挡住的更优 tags, primary_tag)。
    """
    if series is None:
        # 未知系列：按驱动给通用列表
        base = list(TORCH_CUDA_TAGS)
        min_rank = tag_rank("cu118")
    else:
        # 系列优选在前，再补全不低于 min_tag 的其它标签
        base = []
        for t in series.preferred_tags:
            if t not in base:
                base.append(t)
        min_rank = tag_rank(series.min_tag)
        for t in TORCH_CUDA_TAGS:
            if tag_rank(t) >= min_rank and t not in base:
                base.append(t)

    usable: list[str] = []
    blocked: list[str] = []
    for t in base:
        if tag_rank(t) < min_rank:
            continue
        if driver_supports_tag(driver_major, driver_minor, t):
            usable.append(t)
        else:
            blocked.append(t)

    if not usable:
        # 驱动过旧：仍给出系列 min 作为「目标标签」，由上层提示升级驱动后再装
        fallback = series.min_tag if series else DEFAULT_CUDA_TAG
        # blocked 去重且不把 fallback 重复算进「更优被挡」的误导文案
        blocked_unique = []
        for t in blocked:
            if t not in blocked_unique:
                blocked_unique.append(t)
        return [fallback], blocked_unique, fallback

    primary = usable[0]
    return usable, blocked, primary


def match_gpu(
    *,
    has_gpu: bool,
    gpu_name: str = "",
    compute_cap: str = "",
    compute_major: int = 0,
    compute_minor: int = 0,
    driver_version: str = "",
    driver_cuda: str = "",
    driver_cuda_major: int = 0,
    driver_cuda_minor: int = 0,
) -> GpuMatchResult:
    """综合硬件信息生成匹配结果。"""
    r = GpuMatchResult(
        has_gpu=has_gpu,
        gpu_name=gpu_name or "",
        compute_cap=compute_cap or "",
        compute_major=compute_major,
        compute_minor=compute_minor,
        driver_version=driver_version or "",
        driver_cuda=driver_cuda or "",
        driver_cuda_major=driver_cuda_major,
        driver_cuda_minor=driver_cuda_minor,
    )
    if compute_major > 0:
        r.sm_tag = f"sm_{compute_major}{compute_minor}"

    if not has_gpu:
        r.summary = "未检测到 NVIDIA GPU，将安装 CPU 版 PyTorch"
        r.match_method = "none"
        return r

    series, method = resolve_series(
        gpu_name=gpu_name,
        compute_major=compute_major,
        compute_minor=compute_minor,
    )
    r.series = series
    r.match_method = method

    candidates, blocked, primary = filter_tags_for_series(
        series,
        driver_major=driver_cuda_major,
        driver_minor=driver_cuda_minor,
    )
    r.candidate_tags = candidates
    r.blocked_by_driver = blocked
    r.primary_tag = primary
    r.notes = series.notes if series else ""

    r.driver_ok_for_primary = driver_supports_tag(
        driver_cuda_major, driver_cuda_minor, primary
    ) if primary else True

    min_tag = series.min_tag if series else DEFAULT_CUDA_TAG
    r.driver_ok_for_series = driver_supports_tag(
        driver_cuda_major, driver_cuda_minor, min_tag
    )

    # 摘要
    sn = series.name if series else "未识别系列（按通用策略）"
    parts = [f"匹配: {sn}"]
    if r.sm_tag:
        parts.append(r.sm_tag)
    if r.driver_cuda:
        parts.append(f"驱动 CUDA≤{r.driver_cuda}")
    parts.append(f"优选 {primary}")
    if len(candidates) > 1:
        parts.append("备选 " + " → ".join(candidates[1:4]))
    r.summary = " · ".join(parts)

    # 驱动提示
    if blocked and not r.driver_ok_for_series:
        need = TAG_MIN_DRIVER_CUDA.get(min_tag, (12, 0))
        win_drv = TAG_MIN_DRIVER_WIN.get(min_tag, "")
        r.driver_hint = (
            f"当前驱动 CUDA 最高约 {r.driver_cuda or '未知'}，"
            f"低于本系列最低要求 {min_tag}（需驱动 CUDA≥{need[0]}.{need[1]}"
            + (f"，Windows 驱动建议 {win_drv}" if win_drv else "")
            + "）。请先升级 NVIDIA 驱动，再点「创建/修复环境」。"
        )
        r.driver_ok_for_primary = False
        r.driver_ok_for_series = False
    elif blocked:
        best_blocked = blocked[0]
        need = TAG_MIN_DRIVER_CUDA.get(best_blocked, (0, 0))
        win_drv = TAG_MIN_DRIVER_WIN.get(best_blocked, "")
        r.driver_hint = (
            f"驱动暂不支持更优标签 {best_blocked}"
            f"（需 CUDA≥{need[0]}.{need[1]}"
            + (f" / 驱动 {win_drv}" if win_drv else "")
            + f"）。将使用 {primary}；升级驱动后可重装以获得更新构建。"
        )
    elif series and series.id == "blackwell":
        r.driver_hint = (
            "RTX 50 系需 cu129+。若推理报 no kernel image，"
            "请确认已装 cu129/cu130/cu132，并升级到最新 Game Ready / Studio 驱动。"
        )

    return r


def catalog_markdown() -> str:
    """生成清单说明文本（诊断 / AI 指令用）。"""
    lines = [
        "## 主流 GPU 系列 ↔ PyTorch CUDA 匹配清单",
        "",
        "| 系列 | 架构 / SM | 优选 CUDA | 最低 CUDA | 说明 |",
        "|------|-----------|-----------|-----------|------|",
    ]
    for s in GPU_SERIES:
        sm = ", ".join(s.sm_examples) or "-"
        pref = " → ".join(s.preferred_tags) or "-"
        note = (s.notes or "").replace("|", "/")[:40]
        lines.append(
            f"| {s.name} | {s.arch} / {sm} | {pref} | {s.min_tag} | {note} |"
        )
    lines.extend(
        [
            "",
            "### 标签与驱动门槛（nvidia-smi 的 CUDA Version）",
            "",
        ]
    )
    for tag in TORCH_CUDA_TAGS:
        need = TAG_MIN_DRIVER_CUDA.get(tag, (0, 0))
        win = TAG_MIN_DRIVER_WIN.get(tag, "")
        lines.append(
            f"- `{tag}`: 驱动 CUDA ≥ {need[0]}.{need[1]}"
            + (f"，Windows 驱动建议 {win}" if win else "")
        )
    lines.extend(
        [
            "",
            "规则：有独显只装 **一份 CUDA 版** torch（可在应用内强制用 CPU）；",
            "无独显装 CPU 版。Blackwell 切勿使用 cu126 及更旧构建。",
            "",
        ]
    )
    return "\n".join(lines)
