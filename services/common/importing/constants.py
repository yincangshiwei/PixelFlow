"""导入相关常量。

P0 约定：导入常量集中在 importing 子包，避免继续扩张 config.py。
原定义于 ui/main_window.py 顶层，取值保持完全一致。
"""

# 图片格式（进入处理列表）
IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tiff', '.tif', '.gif'}
# 文档容器：导入时只抽取内嵌图片，文档本身不进入列表
DOC_EXTS = {'.docx', '.pdf'}
# HTML 容器：同上
HTML_EXTS = {'.html', '.htm', '.xhtml'}
# 所有「抽图容器」扩展名
EXTRACT_EXTS = HTML_EXTS | DOC_EXTS
# 可直接加入列表的格式（仅图片）
VALID_EXTS = IMAGE_EXTS
# 导入/拖放可接受的文件（图片 + 抽图容器）
IMPORT_EXTS = IMAGE_EXTS | EXTRACT_EXTS

# URL/路径「像图片资源」判断使用的扩展名（含不进入处理列表的格式）
_IMAGE_PATH_EXTS = tuple(sorted(IMAGE_EXTS | {'.svg', '.ico', '.avif', '.jfif'}))
# 从容器抽出时过滤过小的装饰图（字节）
_MIN_EXTRACTED_IMAGE_BYTES = 64

# 图片魔数 → 扩展名
_IMG_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"RIFF", ".webp"),  # 需再确认 WEBP
    (b"BM", ".bmp"),
)
