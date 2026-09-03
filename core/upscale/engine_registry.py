"""放大引擎注册表 —— 「选哪个引擎就显示哪个的参数」的唯一权威来源。

每个引擎自带一套 ``ParamSpec`` 参数 schema：
- UI（``UpscaleFeatureRoute``）按 schema 动态生成面板，**不为任何引擎写死控件**
- Service（``UpscaleService``）按 schema 做校验/规范化，**不为任何引擎写死字段**
- 新增引擎只需往 ``UPSCALE_ENGINES`` 加一条 + 在 ``core/upscale/<engine>.py`` 实现后端

参数取值约定：``combo`` 的 ``choices`` 为 ``(显示文本, 实际值)`` 二元组，
实际值即写入 options / 预设 JSON 的内容（可跨语言稳定）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import config


# ── 参数控件类型 ──
KIND_COMBO = "combo"
KIND_FLOAT = "float"
KIND_INT = "int"
KIND_BOOL = "bool"

_VALID_KINDS = frozenset({KIND_COMBO, KIND_FLOAT, KIND_INT, KIND_BOOL})


@dataclass(frozen=True)
class ParamSpec:
    """单个参数的完整描述（UI 渲染 + Service 校验共用）。"""

    key: str
    label: str
    kind: str = KIND_COMBO
    default: Any = None
    # 布局：group → 一个 QGroupBox；row 相同者在同一行横排（宽屏优先，单行 ≤4 组）
    group: str = ""
    row: int = 0
    # combo
    choices: tuple[tuple[str, Any], ...] = ()
    # float / int
    lo: float = 0.0
    hi: float = 1.0
    step: float = 1.0
    decimals: int = 2
    suffix: str = ""            # 单位后缀，如 "×"
    # 通用
    tooltip: str = ""
    advanced: bool = False      # True → 折叠进「高级」分组
    min_width: int = 0
    editable: bool = False      # combo 是否可编辑（如自定义 GPU）

    def __post_init__(self):
        if not self.key or not str(self.key).strip():
            raise ValueError("ParamSpec.key 不能为空")
        if self.kind not in _VALID_KINDS:
            raise ValueError(f"ParamSpec {self.key}: 未知控件类型 {self.kind!r}")
        if self.kind == KIND_COMBO and not self.choices:
            raise ValueError(f"ParamSpec {self.key}: combo 必须提供 choices")
        if self.kind in (KIND_FLOAT, KIND_INT) and self.lo > self.hi:
            raise ValueError(f"ParamSpec {self.key}: lo 不能大于 hi")

    @property
    def choice_values(self) -> tuple[Any, ...]:
        return tuple(value for _text, value in self.choices)

    def clamp(self, value: Any) -> Any:
        """把任意输入收敛到合法值（不抛错，供非严格模式使用）。"""
        if self.kind == KIND_BOOL:
            return bool(value)
        if self.kind == KIND_COMBO:
            if value in self.choice_values:
                return value
            # 允许传入显示文本（UI 回填 / 手写预设）
            for text, val in self.choices:
                if str(value) == text:
                    return val
            return self.default
        if self.kind == KIND_INT:
            try:
                n = int(round(float(value)))
            except (TypeError, ValueError):
                return self.default
            return max(int(self.lo), min(int(self.hi), n))
        # float
        try:
            f = float(value)
        except (TypeError, ValueError):
            return self.default
        f = max(float(self.lo), min(float(self.hi), f))
        return round(f, int(self.decimals))


@dataclass(frozen=True)
class UpscaleEngineInfo:
    """一个高清放大引擎的完整描述。"""

    id: str
    name: str
    icon: str = ""
    description: str = ""
    # 平台门禁：不在其中的平台上引擎不可用（UI 置灰）
    platforms: frozenset = field(default_factory=lambda: frozenset({"win32", "darwin", "linux"}))
    # 显卡门禁：0 = 不限；40 = 需 RTX 40 系及以上
    min_gpu_generation: int = 0
    gpu_vendor: str = ""            # "nvidia" / ""（不限）
    # 运行时形态：builtin（纯 Python/Pillow）| external_bundle（外挂二进制目录）
    runtime_kind: str = "builtin"
    params: tuple[ParamSpec, ...] = ()
    notes: str = ""
    license_note: str = ""
    download_page: str = ""
    # 输出尺寸硬上限（0 = 不限）
    max_output_width: int = 0
    max_output_height: int = 0
    min_input_side: int = 1

    def __post_init__(self):
        if not self.id or not str(self.id).strip():
            raise ValueError("UpscaleEngineInfo.id 不能为空")
        keys = [p.key for p in self.params]
        dup = {k for k in keys if keys.count(k) > 1}
        if dup:
            raise ValueError(f"引擎 {self.id} 参数 key 重复: {sorted(dup)}")

    # ── 查询 ──

    def param(self, key: str) -> Optional[ParamSpec]:
        for p in self.params:
            if p.key == key:
                return p
        return None

    def defaults(self) -> dict[str, Any]:
        return {p.key: p.default for p in self.params}

    def groups(self, *, advanced: bool = False) -> list[tuple[str, tuple[ParamSpec, ...]]]:
        """按声明顺序返回分组：[(组名, (参数…)), …]。

        :param advanced: False → 只取常规参数；True → 只取高级参数
        """
        out: list[tuple[str, list[ParamSpec]]] = []
        index: dict[str, int] = {}
        for p in self.params:
            if bool(p.advanced) != bool(advanced):
                continue
            name = p.group or "参数"
            if name not in index:
                index[name] = len(out)
                out.append((name, []))
            out[index[name]][1].append(p)
        return [(name, tuple(items)) for name, items in out]

    def rows(self, group_name: str, *, advanced: bool = False) -> list[tuple[ParamSpec, ...]]:
        """某分组内按 ``row`` 聚合的横排行（宽屏布局：同行放多组标签+控件）。"""
        specs = [p for p in self.params if bool(p.advanced) == bool(advanced)
                 and (p.group or "参数") == group_name]
        buckets: dict[int, list[ParamSpec]] = {}
        for p in specs:
            buckets.setdefault(int(p.row), []).append(p)
        return [tuple(buckets[k]) for k in sorted(buckets)]


# ══════════════════════════════════════════════════════════════
#  DLSS 5 —— 参数取值与 DLSS 5 原生运行时（feature-18 / NGX 协议）严格对齐
#  （NR_PRESETS / NR_STYLES / DLSS_MODEL_PRESETS / UPSCALING_MODES）
# ══════════════════════════════════════════════════════════════

# 倍率 → (显示文本, DLSS perf_quality 值, 模式名)
DLSS5_SCALE_MODES: tuple[tuple[str, float, int, str], ...] = (
    ("performance", 2.0, 0, "2× Performance"),
    ("quality", 1.5, 2, "1.5× Quality"),
    ("balanced", 1.724, 1, "1.724× Balanced"),
    ("ultra_performance", 3.0, 3, "3× Ultra Performance"),
    ("dlaa", 1.0, 5, "1× DLAA（仅增强不放大）"),
)

DLSS5_SCALE_FACTOR: dict[str, float] = {m[0]: m[1] for m in DLSS5_SCALE_MODES}
DLSS5_PERF_QUALITY: dict[str, int] = {m[0]: m[2] for m in DLSS5_SCALE_MODES}
DLSS5_MODE_NAME: dict[str, str] = {m[0]: m[3] for m in DLSS5_SCALE_MODES}

# 神经渲染控制（值即传给原生 worker 的整数）
DLSS5_NR_PRESETS: tuple[tuple[str, int], ...] = (
    ("Default", 0), ("Preset #1", 1), ("Preset #2", 2), ("Preset #3", 3),
)
DLSS5_NR_STYLES: tuple[tuple[str, int], ...] = (
    ("Default", 0), ("Natural", 1), ("Cinematic", 2),
)
DLSS5_MODEL_PRESETS: tuple[tuple[str, int], ...] = (
    ("Default", 0), ("J", 10), ("K", 11), ("L", 12), ("M", 13),
)

_DLSS5_SCALE_TOOLTIP = (
    "DLSS 原生档位，倍率与模式严格配对（送入 DLSS 的渲染尺寸 = 原图尺寸，1:1 无预缩放损失）。\n"
    "· 2× Performance：最常用，画质/倍率平衡最好\n"
    "· 1.5× Quality / 1.724× Balanced：倍率小、更贴近原图\n"
    "· 3× Ultra Performance：倍率最大，但为 DLSS 最低画质档；想要 4×/8× 请用下方「多趟放大」\n"
    "· 1× DLAA：不改变尺寸，只做 DLSS 5 神经渲染增强"
)

_DLSS5_MODEL_PRESET_TOOLTIP = (
    "强制指定 DLSS 超分模型预设（独立于上方 NR Preset）。\n"
    "· Default：不覆盖，由 NVIDIA 按模式自行选择（推荐）\n"
    "· J / K：DLSS 4 第一代 Transformer 预设\n"
    "· L / M：DLSS 4.5 第二代 Transformer 预设；静态图想手动挑可先试 L，"
    "若出现过度锐化/边缘振铃再回退 K"
)

DLSS5_ENGINE = UpscaleEngineInfo(
    id="dlss5",
    name="DLSS 5 神经渲染",
    icon="🎯",
    description=(
        "NVIDIA DLSS 5 Neural Rendering（feature-18）+ DLSS 超分。"
        "需 RTX 40 系及以上显卡、64 位 Windows 与外挂运行时。"
    ),
    platforms=frozenset({"win32"}),
    min_gpu_generation=40,
    gpu_vendor="nvidia",
    runtime_kind="external_bundle",
    max_output_width=7680,
    max_output_height=4320,
    min_input_side=64,
    notes=(
        "DLSS 5 是生成式神经渲染（单步像素空间扩散模型），会依据学习到的真实世界外观先验"
        "重构画面，而非像素级忠实还原。人像/自然场景收益明显；"
        "线稿、文字截图、纯色电商图建议先把「NR 强度」调低再批量。"
    ),
    license_note=(
        "本引擎依赖 NVIDIA 专有运行时（nvngx_dlssnr.dll / nvngx_dlss.dll，受 NVIDIA RTX SDK "
        "License 约束，禁止再分发）、ReShade（BSD-3-Clause）与 RenoDX DLSS5 add-on。"
        "运行时包由维护者上传至本项目 Releases 供下载，但不随源码仓库与安装包分发；"
        "本软件与 NVIDIA、ReShade、RenoDX 均无关联、未获背书。"
    ),
    download_page=config.APP_RELEASES_URL,
    params=(
        # ── 放大倍率 ──
        ParamSpec(
            key="scale_mode", label="放大档位", kind=KIND_COMBO,
            default="performance", group="放大倍率", row=0,
            choices=tuple((m[3], m[0]) for m in DLSS5_SCALE_MODES),
            tooltip=_DLSS5_SCALE_TOOLTIP, min_width=190,
        ),
        ParamSpec(
            key="use_multi_pass", label="多趟放大", kind=KIND_BOOL, default=False,
            group="放大倍率", row=0,
            tooltip=(
                "勾选后按「目标倍数」自动拆成多趟 2× 放大（如 4× = 2× 跑两趟，8× = 三趟）。\n"
                "每趟送入 DLSS 的渲染尺寸都与输入 1:1，比单次 3× Ultra Performance 更保细节。\n"
                "受 7680×4320 输出上限约束，超出时自动停在最后一趟并提示。"
            ),
        ),
        ParamSpec(
            key="target_scale", label="目标倍数", kind=KIND_FLOAT, default=4.0,
            group="放大倍率", row=0, lo=1.0, hi=8.0, step=0.5, decimals=1,
            suffix="×", tooltip="多趟放大的最终目标倍数（1~8，步进 0.5）", min_width=90,
        ),
        # ── 神经渲染 ──
        ParamSpec(
            key="nr_style", label="NR 风格", kind=KIND_COMBO, default="Default",
            group="神经渲染 (DLSS 5 NR)", row=0, choices=DLSS5_NR_STYLES,
            tooltip="神经渲染的艺术方向：Default 跟随原生 / Natural 偏保守 / Cinematic 偏影视感",
            min_width=110,
        ),
        ParamSpec(
            key="nr_intensity", label="NR 强度", kind=KIND_FLOAT, default=1.0,
            group="神经渲染 (DLSS 5 NR)", row=0, lo=0.0, hi=2.0, step=0.05, decimals=2,
            tooltip=(
                "神经渲染介入强度 0~2（上游默认 1.00）。\n"
                "0 = 只做 DLSS 超分、几乎不改观感；越高生成感越强。\n"
                "静态图没有运动矢量与时序信息，过高易涂抹细节或产生幻觉纹理。"
            ),
            min_width=90,
        ),
        ParamSpec(
            key="nr_preset", label="NR Preset", kind=KIND_COMBO, default="Default",
            group="神经渲染 (DLSS 5 NR)", row=0, choices=DLSS5_NR_PRESETS,
            tooltip="实验性的原生模型提示，视觉效果依图片内容而定；不确定就保持 Default",
            min_width=110,
        ),
        ParamSpec(
            key="automatic_mask", label="Automatic Mask", kind=KIND_BOOL, default=False,
            group="神经渲染 (DLSS 5 NR)", row=1,
            tooltip="上游标注为实验性开关，默认关闭",
        ),
        # ── 细节与色调 ──
        ParamSpec(
            key="local_structure_strength", label="局部结构", kind=KIND_FLOAT, default=1.0,
            group="细节与色调", row=0, lo=0.0, hi=2.0, step=0.05, decimals=2,
            tooltip="局部结构（纹理/边缘细节）增强强度 0~2", min_width=90,
        ),
        ParamSpec(
            key="local_tone_strength", label="局部色调", kind=KIND_FLOAT, default=1.0,
            group="细节与色调", row=0, lo=0.0, hi=2.0, step=0.05, decimals=2,
            tooltip="局部色调（明暗/对比）调整强度 0~2；批量图想保持一致观感可适当调低",
            min_width=90,
        ),
        ParamSpec(
            key="skin_structure_strength", label="皮肤结构", kind=KIND_FLOAT, default=-1.0,
            group="细节与色调", row=0, lo=-1.0, hi=2.0, step=0.05, decimals=2,
            tooltip="皮肤结构强度 -1~2；-1 = 跟随原生默认。人像图可试 0~1.2", min_width=90,
        ),
        # ── DLSS 模型预设 ──
        ParamSpec(
            key="dlss_model_preset", label="DLSS 模型预设", kind=KIND_COMBO, default="Default",
            group="DLSS 模型预设", row=0, choices=DLSS5_MODEL_PRESETS,
            tooltip=_DLSS5_MODEL_PRESET_TOOLTIP, min_width=110,
        ),
        # ── 高级 ──
        ParamSpec(
            key="warmup_frames", label="预热帧数", kind=KIND_INT, default=0,
            group="高级", row=0, lo=0, hi=8, step=1, advanced=True,
            tooltip="会话建立后的预热帧数；静态图片路径上游默认为 0，一般无需修改",
            min_width=80,
        ),
        ParamSpec(
            key="protect_transparent", label="透明区保护", kind=KIND_BOOL, default=False,
            group="高级", row=0, advanced=True,
            tooltip=(
                "DLSS 不处理 Alpha 通道，全透明区域的 RGB 可能是无意义噪色。\n"
                "勾选后把 Alpha=0 区域的 RGB 归零，避免输出 JPG/WEBP 等有损格式时边缘出现脏色。"
            ),
        ),
        ParamSpec(
            key="ai_gpu", label="AI 处理显卡", kind=KIND_COMBO, default="auto",
            group="高级", row=1, advanced=True, editable=True,
            choices=(("自动选择", "auto"),),
            tooltip="多显卡时可指定用于 DLSS 推理的设备；留「自动选择」即可",
            min_width=170,
        ),
    ),
)


# ── 注册表（菜单/面板顺序以此为准）──
UPSCALE_ENGINES: dict[str, UpscaleEngineInfo] = {
    DLSS5_ENGINE.id: DLSS5_ENGINE,
}

DEFAULT_ENGINE_ID = DLSS5_ENGINE.id

# 输出格式（交由 PixelFlow 现有 Worker 保存逻辑处理）
OUTPUT_FORMATS: tuple[tuple[str, str], ...] = (
    ("PNG（无损，推荐）", "png"),
    ("JPG", "jpg"),
    ("WEBP", "webp"),
)
DEFAULT_OUTPUT_FORMAT = "png"


def list_engines() -> list[UpscaleEngineInfo]:
    return list(UPSCALE_ENGINES.values())


def engine_ids() -> list[str]:
    return list(UPSCALE_ENGINES.keys())


def get_engine(engine_id: str | None) -> Optional[UpscaleEngineInfo]:
    if not engine_id:
        return None
    return UPSCALE_ENGINES.get(str(engine_id))


def resolve_engine_id(raw: Any) -> str:
    """把任意输入收敛为合法引擎 id（非法则回默认）。"""
    eng = get_engine(raw)
    return eng.id if eng is not None else DEFAULT_ENGINE_ID


def engine_defaults(engine_id: str | None = None) -> dict[str, Any]:
    eng = get_engine(engine_id) or get_engine(DEFAULT_ENGINE_ID)
    return eng.defaults() if eng is not None else {}


def coerce_params(
    engine: UpscaleEngineInfo,
    raw: dict | None,
    *,
    strict: bool = True,
) -> tuple[dict[str, Any], list[str]]:
    """按引擎 schema 规范化参数字典。

    :return: (规范化后的参数, 错误列表)；strict=False 时错误列表恒为空、非法值静默收敛
    """
    data = dict(raw or {})
    errors: list[str] = []
    out: dict[str, Any] = {}
    for spec in engine.params:
        present = spec.key in data
        value = data.get(spec.key, spec.default)
        if present and strict:
            if spec.kind == KIND_COMBO and value not in spec.choice_values:
                # 允许显示文本
                if not any(str(value) == text for text, _v in spec.choices):
                    errors.append(f"{spec.label} 取值无效: {value!r}")
            elif spec.kind == KIND_BOOL and not isinstance(value, bool):
                errors.append(f"{spec.label} 应为布尔值")
            elif spec.kind in (KIND_FLOAT, KIND_INT):
                try:
                    n = float(value)
                except (TypeError, ValueError):
                    errors.append(f"{spec.label} 应为数字")
                    n = float(spec.default)
                if not (float(spec.lo) <= n <= float(spec.hi)):
                    errors.append(
                        f"{spec.label} 应在 {spec.lo:g}~{spec.hi:g} 之间"
                    )
        out[spec.key] = spec.clamp(value)
    return out, errors


def all_engines_defaults() -> dict[str, dict[str, Any]]:
    """所有引擎的默认参数（预设按引擎命名空间存放，切换引擎不丢参数）。"""
    return {eid: eng.defaults() for eid, eng in UPSCALE_ENGINES.items()}


__all__ = [
    "ParamSpec",
    "UpscaleEngineInfo",
    "KIND_COMBO", "KIND_FLOAT", "KIND_INT", "KIND_BOOL",
    "UPSCALE_ENGINES",
    "DEFAULT_ENGINE_ID",
    "OUTPUT_FORMATS",
    "DEFAULT_OUTPUT_FORMAT",
    "DLSS5_ENGINE",
    "DLSS5_SCALE_MODES",
    "DLSS5_SCALE_FACTOR",
    "DLSS5_PERF_QUALITY",
    "DLSS5_MODE_NAME",
    "DLSS5_NR_PRESETS",
    "DLSS5_NR_STYLES",
    "DLSS5_MODEL_PRESETS",
    "list_engines",
    "engine_ids",
    "get_engine",
    "resolve_engine_id",
    "engine_defaults",
    "coerce_params",
    "all_engines_defaults",
]
