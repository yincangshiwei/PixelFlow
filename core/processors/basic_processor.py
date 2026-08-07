"""
基础图片处理器 —— 格式转换 + 图片压缩 + 修改 DPI + 批量重命名

P2：UI 已迁至 ui.routes.process.features.basic_route.BasicFeatureRoute；
参数校验/规范化在 services.features.basic_service.BasicService。
本类只负责 process() 纯图像变换，不创建或读取控件。
不依赖 services / ui（保持 core → 无上层反向依赖）。
"""
from PIL import Image

from core.base_processor import BaseProcessor


def default_basic_options() -> dict:
    """出厂默认参数（BasicService / 预设与此保持一致）。"""
    return {
        "enable_compress": False,
        "compress_mode": "quality",
        "quality": 85,
        "target_size_kb": 500,
        "enable_format": False,
        "output_format": "png",
        "enable_dpi": False,
        "dpi": 300,
        "enable_rename": False,
        "prefix_mode": "custom",
        "prefix": "",
        "start_index": 1,
        "digits": 3,
    }


class BasicProcessor(BaseProcessor):
    """基础图片处理：格式转换 / 压缩 / 修改 DPI / 批量重命名（纯处理，无 UI）。"""

    name = "基础处理"
    description = "格式转换 / 图片压缩 / 修改 DPI / 批量重命名，可任意组合"
    icon = "⚙"
    preset_id = "basic_process"

    def default_options(self) -> dict:
        return default_basic_options()

    def process(self, img: Image.Image, options: dict) -> tuple[Image.Image, dict]:
        """
        格式转换的色彩模式在此处理；压缩与 DPI 在 worker 保存阶段写入。
        重命名逻辑由 worker 在构建输出文件名时处理。
        DPI 不改变像素数据，仅在保存时写入 density 元数据。
        """
        details = {"original_size": img.size, "original_mode": img.mode}

        output_fmt = options.get("output_format", "png") if options.get("enable_format") else None
        if output_fmt:
            output_fmt = str(output_fmt).lower().strip()
            if output_fmt == "jpeg":
                output_fmt = "jpg"

        # 仅在启用格式转换时做色彩模式适配；
        # 未转换格式时保持原 mode，避免「仅改 DPI / 仅重命名」时无谓转色导致画质/体积变化。
        if output_fmt in ("jpg", "bmp"):
            if img.mode in ("RGBA", "LA", "P"):
                background = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                if img.mode in ("RGBA", "LA"):
                    background.paste(img, mask=img.split()[-1])
                else:
                    background.paste(img)
                img = background
                details["mode_converted"] = "RGBA→RGB (白底合并)"
            elif img.mode != "RGB":
                img = img.convert("RGB")
        elif output_fmt in ("png", "webp"):
            if img.mode != "RGBA":
                img = img.convert("RGBA")

        if options.get("enable_dpi"):
            details["dpi"] = int(options.get("dpi", 300))

        details["output_format"] = output_fmt or "原格式"
        return img, details
