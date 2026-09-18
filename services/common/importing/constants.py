"""导入相关常量。

P0 约定：导入常量集中在 importing 子包，避免继续扩张 config.py。
原定义于 ui/main_window.py 顶层，取值保持完全一致。

相机 RAW 扩展名权威源在 core.image_io.RAW_EXTS；此处再导出，供扫描/对话框使用。
"""

from core.image_io import RAW_EXTS as _RAW_EXTS

# 常规位图格式（进入处理列表）
_STANDARD_IMAGE_EXTS = {
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif", ".gif",
}
# 相机 RAW（与标准图一并进入列表；解码走 core.image_io.load_image）
RAW_EXTS = set(_RAW_EXTS)
# 图片格式（进入处理列表）= 标准图 + RAW
IMAGE_EXTS = set(_STANDARD_IMAGE_EXTS) | RAW_EXTS
# 文档容器：导入时只抽取内嵌图片，文档本身不进入列表
DOC_EXTS = {".docx", ".pdf"}
# HTML 容器：同上
HTML_EXTS = {".html", ".htm", ".xhtml"}
# 所有「抽图容器」扩展名
EXTRACT_EXTS = HTML_EXTS | DOC_EXTS
# 可直接加入列表的格式（仅图片）
VALID_EXTS = IMAGE_EXTS
# 导入/拖放可接受的文件（图片 + 抽图容器）
IMPORT_EXTS = IMAGE_EXTS | EXTRACT_EXTS

# URL/路径「像图片资源」判断使用的扩展名（含不进入处理列表的格式）
_IMAGE_PATH_EXTS = tuple(sorted(IMAGE_EXTS | {".svg", ".ico", ".avif", ".jfif"}))
# 从容器抽出时过滤过小的装饰图（字节）
_MIN_EXTRACTED_IMAGE_BYTES = 64

# 图片魔数 → 扩展名（无扩展名落盘时用；RAW 多靠后缀 + rawpy，不在此表）
_IMG_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"RIFF", ".webp"),  # 需再确认 WEBP
    (b"BM", ".bmp"),
)


def image_dialog_filter(
    *,
    include_containers: bool = True,
    title_all: str = "支持的文件",
    title_images: str = "图片文件",
    title_containers: str = "从文档抽图",
) -> str:
    """根据 IMAGE_EXTS 生成 QFileDialog 过滤器字符串。

    标准图与 RAW 一并列出；可选附带 HTML/DOCX/PDF 抽图容器。
    """
    # 稳定顺序：先标准再 RAW
    std = sorted(_STANDARD_IMAGE_EXTS)
    raw = sorted(RAW_EXTS)
    img_patterns = " ".join(f"*{e}" for e in std + raw)
    parts = [
        f"{title_all} ({img_patterns}"
        + (" *.docx *.pdf *.html *.htm" if include_containers else "")
        + ")",
        f"{title_images} ({img_patterns})",
    ]
    if include_containers:
        parts.append(f"{title_containers} (*.docx *.pdf *.html *.htm *.xhtml)")
    return ";;".join(parts)


def image_only_dialog_filter(title: str = "Image Files") -> str:
    """仅图片（含 RAW）的对话框过滤器，供叠加层选图等使用。"""
    std = sorted(_STANDARD_IMAGE_EXTS)
    raw = sorted(RAW_EXTS)
    patterns = " ".join(f"*{e}" for e in std + raw)
    return f"{title} ({patterns})"
