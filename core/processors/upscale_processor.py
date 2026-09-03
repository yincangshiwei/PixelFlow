"""高清放大处理器 —— 按引擎分派到具体放大后端（当前：DLSS 5）。

纯处理，无 UI；参数校验/规范化在 ``services.features.upscale_service.UpscaleService``，
参数面板在 ``ui.routes.process.features.upscale_route.UpscaleFeatureRoute``。

与 AI 抠图一致的隔离原则：主程序不内嵌任何 DLSS / ReShade / RenoDX 二进制，
运行时由用户在「配置 → 高清放大引擎」下载或指定目录。
"""
from __future__ import annotations

from PIL import Image, ImageCms, ImageOps

from core.base_processor import BaseProcessor
from core.upscale.engine_registry import (
    DEFAULT_ENGINE_ID,
    DEFAULT_OUTPUT_FORMAT,
    all_engines_defaults,
    get_engine,
)


def default_upscale_options() -> dict:
    """出厂默认参数（UpscaleService / 预设与此保持一致）。

    结构：``engine`` 指定当前引擎，``engines`` 按引擎命名空间存放各自参数，
    切换引擎时不会丢失另一套参数。
    """
    return {
        "engine": DEFAULT_ENGINE_ID,
        "engines": all_engines_defaults(),
        "output_format": DEFAULT_OUTPUT_FORMAT,
        "quality": 95,
    }


_SRGB_PROFILE = None


def _srgb_profile():
    global _SRGB_PROFILE
    if _SRGB_PROFILE is None:
        try:
            _SRGB_PROFILE = ImageCms.createProfile("sRGB")
        except Exception:
            _SRGB_PROFILE = False
    return _SRGB_PROFILE or None


def _to_srgb(img: Image.Image, details: dict) -> Image.Image:
    """带 ICC 配置文件的图片统一转到 sRGB。

    放大结果通常另存为 PNG/JPG，不会携带原 ICC；不转换会导致
    AdobeRGB / Display P3 等广色域图输出后颜色发灰或过饱和。
    """
    profile = getattr(img, "info", {}).get("icc_profile")
    if not profile:
        return img
    srgb = _srgb_profile()
    if srgb is None:
        return img
    try:
        src = ImageCms.ImageCmsProfile(ImageCms.core.profile_frombytes(profile))
        if ImageCms.getProfileName(src).strip().lower().startswith("srgb"):
            return img
        target = ImageCms.ImageCmsProfile(srgb)
        mode = img.mode if img.mode in ("RGB", "RGBA") else "RGB"
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert(mode)
        out = ImageCms.profileToProfile(
            img, src, target, outputMode=mode, renderingIntent=ImageCms.Intent.PERCEPTUAL,
        )
        details["icc_converted"] = "已转换到 sRGB"
        return out
    except Exception as e:
        details["icc_error"] = f"ICC 转换失败（已按原样处理）: {e}"
        return img


class UpscaleProcessor(BaseProcessor):
    """高清放大：按所选引擎放大图片（当前仅 DLSS 5，需 RTX 40 系及以上）。"""

    name = "高清放大"
    description = "AI 高清放大（DLSS 5 神经渲染 + 超分），支持多趟放大至 8×"
    icon = "🔍"
    preset_id = "upscale"

    def default_options(self) -> dict:
        return default_upscale_options()

    def process(self, img: Image.Image, options: dict) -> tuple[Image.Image, dict]:
        options = dict(options or {})
        engine_id = str(options.get("engine") or DEFAULT_ENGINE_ID)
        engine = get_engine(engine_id)
        if engine is None:
            raise ValueError(
                f"未知的放大引擎: {engine_id!r}。请在参数面板重新选择引擎。"
            )

        details: dict = {
            "original_size": img.size,
            "original_mode": img.mode,
            "engine": engine_id,
            "engine_name": engine.name,
        }

        # EXIF 方向：放大结果一般另存为 PNG/JPG，不会带 EXIF，
        # 不先转正会导致竖拍照片输出后横躺
        try:
            fixed = ImageOps.exif_transpose(img)
            if fixed is not None and fixed.size != img.size:
                details["exif_orientation"] = "已按 EXIF 方向转正"
                img = fixed
            elif fixed is not None:
                img = fixed
        except Exception:
            pass

        img = _to_srgb(img, details)

        if engine_id == "dlss5":
            from core.upscale.dlss5 import upscale_image
            out, extra = upscale_image(img, options)
        else:
            raise ValueError(f"引擎 {engine_id!r} 尚未实现后端")

        details.update(extra)

        # 输出格式色彩模式适配：JPG 无 Alpha，按项目既有约定白底合并
        fmt = str(options.get("output_format", DEFAULT_OUTPUT_FORMAT) or "png").lower().strip()
        if fmt in ("jpg", "jpeg") and out.mode in ("RGBA", "LA", "P"):
            rgba = out.convert("RGBA")
            background = Image.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.split()[-1])
            out = background
            details["mode_converted"] = "RGBA→RGB (白底合并)"

        details["output_size"] = f"{out.size[0]}×{out.size[1]}"
        return out, details
