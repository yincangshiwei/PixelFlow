"""DLSS5 引擎实现 —— 尺寸规划、多趟放大、会话池、Alpha 保留。

只用 Pillow + 标准库：像素走 ``Image.tobytes()/frombytes()``，
运动矢量是全零字节串，因此**不需要 numpy / opencv**。

相对社区已有实现（DLSS5 便携包）的优化点：
1. **多趟放大**：参考实现最高只有 3×；本模块把目标倍数拆成多趟，每趟送入 DLSS 的
   渲染尺寸都与该趟输入 1:1（无预缩放损失），4× = 2× 跑两趟、8× = 三趟。
2. **会话池**：同尺寸批量图复用一个原生 worker 进程，避免每张图重建 D3D12/NGX。
3. **门禁前置**：显卡架构、运行时完整性、尺寸上限都在渲染前判定并给出可读原因。
4. **feature-18 校验**：渲染后必须验证 ReShade 日志中的三条证据，
   否则报错——避免 DLSS 未生效时静默退化成普通缩放。
"""
from __future__ import annotations

import itertools
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from PIL import Image

from core.upscale import upscale_settings
from core.upscale.dlss5_session import (
    DLSS5Cancelled,
    DLSS5Error,
    DLSS5Session,
    NativeSettings,
)
from core.upscale.engine_registry import (
    DLSS5_MODE_NAME,
    DLSS5_PERF_QUALITY,
    DLSS5_SCALE_FACTOR,
    DLSS5_MODEL_PRESETS,
    DLSS5_NR_PRESETS,
    DLSS5_NR_STYLES,
    get_engine,
)
from core.upscale.hardware_gate import check_engine
from core.upscale.runtime_bundle import BundleStatus, inspect_bundle

ENGINE_ID = "dlss5"

# 输出边界（上游 resolve_output_size 同规则：长边 ≤7680、短边 ≤4320，偶数对齐）
MAX_OUTPUT_LONG = 7680
MAX_OUTPUT_SHORT = 4320
MIN_INPUT_SIDE = 64

# 多趟放大可用的档位（DLAA 1× 不参与，它不改变尺寸）
MULTI_PASS_MODES: tuple[tuple[str, float], ...] = (
    ("quality", 1.5),
    ("balanced", 1.724),
    ("performance", 2.0),
    ("ultra_performance", 3.0),
)
MAX_PASSES = 4
MAX_TOTAL_SCALE = 8.0

# 会话池
SESSION_FRAME_BUDGET = 256          # 单个会话声明的帧预算，用满即重建
SESSION_IDLE_TTL = 180.0            # 空闲多久后回收（秒）


# ══════════════════════════════════════════════════════════════
#  尺寸与趟数规划（纯函数，可单测）
# ══════════════════════════════════════════════════════════════

def nearest_even(value: float) -> int:
    """与上游 ``_nearest_even`` 一致：四舍五入到偶数，最小 2。"""
    return max(2, int(math.floor(float(value) / 2.0 + 0.5)) * 2)


def exceeds_boundary(width: int, height: int) -> bool:
    """长边 >7680 或短边 >4320 即越界（与上游一致）。"""
    return max(width, height) > MAX_OUTPUT_LONG or min(width, height) > MAX_OUTPUT_SHORT


def output_size_for(width: int, height: int, mode: str) -> tuple[int, int]:
    """单趟放大后的输出尺寸（偶数对齐）。"""
    factor = DLSS5_SCALE_FACTOR.get(mode)
    if factor is None:
        raise DLSS5Error(f"未知的放大档位: {mode!r}")
    return nearest_even(width * factor), nearest_even(height * factor)


