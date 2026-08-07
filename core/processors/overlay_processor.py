"""
图片叠加处理器 —— 在图片上叠加文本和图片元素（纯处理，无 UI）。

P4：UI 已迁至 ui.routes.process.features.overlay_route.OverlayFeatureRoute；
参数校验在 services.features.overlay_service.OverlayService。
"""
from __future__ import annotations

import os
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

from core.base_processor import BaseProcessor
from core.image_processor import hex_to_rgba


def default_overlay_options() -> dict:
    return {
        "elements": [],
        "output_format": "png",
        "custom_fonts": {},
    }


class OverlayElement:
    """叠加元素基类"""
    def __init__(self, element_type, x, y, name=""):
        self.element_type = element_type
        self.x = x
        self.y = y
        self.name = name


class TextElement(OverlayElement):
    """文本元素"""
    def __init__(
        self, x=50, y=50, source="fixed", content="",
        font_size=24, font_family="Microsoft YaHei", bold=False,
        color="#FFFFFF", excel_file="", match_column=0, data_column=0,
        excel_row_start=2, name="",
    ):
        super().__init__("text", x, y, name)
        self.source = source
        self.content = content
        self.font_size = font_size
        self.font_family = font_family
        self.bold = bold
        self.color = color
        self.excel_file = excel_file
        self.match_column = match_column
        self.data_column = data_column
        self.excel_row_start = excel_row_start


class ImageElement(OverlayElement):
    """图片元素"""
    def __init__(self, x=100, y=100, image_path="", width=200, height=200, name=""):
        super().__init__("image", x, y, name)
        self.image_path = image_path
        self.width = width
        self.height = height


