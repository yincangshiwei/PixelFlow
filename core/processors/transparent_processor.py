"""
透明图处理器 —— AI 抠图 + 裁剪透明边缘 + 画布主体布局（纯处理，无 UI）。

P4：UI 已迁至 ui.routes.process.features.transparent_route.TransparentFeatureRoute；
参数校验在 services.features.transparent_service.TransparentService。
"""
from __future__ import annotations

from PIL import Image

from core.base_processor import BaseProcessor
from core.image_processor import (
    trim_transparent,
    place_subject_on_canvas,
)
from core.matting.inference import (
    remove_background,
    remove_background_batch,
    recommend_matting_batch_size,
    ensure_matting_session_ready,
    MattingError,
)


def default_transparent_options() -> dict:
    return {
        "enable_matting": False,
        "matting_model": "ben2",
        "matting_refine": False,
        "keep_matting": False,
        "enable_trim": True,
        "alpha_threshold": 0,
        "enable_layout": False,
        "canvas_w": 1500,
        "canvas_h": 1500,
        "canvas_color": "#FFFFFF",
        "subject_percent": 80,
        "detail_restore": "normal",
        "output_format": "png",
    }


class TransparentImageProcessor(BaseProcessor):
    """透明图处理（纯处理，无 UI）。"""

    name = "透明图处理"
    description = "AI 抠图 → 去除透明边缘 → 按画布占比等比放置主体"
    icon = "✂"
    preset_id = "transparent_image"

    def default_options(self) -> dict:
        return default_transparent_options()

    def preferred_matting_batch_size(self, options: dict) -> int:
        """
        ProcessWorker micro-batch 大小。
        未开 AI 抠图 / 开边缘精炼 → 1；否则按 GPU 显存自适应 1~3。
        """
        if not options.get("enable_matting"):
            return 1
        if options.get("matting_refine"):
            return 1
        mid = options.get("matting_model", "ben2")
        try:
            info = ensure_matting_session_ready(mid)
            return int(info.get("recommend_batch") or 1)
        except Exception:
            return recommend_matting_batch_size(
                mid, refine=bool(options.get("matting_refine", False))
            )

    def _post_matting(
        self, img: Image.Image, options: dict, details: dict
    ) -> tuple[Image.Image, dict]:
        """抠图之后的 trim + 画布布局（单张）。"""
        img = img.convert("RGBA")
        did_matting = bool(options.get("enable_matting") or details.get("matting_model"))

        if options.get("enable_trim"):
            img, bbox = trim_transparent(img, options.get("alpha_threshold", 0))
            details["trim_bbox"] = bbox
            details["trimmed_size"] = img.size

        # 保留抠图：在 trim 之后、画布布局之前缓存透明主体（布局会改背景/尺寸）
        if did_matting and options.get("keep_matting"):
            details["_matting_keep_img"] = img.copy()

        # 智能对象式布局：asset 锁定为当前全分辨率主体，只把变换参数交给
        # place_subject_on_canvas，导出时预乘 Alpha 后一次栅格化到画布。
        if options.get("enable_layout"):
            asset = img
            img, layout_info = place_subject_on_canvas(
                asset,
                (int(options["canvas_w"]), int(options["canvas_h"])),
                subject_percent=int(options.get("subject_percent", 80)),
                canvas_color=options.get("canvas_color", "#FFFFFF"),
                detail_restore=str(options.get("detail_restore", "normal") or "normal"),
            )
            details.update(layout_info)

        return img, details

    @staticmethod
    def _matting_runtime_details(mid: str, refine: bool, batch_n: int) -> dict:
        """收集可写入日志的抠图运行信息（设备/路径/batch）。"""
        info = {
            "matting_model": mid,
            "matting_refine": refine,
            "matting_batch": max(1, int(batch_n)),
            "matting_path": "refine_rgba" if refine else "mask_prescale",
            "matting_device": "",
            "matting_vram_gb": 0.0,
        }
        try:
            st = ensure_matting_session_ready(mid)
            info["matting_device"] = str(st.get("device") or "")
            info["matting_vram_gb"] = float(st.get("vram_gb") or 0.0)
        except Exception:
            pass
        return info

    def process(self, img: Image.Image, options: dict) -> tuple[Image.Image, dict]:
        details = {"original_size": img.size}

        # 1) AI 抠图（最前）
        if options.get("enable_matting"):
            mid = options.get("matting_model", "ben2")
            refine = bool(options.get("matting_refine", False))
            try:
                img = remove_background(
                    img,
                    model_id=mid,
                    refine_foreground=refine,
                )
                details.update(self._matting_runtime_details(mid, refine, 1))
                details["matting_size"] = img.size
            except MattingError:
                raise
            except Exception as e:
                raise MattingError(str(e)) from e

        return self._post_matting(img, options, details)

    def matting_many(
        self, images: list[Image.Image], options_list: list[dict]
    ) -> list[tuple[Image.Image, dict]]:
        """
        仅执行 AI 抠图（不 trim/画布），供流水线后处理线程重叠使用。
        返回与输入等长的 (RGBA抠图结果, details) 列表。
        """
        if not images:
            return []
        if len(images) != len(options_list):
            raise ValueError("matting_many: images 与 options_list 长度不一致")

        opt0 = options_list[0]
        enable = bool(opt0.get("enable_matting"))
        refine = bool(opt0.get("matting_refine", False))
        mid = opt0.get("matting_model", "ben2")

        for o in options_list[1:]:
            if (
                bool(o.get("enable_matting")) != enable
                or bool(o.get("matting_refine", False)) != refine
                or (o.get("matting_model", "ben2") != mid)
            ):
                # 参数不一致：逐张仅抠图
                return [self._matting_one(im, o) for im, o in zip(images, options_list)]

        if not enable:
            out = []
            for im in images:
                rgba = im.convert("RGBA")
                out.append((rgba, {"original_size": im.size}))
            return out

        if refine or len(images) == 1:
            return [self._matting_one(im, o) for im, o in zip(images, options_list)]

        try:
            matted = remove_background_batch(
                images,
                model_id=mid,
                refine_foreground=False,
                batch_size=len(images),
            )
        except MattingError:
            raise
        except Exception as e:
            raise MattingError(str(e)) from e

        runtime = self._matting_runtime_details(mid, False, len(images))
        runtime["matting_pipeline"] = True
        out: list[tuple[Image.Image, dict]] = []
        for im0, mimg in zip(images, matted):
            details = {
                "original_size": im0.size,
                "matting_size": mimg.size,
            }
            details.update(runtime)
            details["matting_batch"] = len(images)
            out.append((mimg, details))
        return out

    def _matting_one(
        self, img: Image.Image, options: dict
    ) -> tuple[Image.Image, dict]:
        """单张仅抠图（或未开抠图时转 RGBA）。"""
        details = {"original_size": img.size}
        if not options.get("enable_matting"):
            return img.convert("RGBA"), details
        mid = options.get("matting_model", "ben2")
        refine = bool(options.get("matting_refine", False))
        try:
            out = remove_background(
                img, model_id=mid, refine_foreground=refine
            )
            details.update(self._matting_runtime_details(mid, refine, 1))
            details["matting_size"] = out.size
            details["matting_pipeline"] = True
            return out, details
        except MattingError:
            raise
        except Exception as e:
            raise MattingError(str(e)) from e

    def process_many(
        self, images: list[Image.Image], options_list: list[dict]
    ) -> list[tuple[Image.Image, dict]]:
        """
        micro-batch 入口：同批 options 的 AI 参数应一致。
        完整路径 = 抠图 + 后处理（无流水线时使用）。
        """
        if not images:
            return []
        if len(images) != len(options_list):
            raise ValueError("process_many: images 与 options_list 长度不一致")
        if len(images) == 1:
            return [self.process(images[0], options_list[0])]

        matted = self.matting_many(images, options_list)
        out: list[tuple[Image.Image, dict]] = []
        for (mimg, det), o in zip(matted, options_list):
            out.append(self._post_matting(mimg, o, dict(det)))
        return out