def plan_passes(target_scale: float, max_passes: int = MAX_PASSES) -> tuple[tuple[str, ...], float]:
    """把目标倍数拆成若干趟原生档位。

    择优规则：乘积与目标的对数误差最小 → 趟数最少 → 达成倍数最大。
    精确命中（如 4.0 = 2×2）会立即返回最少趟数的方案。

    趟次按倍率**降序**排列，两个原因：
    1. 触到 7680×4320 上限被截断时，先跑大倍率能保住更大的达成倍数
       （1080p 请求 6×：降序 3×→2× 截断后仍有 3×；升序 2×→3× 只剩 2×）
    2. 最后一趟是画质更好的 Performance(2×) 而非 Ultra Performance(3×)，
       最终观感由末趟决定

    :return: (档位 key 元组, 实际达成倍数)
    """
    try:
        target = float(target_scale)
    except (TypeError, ValueError):
        target = 1.0
    if not math.isfinite(target) or target <= 1.0 + 1e-6:
        return ("dlaa",), 1.0
    target = min(target, MAX_TOTAL_SCALE)

    best: Optional[tuple[tuple[int, float, tuple[str, ...]], float]] = None
    for n in range(1, max(1, int(max_passes)) + 1):
        local_best = None
        for combo in itertools.product(MULTI_PASS_MODES, repeat=n):
            product = 1.0
            for _mode, factor in combo:
                product *= factor
            if product > MAX_TOTAL_SCALE + 1e-6:
                continue
            err = abs(math.log(product / target))
            key = (round(err, 9), -product)
            if local_best is None or key < local_best[0]:
                local_best = (key, product, tuple(m for m, _f in combo))
        if local_best is None:
            continue
        candidate = (local_best[0][0], float(n), local_best[2]), local_best[1]
        if best is None or candidate[0] < best[0]:
            best = candidate
        # 已精确命中且是当前最少趟数 → 无需再搜更长的组合
        if local_best[0][0] < 1e-9:
            break

    if best is None:
        return ("performance",), 2.0
    ordered = tuple(
        sorted(best[0][2], key=lambda m: DLSS5_SCALE_FACTOR.get(m, 1.0), reverse=True)
    )
    return ordered, best[1]


def apply_output_cap(
    width: int,
    height: int,
    modes: tuple[str, ...] | list[str],
) -> tuple[list[str], int, int, bool]:
    """按 7680×4320 上限逐趟推进，越界的那一趟及之后全部丢弃。

    :return: (实际执行的档位列表, 最终宽, 最终高, 是否被上限截断)
    """
    executed: list[str] = []
    cw, ch = nearest_even(width), nearest_even(height)
    truncated = False
    for mode in modes:
        nw, nh = output_size_for(cw, ch, mode)
        if exceeds_boundary(nw, nh):
            truncated = True
            break
        executed.append(mode)
        cw, ch = nw, nh
    return executed, cw, ch, truncated


def resolve_plan(
    width: int,
    height: int,
    params: dict,
) -> tuple[list[str], int, int, bool, float]:
    """综合档位/多趟设置，得出执行计划。

    :return: (档位列表, 输出宽, 输出高, 是否被上限截断, 达成倍数)
    """
    if width < MIN_INPUT_SIDE or height < MIN_INPUT_SIDE:
        raise DLSS5Error(
            f"图片尺寸 {width}×{height} 过小，DLSS 要求宽高都至少 "
            f"{MIN_INPUT_SIDE} 像素。"
        )
    use_multi = bool(params.get("use_multi_pass"))
    if use_multi:
        modes, achieved = plan_passes(float(params.get("target_scale", 4.0)))
    else:
        mode = str(params.get("scale_mode") or "performance")
        if mode not in DLSS5_SCALE_FACTOR:
            mode = "performance"
        modes, achieved = (mode,), DLSS5_SCALE_FACTOR[mode]

    executed, out_w, out_h, truncated = apply_output_cap(width, height, modes)
    if not executed:
        raise DLSS5Error(
            f"图片尺寸 {width}×{height} 放大 {achieved:g}× 后为 "
            f"{output_size_for(width, height, modes[0])[0]}×"
            f"{output_size_for(width, height, modes[0])[1]}，"
            f"超出 DLSS 支持的 {MAX_OUTPUT_LONG}×{MAX_OUTPUT_SHORT} 上限。"
            "请降低放大档位或关闭多趟放大。"
        )
    if truncated:
        achieved = round(out_w / max(1, width), 4)
    return executed, out_w, out_h, truncated, achieved