class OverlayProcessor(BaseProcessor):
    """图片叠加（纯处理，无 UI）。"""

    name = "图片叠加"
    description = "在图片上叠加文本和图片元素，支持Excel数据、图片名称等数据源"
    icon = "🎨"
    preset_id = "image_overlay"

    def __init__(self):
        self._font_cache: dict = {}
        self._custom_fonts: dict = {}
        self._base_img_width = 1920
        self._base_img_height = 1080
        self._has_base_image = False

    def set_base_image_size(self, w: int, h: int):
        if w > 0 and h > 0:
            self._base_img_width = w
            self._base_img_height = h
            self._has_base_image = True

    def default_options(self) -> dict:
        return default_overlay_options()

    def _clear_font_cache(self):
        """清理字体缓存，释放内存"""
        for font in self._font_cache.values():
            try:
                del font
            except:
                pass
        self._font_cache.clear()

    def _get_font(self, font_family: str, font_size: int, bold: bool):
        """获取字体，支持中文字体，使用缓存避免重复加载"""
        # 生成缓存键
        cache_key = f"{font_family}_{font_size}_{bold}"
        
        # 检查缓存
        if cache_key in self._font_cache:
            return self._font_cache[cache_key]
        
        # 缓存过大时清理（防止内存泄漏）
        if len(self._font_cache) > 100:
            self._clear_font_cache()
        
        try:
            # 尝试使用指定字体
            font_path = None
            
            # 检查是否为自定义字体
            if hasattr(self, '_custom_fonts') and font_family in self._custom_fonts:
                font_path = self._custom_fonts[font_family]
                font = ImageFont.truetype(font_path, font_size)
                self._font_cache[cache_key] = font
                return font
            
            # Windows 系统字体路径
            if os.name == 'nt':
                font_dir = os.path.join(os.environ.get('WINDIR', 'C:\\Windows'), 'Fonts')
                
                # 字体映射（优先使用中文字体）
                font_map = {
                    'Microsoft YaHei': ('msyh.ttc', 0),  # 微软雅黑，索引0为常规体
                    'SimHei': ('simhei.ttf', 0),         # 黑体
                    'SimSun': ('simsun.ttc', 0),         # 宋体，索引0
                    'KaiTi': ('simkai.ttf', 0),          # 楷体
                    'FangSong': ('simfang.ttf', 0),      # 仿宋
                    'Arial': ('arial.ttf', 0),           # Arial（不支持中文）
                    'Times New Roman': ('times.ttf', 0), # Times New Roman（不支持中文）
                }
                
                font_info = font_map.get(font_family)
                if font_info:
                    font_file, font_index = font_info
                    font_path = os.path.join(font_dir, font_file)
                    if not os.path.exists(font_path):
                        font_path = None
                    
                    # 如果是 .ttc 字体文件，需要指定索引
                    if font_path and font_path.endswith('.ttc'):
                        font = ImageFont.truetype(font_path, font_size, index=font_index)
                        self._font_cache[cache_key] = font
                        return font
                    elif font_path:
                        font = ImageFont.truetype(font_path, font_size)
                        self._font_cache[cache_key] = font
                        return font
            
            # 如果指定字体加载失败，尝试加载中文字体作为回退
            fallback_fonts = ['msyh.ttc', 'simhei.ttf', 'simsun.ttc']
            if os.name == 'nt':
                font_dir = os.path.join(os.environ.get('WINDIR', 'C:\\Windows'), 'Fonts')
                for fallback in fallback_fonts:
                    fallback_path = os.path.join(font_dir, fallback)
                    if os.path.exists(fallback_path):
                        if fallback.endswith('.ttc'):
                            font = ImageFont.truetype(fallback_path, font_size, index=0)
                            self._font_cache[cache_key] = font
                            return font
                        else:
                            font = ImageFont.truetype(fallback_path, font_size)
                            self._font_cache[cache_key] = font
                            return font
            
            # 最终回退到默认字体
            font = ImageFont.load_default()
            self._font_cache[cache_key] = font
            return font
        except Exception as e:
            # 异常时回退到默认字体
            print(f"字体加载失败: {e}，使用默认字体")
            font = ImageFont.load_default()
            self._font_cache[cache_key] = font
            return font

    def _read_excel_data(self, excel_file: str, match_column: int, data_column: int, 
                         row_start: int, image_filename: str):
        """从Excel读取数据，通过文件名匹配对应行"""
        try:
            import openpyxl
            
            if not excel_file or not os.path.exists(excel_file):
                return ""
            
            wb = openpyxl.load_workbook(excel_file, data_only=True)
            ws = wb.active
            
            # 获取文件名（不含扩展名）
            image_stem = Path(image_filename).stem
            
            # 遍历数据行，查找匹配的文件名
            for row in range(row_start, ws.max_row + 1):
                # 读取匹配列的值
                match_col_letter = openpyxl.utils.get_column_letter(match_column)
                match_value = ws[f'{match_col_letter}{row}'].value
                
                if match_value is None:
                    continue
                
                # 转换为字符串并去除扩展名（如果匹配列包含扩展名）
                match_str = str(match_value).strip()
                # 去除可能的扩展名
                if '.' in match_str:
                    match_stem = Path(match_str).stem
                else:
                    match_stem = match_str
                
                # 匹配文件名（不区分大小写）
                if match_stem.lower() == image_stem.lower():
                    # 找到匹配行，读取数据列
                    data_col_letter = openpyxl.utils.get_column_letter(data_column)
                    data_value = ws[f'{data_col_letter}{row}'].value
                    return str(data_value) if data_value is not None else ""
            
            # 未找到匹配行
            return ""
            
        except Exception as e:
            return f"[Excel读取错误: {str(e)}]"

    def _get_text_content(self, element: TextElement, image_path: str, image_index: int):
        """获取文本内容"""
        if element.source == 'fixed':
            return element.content
        elif element.source == 'filename':
            # 获取文件名（不含扩展名）
            return Path(image_path).stem
        elif element.source == 'excel':
            return self._read_excel_data(
                element.excel_file, 
                element.match_column,
                element.data_column,
                element.excel_row_start,
                image_path
            )
        return ""

    def process(self, img: Image.Image, options: dict) -> tuple[Image.Image, dict]:
        """处理单张图片，叠加元素"""
        # 运行时字体映射来自 options（面板不再持有）
        self._custom_fonts = dict(options.get("custom_fonts") or {})
        details = {"original_size": img.size, "overlays": []}
        
        # 转换为RGBA
        img = img.convert("RGBA")
        img_width, img_height = img.size
        
        # 注意：process方法在批量处理时会被多次调用
        # 我们需要知道当前是第几张图片，以便正确读取Excel数据
        # 但BaseProcessor的process接口没有提供image_index参数
        # 这里我们使用一个临时方案：从options中获取（需要在worker中传递）
        image_index = options.get('_image_index', 0)
        current_image_path = options.get('_current_image_path', '')
        
        # 创建绘图对象
        draw = ImageDraw.Draw(img)
        
        elements_data = options.get('elements', [])
        
        for idx, elem_data in enumerate(elements_data):
            try:
                if elem_data['type'] == 'text':
                    # 文本元素
                    text_content = ""
                    if elem_data['source'] == 'fixed':
                        text_content = elem_data.get('content', '')
                    elif elem_data['source'] == 'filename':
                        text_content = Path(current_image_path).stem if current_image_path else ""
                    elif elem_data['source'] == 'excel':
                        text_content = self._read_excel_data(
                            elem_data.get('excel_file', ''),
                            elem_data.get('match_column', 1),
                            elem_data.get('data_column', 2),
                            elem_data.get('excel_row_start', 2),
                            current_image_path
                        )
                    
                    if not text_content:
                        details["overlays"].append({
                            "type": "text_skipped",
                            "index": idx,
                            "reason": "文本内容为空",
                            "source": elem_data.get('source', 'unknown'),
                            "position": (elem_data['x'], elem_data['y'])
                        })
                        continue
                    
                    # 直接使用像素坐标
                    x = elem_data['x']
                    y = elem_data['y']
                    
                    # 获取字体
                    font_family = elem_data.get('font_family', 'Microsoft YaHei')
                    font_size = elem_data.get('font_size', 24)
                    bold = elem_data.get('bold', False)
                    font = self._get_font(font_family, font_size, bold)
                    
                    # 获取颜色
                    color = hex_to_rgba(elem_data.get('color', '#FFFFFF'))
                    
                    # 绘制文本
                    draw.text((x, y), text_content, fill=color, font=font)
                    
                    details["overlays"].append({
                        "type": "text",
                        "index": idx,
                        "content": text_content[:30],
                        "font": f"{font_family} {font_size}px",
                        "position": (x, y),
                        "color": elem_data.get('color', '#FFFFFF')
                    })
                
                elif elem_data['type'] == 'image':
                    # 图片元素
                    overlay_path = elem_data.get('image_path', '')
                    if not overlay_path or not os.path.exists(overlay_path):
                        continue
                    
                    # 直接使用像素坐标和尺寸
                    x = elem_data['x']
                    y = elem_data['y']
                    overlay_w = elem_data['width']
                    overlay_h = elem_data['height']
                    
                    # 加载叠加图片
                    overlay_img = Image.open(overlay_path).convert("RGBA")
                    overlay_img = overlay_img.resize((overlay_w, overlay_h), Image.LANCZOS)
                    
                    # 粘贴到目标位置
                    img.paste(overlay_img, (x, y), overlay_img)
                    
                    # 关闭叠加图片释放内存
                    overlay_img.close()
                    
                    details["overlays"].append({
                        "type": "image",
                        "index": idx,
                        "path": Path(overlay_path).name,
                        "position": (x, y),
                        "size": (overlay_w, overlay_h)
                    })
            
            except Exception as e:
                import traceback
                error_trace = traceback.format_exc()
                details["overlays"].append({
                    "type": "error",
                    "index": idx,
                    "error": str(e),
                    "traceback": error_trace[:200]  # 只保留前200字符
                })
        
        # 记录处理统计
        text_count = sum(1 for o in details["overlays"] if o.get("type") == "text")
        image_count = sum(1 for o in details["overlays"] if o.get("type") == "image")
        skipped_count = sum(1 for o in details["overlays"] if o.get("type") in ("text_skipped", "image_skipped"))
        error_count = sum(1 for o in details["overlays"] if o.get("type") == "error")
        details["summary"] = f"文本:{text_count}, 图片:{image_count}, 跳过:{skipped_count}, 错误:{error_count}, 总计:{len(elements_data)}"
        
        # 清理绘图对象
        del draw
        
        return img, details