# ══════════════════════════════════════════════════════════════
#  参数 → 原生协议
# ══════════════════════════════════════════════════════════════

def _lookup(pairs: tuple[tuple[str, int], ...], key: Any, default: int = 0) -> int:
    text = str(key if key is not None else "")
    for label, value in pairs:
        if text == label or text == str(value):
            return int(value)
    return default


def native_settings_from_params(params: dict, mode: str) -> NativeSettings:
    """把面板参数翻译成原生 worker 协议值。"""
    return NativeSettings(
        perf_quality=int(DLSS5_PERF_QUALITY.get(mode, 0)),
        dlss_model_preset=_lookup(
            DLSS5_MODEL_PRESETS, params.get("dlss_model_preset"), 0
        ),
        profile=0,
        preset=_lookup(DLSS5_NR_PRESETS, params.get("nr_preset"), 0),
        style=_lookup(DLSS5_NR_STYLES, params.get("nr_style"), 0),
        auto_mask=1 if params.get("automatic_mask") else 0,
        ui_correction=0,
        intensity=float(params.get("nr_intensity", 1.0)),
        local_tone=float(params.get("local_tone_strength", 1.0)),
        local_structure=float(params.get("local_structure_strength", 1.0)),
        skin_structure=float(params.get("skin_structure_strength", -1.0)),
        warmup_frames=max(0, int(params.get("warmup_frames", 0) or 0)),
    )


def _settings_key(s: NativeSettings) -> tuple:
    return (
        s.perf_quality, s.dlss_model_preset, s.profile, s.preset, s.style,
        s.auto_mask, s.ui_correction, round(s.intensity, 4), round(s.local_tone, 4),
        round(s.local_structure, 4), round(s.skin_structure, 4), s.warmup_frames,
    )


# ══════════════════════════════════════════════════════════════
#  会话池
# ══════════════════════════════════════════════════════════════

@dataclass
class _PoolEntry:
    session: DLSS5Session
    key: tuple
    sent: int = 0
    last_used: float = 0.0


_pool: dict[tuple, _PoolEntry] = {}
_pool_lock = threading.RLock()

# 进程级取消标记：ProcessWorker.cancel() 由 GUI 线程调用，
# 置位后正在进行的单张渲染会在下一帧边界中断（大图单趟渲染无法中途打断）
_cancel_event = threading.Event()


def request_cancel() -> None:
    """请求取消当前 DLSS5 渲染（GUI 线程调用，线程安全）。"""
    _cancel_event.set()


def clear_cancel() -> None:
    _cancel_event.clear()


def get_cancel_event() -> threading.Event:
    return _cancel_event


def shutdown_upscale_sessions() -> None:
    """关闭所有常驻 DLSS5 会话（批处理结束 / 取消 / 退出时调用）。"""
    with _pool_lock:
        entries = list(_pool.values())
        _pool.clear()
    for entry in entries:
        try:
            entry.session.abort()
        except Exception:
            pass


def _evict_expired(now: float) -> None:
    """回收空闲超时的会话（调用方需持有 _pool_lock）。"""
    stale = [k for k, v in _pool.items() if (now - v.last_used) > SESSION_IDLE_TTL]
    for key in stale:
        entry = _pool.pop(key, None)
        if entry is not None:
            try:
                entry.session.close()
            except Exception:
                try:
                    entry.session.abort()
                except Exception:
                    pass


def _acquire_session(
    *,
    bundle: BundleStatus,
    in_w: int,
    in_h: int,
    out_w: int,
    out_h: int,
    settings: NativeSettings,
    cancel_event: Optional[threading.Event],
) -> _PoolEntry:
    """取一个可复用的会话；不存在或已用满则新建。"""
    worker = bundle.worker_path
    host = bundle.host_dir
    if worker is None or host is None:
        raise DLSS5Error("DLSS5 运行时目录不完整，无法定位渲染入口")

    key = (str(worker), in_w, in_h, out_w, out_h, _settings_key(settings))
    now = time.monotonic()
    with _pool_lock:
        _evict_expired(now)
        entry = _pool.get(key)
        if entry is not None and entry.session.alive and entry.sent < SESSION_FRAME_BUDGET:
            entry.last_used = now
            return entry
        if entry is not None:
            _pool.pop(key, None)
            try:
                entry.session.close()
            except Exception:
                try:
                    entry.session.abort()
                except Exception:
                    pass

        session = DLSS5Session(
            worker_path=worker,
            host_dir=host,
            reshade_log=bundle.reshade_log_path,
            input_width=in_w,
            input_height=in_h,
            output_width=out_w,
            output_height=out_h,
            frame_count=SESSION_FRAME_BUDGET,
            settings=settings,
            cancel_event=cancel_event,
        )
        try:
            session.start()
        except Exception:
            session.abort()
            raise
        entry = _PoolEntry(session=session, key=key, sent=0, last_used=now)
        _pool[key] = entry
        return entry


def _release_session(entry: _PoolEntry, *, ok: bool) -> None:
    with _pool_lock:
        if ok:
            entry.sent += 1
            entry.last_used = time.monotonic()
        else:
            existing = _pool.get(entry.key)
            if existing is entry:
                _pool.pop(entry.key, None)
            try:
                entry.session.abort()
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════
#  像素处理
# ══════════════════════════════════════════════════════════════

def _to_rgba(img: Image.Image) -> Image.Image:
    if img.mode == "RGBA":
        return img
    if img.mode in ("RGB", "L", "LA", "P", "CMYK", "I", "F"):
        return img.convert("RGBA")
    return img.convert("RGBA")


def _fit_rgba(img: Image.Image, width: int, height: int) -> Image.Image:
    """与上游 ``resize_fit`` 等价：等比 contain + 居中、不透明黑边补齐。

    正常路径下 render 尺寸 == 输入尺寸，此函数是空操作，仅作安全兜底。
    """
    if img.size == (width, height):
        return img
    sw, sh = img.size
    scale = min(width / max(1, sw), height / max(1, sh))
    fw = max(1, min(width, int(round(sw * scale))))
    fh = max(1, min(height, int(round(sh * scale))))
    resized = img.resize((fw, fh), resample=Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 255))
    canvas.paste(resized, ((width - fw) // 2, (height - fh) // 2))
    return canvas


def _protect_transparent(img: Image.Image) -> Image.Image:
    """把 Alpha=0 区域的 RGB 归零，避免有损格式在透明边缘产生脏色。"""
    if img.mode != "RGBA":
        return img
    r, g, b, a = img.split()
    mask = a.point(lambda v: 255 if v == 0 else 0)
    if not mask.getextrema()[1]:
        return img
    black = Image.new("L", img.size, 0)
    return Image.merge(
        "RGBA",
        (Image.composite(black, r, mask),
         Image.composite(black, g, mask),
         Image.composite(black, b, mask),
         a),
    )


def _placeholder_upscale(img: Image.Image, out_w: int, out_h: int) -> Image.Image:
    """开发调试用占位后端：Pillow LANCZOS，不加载 DLSS。"""
    return img.resize((out_w, out_h), resample=Image.Resampling.LANCZOS)


# ══════════════════════════════════════════════════════════════
#  对外主入口
# ══════════════════════════════════════════════════════════════

def resolve_params(options: dict) -> dict:
    """从 options 中取出 DLSS5 参数（兼容命名空间与扁平两种形态）。"""
    engines = options.get("engines")
    if isinstance(engines, dict) and isinstance(engines.get(ENGINE_ID), dict):
        return dict(engines[ENGINE_ID])
    return dict(options or {})


def preflight(options: dict, *, force: bool = False) -> tuple[Any, BundleStatus]:
    """渲染前门禁：返回 (可用性判定, 运行时状态)；不可用时抛 DLSS5Error。"""
    av = check_engine(ENGINE_ID, force=force)
    if not av.usable:
        raise DLSS5Error(
            "DLSS 5 高清放大当前不可用：\n" + "\n".join(av.reasons)
        )
    bundle = av.bundle if av.bundle is not None else inspect_bundle()
    if not bundle.ready:
        raise DLSS5Error(
            "DLSS 5 运行时未就绪：\n" + "\n".join(bundle.problems or [bundle.detail])
        )
    return av, bundle


def upscale_image(
    img: Image.Image,
    options: dict,
    *,
    cancel_event: Optional[threading.Event] = None,
) -> tuple[Image.Image, dict]:
    """对单张图片执行 DLSS5 高清放大。

    :return: (放大后的图片, 详情字典)
    """
    started = time.perf_counter()
    params = resolve_params(options)
    engine = get_engine(ENGINE_ID)
    src_w, src_h = img.size
    # 未显式传入时用进程级取消标记（ProcessWorker.cancel() 会置位）
    ev = cancel_event if cancel_event is not None else _cancel_event

    placeholder = upscale_settings.get_placeholder_backend()
    if placeholder:
        executed, out_w, out_h, truncated, achieved = resolve_plan(src_w, src_h, params)
        rgba = _to_rgba(img)
        alpha = rgba.split()[-1] if rgba.mode == "RGBA" else None
        result = _placeholder_upscale(rgba.convert("RGB"), out_w, out_h)
        if alpha is not None:
            result = result.convert("RGBA")
            result.putalpha(alpha.resize((out_w, out_h), resample=Image.Resampling.LANCZOS))
            if params.get("protect_transparent"):
                result = _protect_transparent(result)
        return result, {
            "engine": ENGINE_ID,
            "backend": "placeholder(LANCZOS)",
            "warning": "开发占位后端：未加载 DLSS，仅用于验证处理链路",
            "passes": [DLSS5_MODE_NAME.get(m, m) for m in executed],
            "pass_count": len(executed),
            "achieved_scale": achieved,
            "input_size": f"{src_w}×{src_h}",
            "output_size": f"{out_w}×{out_h}",
            "truncated_by_limit": truncated,
            "elapsed": round(time.perf_counter() - started, 3),
        }

    av, bundle = preflight(options)

    executed, out_w, out_h, truncated, achieved = resolve_plan(src_w, src_h, params)
    if not executed:
        raise DLSS5Error("没有可执行的放大趟数（可能已超出输出上限）")

    rgba = _to_rgba(img)
    has_alpha = img.mode in ("RGBA", "LA", "PA") or (
        img.mode == "P" and "transparency" in getattr(img, "info", {})
    )
    src_alpha = rgba.split()[-1] if has_alpha else None

    gpu_text = av.gpu.display if av.gpu is not None else "未知 GPU"
    pass_details: list[str] = []
    verified = False
    cursor = rgba
    cur_w, cur_h = src_w, src_h

    for i, mode in enumerate(executed):
        if ev.is_set():
            raise DLSS5Cancelled()
        p_out_w, p_out_h = output_size_for(cur_w, cur_h, mode)
        settings = native_settings_from_params(params, mode)
        entry = _acquire_session(
            bundle=bundle,
            in_w=cur_w, in_h=cur_h,
            out_w=p_out_w, out_h=p_out_h,
            settings=settings,
            cancel_event=ev,
        )
        session = entry.session
        ok = False
        try:
            feed = _fit_rgba(cursor.convert("RGBA"), session.render_width, session.render_height)
            out_bytes = session.process_frame(
                index=entry.sent,
                rgba=feed.tobytes(),
                reset=True,
                pts=entry.sent,
            )
            result = Image.frombytes("RGBA", (p_out_w, p_out_h), out_bytes)
            if i == len(executed) - 1:
                # 只在最后一趟做 feature-18 校验（会话可能被复用，日志证据累积）
                session.verify_feature_18()
                verified = True
            ok = True
        except DLSS5Cancelled:
            _release_session(entry, ok=False)
            raise
        except Exception:
            _release_session(entry, ok=False)
            raise
        finally:
            if ok:
                _release_session(entry, ok=True)

        pass_details.append(
            f"第{i + 1}趟 {DLSS5_MODE_NAME.get(mode, mode)}: "
            f"{cur_w}×{cur_h} → {p_out_w}×{p_out_h}"
        )
        cursor = result
        cur_w, cur_h = p_out_w, p_out_h

    out_img = cursor
    if out_img.size != (out_w, out_h):
        out_img = out_img.resize((out_w, out_h), resample=Image.Resampling.LANCZOS)

    # Alpha：DLSS 不处理透明通道，用原图 Alpha 放大后覆盖（与上游一致）
    if src_alpha is not None:
        if out_img.mode != "RGBA":
            out_img = out_img.convert("RGBA")
        out_img.putalpha(
            src_alpha.resize((out_w, out_h), resample=Image.Resampling.LANCZOS)
        )
        if params.get("protect_transparent"):
            out_img = _protect_transparent(out_img)
    elif out_img.mode == "RGBA":
        out_img = out_img.convert("RGB")

    detail = {
        "engine": ENGINE_ID,
        "backend": "native(feature-18)",
        "passes": [DLSS5_MODE_NAME.get(m, m) for m in executed],
        "pass_count": len(executed),
        "requested_scale": (
            float(params.get("target_scale", 0)) if params.get("use_multi_pass")
            else DLSS5_SCALE_FACTOR.get(str(params.get("scale_mode")), 1.0)
        ),
        "achieved_scale": achieved,
        "input_size": f"{src_w}×{src_h}",
        "output_size": f"{out_w}×{out_h}",
        "truncated_by_limit": truncated,
        "pass_detail": " | ".join(pass_details),
        "gpu": gpu_text,
        "nr": (
            f"style={params.get('nr_style')} intensity={params.get('nr_intensity')} "
            f"preset={params.get('nr_preset')} tone={params.get('local_tone_strength')} "
            f"structure={params.get('local_structure_strength')} "
            f"skin={params.get('skin_structure_strength')}"
        ),
        "dlss_model_preset": str(params.get("dlss_model_preset")),
        "feature_18_verified": verified,
        "alpha_preserved": bool(src_alpha is not None),
        "elapsed": round(time.perf_counter() - started, 3),
    }
    if engine is not None and engine.notes:
        detail["engine_note"] = engine.notes
    return out_img, detail


def describe_plan_text(width: int, height: int, params: dict) -> str:
    """UI 实时提示用：给定原图尺寸与参数，返回预计输出与趟数说明。"""
    try:
        executed, out_w, out_h, truncated, achieved = resolve_plan(width, height, params)
    except DLSS5Error as e:
        return f"⚠ {e}"
    bits = [
        f"{width}×{height} → {out_w}×{out_h}",
        f"实际 {achieved:g}×",
        f"{len(executed)} 趟：" + " + ".join(DLSS5_MODE_NAME.get(m, m) for m in executed),
    ]
    if truncated:
        bits.append(
            f"⚠ 已按 {MAX_OUTPUT_LONG}×{MAX_OUTPUT_SHORT} 上限截断趟数"
        )
    return "  ·  ".join(bits)


__all__ = [
    "ENGINE_ID",
    "MAX_OUTPUT_LONG", "MAX_OUTPUT_SHORT", "MIN_INPUT_SIDE",
    "MULTI_PASS_MODES", "MAX_PASSES", "MAX_TOTAL_SCALE",
    "SESSION_FRAME_BUDGET", "SESSION_IDLE_TTL",
    "DLSS5Error", "DLSS5Cancelled",
    "nearest_even",
    "exceeds_boundary",
    "output_size_for",
    "plan_passes",
    "apply_output_cap",
    "resolve_plan",
    "native_settings_from_params",
    "resolve_params",
    "preflight",
    "upscale_image",
    "describe_plan_text",
    "shutdown_upscale_sessions",
    "request_cancel",
    "clear_cancel",
    "get_cancel_event",
]
