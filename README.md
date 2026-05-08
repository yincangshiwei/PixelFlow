# PixelFlow - 图像处理工作台

一款基于 PySide6 的桌面图像处理工具，采用插件化架构，支持多种图像处理功能的灵活扩展，支持单图和批量处理。

---

## 界面截图

### 主界面

![主页面.png](resources/screenshots/%E4%B8%BB%E9%A1%B5%E9%9D%A2.png)

### 图像处理 — 透明图处理

![透明图处理.png](resources/screenshots/%E9%80%8F%E6%98%8E%E5%9B%BE%E5%A4%84%E7%90%86.png)

#### 裁剪透明边缘

![透明图裁剪透明边缘.png](resources/screenshots/%E9%80%8F%E6%98%8E%E5%9B%BE%E8%A3%81%E5%89%AA%E9%80%8F%E6%98%8E%E8%BE%B9%E7%BC%98.png)

#### 裁剪加白底生成

![裁剪+白底生成.png](resources/screenshots/%E8%A3%81%E5%89%AA%2B%E7%99%BD%E5%BA%95%E7%94%9F%E6%88%90.png)

### 图像处理 — 基础处理

![基础处理.png](resources/screenshots/%E5%9F%BA%E7%A1%80%E5%A4%84%E7%90%86.png)

### 图像处理 — 图片排版导出

![图像排版导出.png](resources/screenshots/%E5%9B%BE%E5%83%8F%E6%8E%92%E7%89%88%E5%AF%BC%E5%87%BA.png)

### 图像处理 — 图片叠加

![图片叠加.png](resources/screenshots/%E5%9B%BE%E7%89%87%E5%8F%A0%E5%8A%A0.png)

### 后台日志

![后台日志.png](resources/screenshots/%E5%90%8E%E5%8F%B0%E6%97%A5%E5%BF%97.png)

---


## 界面设计

采用 **毛玻璃（Frosted Glass）深色主题**，左右双栏布局，整体风格追求科技感与通透质感，适配未来 AI 功能扩展。

### 左栏 — 图片列表区

| 模块 | 说明 |
|------|------|
| 文件操作栏 | 添加文件、添加文件夹、移除选中、清空，支持拖放图片/文件夹 |
| 缩略图列表 | 48×48 缩略图 + 文件名，后台线程异步加载不卡界面 |
| 预览区 | 点击列表项即可预览原图，显示文件名和尺寸信息 |

### 右栏 — Tab 切换区 + 输出设置 + 操作栏

右栏顶部为 **Tab 按钮**（图像处理 / 后台日志 / 版本日志），共享同一块主区域通过 `QStackedWidget` 切换：

| Tab 页 | 说明 |
|--------|------|
| **图像处理** | 功能下拉菜单 + 预设管理栏 + 参数面板 |
| **后台日志** | 日志占满整个区域，底部有清空日志按钮 |
| **版本日志** | 渲染 `resources/CHANGELOG.md`，支持 Markdown 格式展示，记录每次更新内容 |

Tab 下方始终可见的固定区域：

| 区域 | 说明 |
|------|------|
| **输出设置** | 四种输出路径模式 + 自动创建文件夹选项 |
| **底部操作栏** | 进度条 + 开始处理 / 取消按钮 |

点击「开始处理」时自动切换到后台日志页，方便实时查看完整日志。

### 输出路径模式

| 模式 | 行为 |
|------|------|
| 桌面路径（默认） | 显示路径输入框，默认填充桌面路径，可手动修改 |
| 自定义路径 | 显示路径输入框 + 浏览按钮，自由选择目录 |
| 原图路径 (覆盖原图) | 直接覆盖原文件，输出到原图所在目录 |
| 原图路径 (另存副本) | 在原图目录生成带后缀的副本，不覆盖原文件 |

桌面路径和自定义路径模式可勾选「在该路径下自动创建文件夹保存」（默认开启），自动在目标目录下创建 `PixelFlow_output/` 子文件夹。原图路径模式（覆盖/副本）下该选项自动隐藏。

### 预设系统

每个功能模块独立管理预设，预设文件按功能存放：

```
presets/
└── transparent_image/      ← 按 preset_id 一个功能一个文件夹
    ├── default.json        ← 默认参数（启动时自动生成/加载）
    ├── 电商白底图.json      ← 用户自定义预设
    └── 社交头像.json
```

| 操作 | 说明 |
|------|------|
| **选择预设** | 从下拉列表选择预设，**自动应用参数到面板**（无需额外点击加载按钮） |
| **加载预设** | 从外部路径导入预设文件（.json），支持跨功能加载。检测到重名时可选择**重命名**或**覆盖** |
| **保存预设** | 弹出输入框自定义名称，保存当前面板参数为 JSON |
| **恢复默认** | 用出厂默认值覆盖 default.json 并应用到面板 |
| **删除预设** | 删除选中的用户预设（default 不可删除） |
| **定位预设** | 一键打开当前功能的预设文件夹，方便直接管理预设文件 |

启动时自动加载 `default.json`，用户无需每次重新配置参数。支持从外部导入预设文件，方便预设分享和迁移。

### UI 设计规范

> 完整的 UI 设计规范已迁移至 `.codebuddy/rules/UI规则.mdc`，所有新功能、新面板、新控件必须遵循该规范。

以下为核心要点速览：

- **毛玻璃深色主题** — `rgba()` 半透明背景 + 微光边框，禁止使用 `QGraphicsBlurEffect`
- **色彩** — 主题色蓝紫渐变 `#5b8af5 → #7c6cf5`，主文字 `#e0e4f0`，面板底色 `rgba(30, 30, 52, 160)`
- **圆角** — 面板级 `14px`，卡片级 `10px`，控件级 `7~8px`
- **下拉框** — 所有 `QComboBox` 必须调用 `setStyleSheet(config.COMBOBOX_STYLE)`（定义于 `config.py`），禁止内联硬编码，确保下拉列表背景不透明、字体清晰可见
- **图标按钮** — 元素列表操作按钮使用 `config.ICON_BTN_STYLE`（emoji 图标 + 半透明磨砂底色 + hover 蓝光边框），与下拉框同级统一定义

---

## 后端架构

### 插件化处理器框架

```
core/
├── base_processor.py          # 基类 BaseProcessor + 注册表
├── image_processor.py         # 底层通用图像处理函数（裁剪、缩放等）
├── preset_manager.py          # 预设管理器（按功能管理 JSON 预设文件）
├── worker.py                  # 通用后台批量处理线程（基于 QThread）
└── processors/                # 处理器插件目录（每个功能一个文件）
    └── transparent_processor.py
```

**核心设计：**

- **`BaseProcessor`** — 抽象基类，定义处理器接口：
  - `name` / `description` / `icon` / `preset_id` — 名称、说明、图标、预设目录名
  - `create_panel()` — 创建参数设置面板（返回 QWidget）
  - `gather_options()` — 从面板收集参数（返回 dict）
  - `apply_options(options)` — 将参数字典应用到面板（用于加载预设）
  - `default_options()` — 返回出厂默认参数
  - `process(img, options)` — 处理单张图片（返回处理后 Image + 详情 dict）
  - `get_output_format()` — 返回输出格式

- **`@register_processor`** — 装饰器，自动将处理器类注册到全局注册表

- **`PresetManager`** — 按功能模块管理预设 JSON 文件，支持增删改查和默认预设

- **`ProcessWorker`** — 通用后台线程，接收任意 `BaseProcessor` 实例和参数，逐张处理并发射进度/完成信号

- **`image_processor.py`** — 底层函数库（`trim_transparent`、`resize_image`、`hex_to_rgba` 等），可被多个处理器复用

### 数据流

```
用户操作 → MainWindow 收集文件列表 + 处理器选项
         → ProcessWorker(QThread) 后台遍历文件
         → BaseProcessor.process() 处理每张图
         → 信号通知 UI 更新进度/日志（自动切换到日志 Tab）
```

---

## 功能说明

### ✂ 透明图处理

专为透明背景 PNG 图片设计的三步处理流水线，每个步骤独立开关：

| 步骤 | 参数 | 说明 |
|------|------|------|
| **裁剪透明边缘** | Alpha 阈值 (0-254) | 去除四周多余透明空间。阈值为 0 表示仅裁剪完全透明像素；调高可忽略半透明边缘 |
| **调整尺寸** | 宽、高、缩放模式 | 三种模式：等比缩放（完整显示）/ 等比铺满（可能裁切）/ 拉伸填充（不保持比例） |
| **放置到画布** | 宽、高、背景颜色 | 创建指定大小和颜色的画布，将图片居中放置。颜色支持 HEX / RGBA 透明色 |

**典型使用场景：** 电商产品图批量处理 — 抠图后去除多余透明边缘 → 统一缩放到目标尺寸 → 放入白色画布居中展示。

### ⚙ 基础处理

通用图片批量处理流水线，三个步骤均可独立启用，支持任意组合：

| 步骤 | 参数 | 说明 |
|------|------|------|
| **格式转换** | PNG / JPG / BMP / WEBP | 统一转换为目标格式，JPG/BMP 自动以白底合并透明通道 |
| **图片压缩** | 按质量 (1~100) / 按目标大小 (KB) | 按质量：控制 JPG / WEBP 输出质量；按目标大小：二分法自动寻找最优质量使文件不超过指定 KB，**仅对 JPG / WEBP 有效**（选择此模式时格式转换自动锁定为 JPG 或 WEBP） |
| **批量重命名** | 前缀模式、前缀文本、起始序号、位数 | 自定义前缀或保留原文件名，序号自动补零 |

**典型使用场景：** 电商图批量转 JPG + 压缩到 100KB 以内 + 按 `product_001` 格式重命名。

---

### 📑 图片排版导出

将多张图片按指定布局和顺序排列，导出为 PPTX / PDF / DOCX：

| 选项 | 说明 |
|------|------|
| **图片压缩（可选）** | 排版前预处理：按目标大小（KB）对每张图进行二分法压缩，支持 JPEG / WEBP 格式，默认不启用 |
| **导出格式** | PPTX / PDF / DOCX |
| **页面尺寸** | 自由设置页面宽高（厘米），不同格式智能切换默认尺寸（如 PPT 默认 33x19，PDF 默认 A4） |
| **排版布局** | 自定义每页图片数量 + 每行最大列数，图片先横向排满一行再换行，等比缩放填满行宽 |
| **智能排版算法** | 宽度按列均分，高度按每张图实际比例自适应；逐行计算行高，整体垂直居中；空间不足时等比缩小 |
| **排序规则** | 1. 按文件名默认排序（支持目录层级排序）<br>2. 读取 Excel 文件，按指定列的值进行排序，支持设置数据起始行（跳过表头） |
| **页面叠加元素** | 每页均可叠加任意数量的文字层或图片层，左侧元素列表 + 右侧配置面板（左右分栏），emoji 图标按钮操作（添加/删除/重命名/上下调序），支持用户自定义元素名称；支持 3×3 宫格快速定位（基于页面尺寸自动计算 X/Y 坐标） |

#### 页面叠加元素系统

支持在每一页排版结果上叠加额外内容，灵活自定义位置和样式：

| 叠加层类型 | 说明 |
|-----------|------|
| **文字层** | 三种数据源：固定文本、Excel 列数据（通过文件名匹配对应行）、图片文件名；支持字体选择（含加载外部字体）、字号、加粗、颜色（颜色选择器实时预览，默认黑色适配文档白底） |
| **图片层** | 指定本地图片路径（PNG/JPG，如 Logo）；可设置 X/Y 坐标（厘米）、宽/高（厘米） |

- **坐标系**：X/Y 均为相对页面左上角的距离（厘米），宽/高为元素实际尺寸（厘米），与页面尺寸直观对应；支持 3×3 宫格快速定位，基于页面宽高自动计算坐标
- **添加方式**：每个元素可独立选择「**叠加（不占空间）**」或「**占用空间（参与排版边距）**」两种模式
  - **叠加**：直接绘制在页面上，不影响图片排版区域大小
  - **占用空间**：元素占据页面边缘区域，图片排版区自动缩小避让（如顶部 Logo、底部文字）
- **Excel 数据匹配**：每个文字层可独立配置 Excel 文件、匹配列、数据列、数据起始行，通过图片文件名精确匹配对应行
- **PPTX/PDF**：支持精确绝对定位叠加；**DOCX**：以段落形式追加（Word 不支持绝对坐标定位）
- 叠加层配置（含位置、样式、数据源、添加方式）可保存到预设，下次直接加载

**典型使用场景：** 将一个目录下的数百张产品图，按 Excel 中定义的产品编号排序，每页 4 张（每行最多 4 列），左下角叠加 Excel 中对应行的产品名称（占用空间），右上角叠加公司 Logo（占用空间），图片自动缩小避让 Logo 和文字区域，一键导出为产品画册 PDF 或汇报 PPT。

---

### 🎨 图片叠加

在图片上叠加文本和图片元素，支持多种数据源和灵活的样式配置，适用于批量添加水印、标注、产品信息等场景。

| 功能模块 | 说明 |
|---------|------|
| **元素列表管理** | 左侧元素列表 + 右侧配置面板（左右分栏），emoji 图标按钮操作（添加/删除/重命名/上下调序），支持用户自定义元素名称 |
| **文本元素** | 三种数据源：固定文本、Excel 列数据、图片文件名；支持字体选择、字号调整、加粗、颜色自定义 |
| **图片元素** | 叠加外部图片（如 Logo、水印图），支持设置位置和大小 |
| **坐标系统** | X/Y 均为像素坐标，相对于图片左上角，精确控制元素位置；支持 3×3 宫格快速定位（点击格点自动根据底图尺寸计算坐标） |
| **智能位置分配** | 新增元素自动错开 Y 坐标，避免默认重叠 |
| **Excel 数据匹配** | 通过文件名精确匹配 Excel 对应行，支持设置匹配列和数据列，不依赖图片顺序 |
| **字体支持** | 内置 Microsoft YaHei、SimHei、SimSun、KaiTi、FangSong、Arial、Times New Roman 等常用字体；**支持加载外部字体文件**（.ttf/.otf/.ttc），动态添加到字体列表，自动回退中文字体 |
| **字体缓存优化** | 相同字体配置自动复用，避免重复加载导致内存泄漏 |
| **资源管理** | 叠加图片使用后自动释放内存，支持大批量图片稳定处理 |
| **输出格式** | PNG / WEBP / JPG |

#### 文本元素数据源

| 数据源 | 说明 | 适用场景 |
|-------|------|---------|
| **固定文本** | 手动输入固定内容，所有图片使用相同文本 | 添加统一的水印、版权声明、品牌标语 |
| **Excel 列数据** | 从 Excel 文件读取，通过文件名匹配对应行，读取指定列数据 | 批量添加产品编号、名称、价格等差异化信息 |
| **图片文件名** | 自动使用当前图片的文件名（不含扩展名）作为文本 | 快速标注文件名、制作索引图 |

#### Excel 数据匹配配置

| 参数 | 说明 | 默认值 |
|-----|------|-------|
| **匹配列** | 图片文件名所在的列号（从 1 开始），用于查找对应行 | 1 |
| **数据列** | 要读取的文本数据列号（从 1 开始） | 2 |
| **数据起始行** | 数据从第几行开始（跳过表头） | 2 |

**匹配逻辑：** 遍历 Excel 从起始行开始的所有行 → 读取匹配列的值 → 与当前图片文件名（不含扩展名）不区分大小写匹配 → 找到匹配行后读取数据列内容 → 叠加到图片指定位置。

#### 文本样式配置

| 样式项 | 说明 | 默认值 |
|-------|------|-------|
| **字体** | 内置常用字体，支持加载外部字体文件（.ttf/.otf/.ttc）动态扩展 | Microsoft YaHei |
| **字号** | 8-200 像素 | 24 |
| **加粗** | 可选开启/关闭 | 关闭 |
| **颜色** | 支持 HEX / RGBA 透明色，颜色选择器实时预览 | #FFFFFF（白色） |

#### 元素配置说明

**文本元素：**
- 位置：X/Y 像素坐标（相对于图片左上角，如 1000×1000 的图片中 X=500, Y=500 表示居中）
- 内容：根据数据源自动获取或手动输入
- 样式：字体、字号、加粗、颜色可独立配置

**图片元素：**
- 位置：X/Y 像素坐标（相对于底图左上角）
- 大小：宽度/高度（像素值，精确控制叠加图片尺寸）
- 支持 PNG 透明图叠加，自动保留透明通道

**典型使用场景：**

1. **电商产品图批量标注** — 数百张产品图，通过 Excel 匹配产品编号和价格，叠加到图片左下角，右上角添加品牌 Logo，一键完成。
2. **摄影作品水印** — 批量添加摄影师名称（固定文本或文件名）+ 半透明 Logo 水印。
3. **产品说明书配图** — 在每张产品图上不同位置叠加多个文本标签（名称、规格、材质等），清晰标注。
4. **活动海报个性化** — 底图相同，通过 Excel 为每张海报叠加不同的参与者姓名、座位号等信息。

---

- **批量处理** — 多文件添加、文件夹导入、拖放操作，后台线程处理不卡界面
- **目录层级展示** — 导入文件夹时保留子目录层级，方便按目录结构排序和排版
- **缩略图列表** — 文件列表显示 48×48 缩略图 + 文件名，异步加载
- **自适应参数面板** — 右侧参数区支持滚动，完美容纳复杂的参数项
- **实时预览** — 点击文件列表即可预览原图及尺寸信息
- **预设管理** — 按功能保存/加载/恢复参数预设，下拉选择即自动应用，支持从外部文件导入预设，启动自动加载默认预设
- **多格式输出** — PNG、WebP、JPG
- **后台日志** — 独立 Tab 页占满区域显示完整日志，支持清空

---

## 项目结构

```
PixelFlow/
├── app.py                              # 应用入口
├── config.py                           # 全局配置（名称、版本、路径等唯一信源）
├── build.py                            # 自动化打包脚本
├── PixelFlow.spec                      # PyInstaller 打包配置（从 config.py 读取）
├── installer.iss.template              # Inno Setup 模板（由 build.py 生成 .iss）
├── ChineseSimplified.isl               # Inno Setup 简体中文语言文件
├── innosetup-6.7.1.exe                 # Inno Setup 安装程序（随项目附带，方便一键配置打包环境）
├── requirements.txt                    # 依赖清单
├── resources/                          # UI 资源文件
│   ├── app.ico
│   ├── check_on.svg / check_off.svg
│   ├── radio_on.svg / radio_off.svg
│   ├── CHANGELOG.md                    # 版本日志（Markdown，供「版本日志」Tab 渲染展示）
│   └── screenshots/                    # 界面截图（用于 README 展示）
├── presets/                            # 预设文件目录（运行时自动生成）
│   ├── transparent_image/
│   ├── basic_process/
│   ├── img2doc/
│   └── image_overlay/
├── core/                               # 后端核心
│   ├── base_processor.py               # 处理器基类 + 注册表（支持批量合并接口）
│   ├── base_file_processor.py          # 文件处理器基类 + 注册表
│   ├── image_processor.py              # 底层图像处理函数（含二分法最优压缩算法）
│   ├── preset_manager.py               # 预设管理器
│   ├── worker.py                       # 后台批量处理线程（支持单图和批量合并路由）
│   ├── file_worker.py                  # 文件批量处理线程
│   └── processors/
│       ├── transparent_processor.py    # 透明图处理器
│       ├── basic_processor.py          # 基础处理器
│       ├── img2doc_processor.py        # 图片排版导出处理器
│       └── overlay_processor.py        # 图片叠加处理器
└── ui/
    └── main_window.py                  # 主窗口（支持自适应滚动、双处理器体系、目录树展示）
```

## 环境要求

- Python 3.12+
- PySide6 < 6.9
- Pillow >= 10.0
- python-pptx >= 0.6（图片排版导出 PPT）
- reportlab >= 4.0（图片排版导出 PDF）
- python-docx >= 1.1（图片排版导出 Word）
- openpyxl >= 3.1（Excel 排序功能）

## 安装与运行

```bash
pip install -r requirements.txt
python app.py
```

---

## 打包与分发

### 方式一：打包为可执行程序（免安装）

使用 PyInstaller 将项目打包为独立 exe，用户无需安装 Python 环境。

```bash
# 安装打包依赖
pip install pyinstaller

# 一键打包
python build.py
```

打包完成后，输出目录为 `dist/PixelFlow/`，直接运行其中的 `PixelFlow.exe` 即可。

### 方式二：生成 Windows 安装包

在方式一的基础上，使用 Inno Setup 6 生成标准 Windows 安装程序（`.exe` 安装包）。

```bash
# 一键打包 + 生成安装包
python build.py --installer
```

安装包输出到 `dist/installer/PixelFlow_Setup_x.x.x.exe`。

#### Inno Setup 安装

项目根目录已附带 **`innosetup-6.7.1.exe`**（Inno Setup 官方安装程序）和 **`ChineseSimplified.isl`**（简体中文语言包），无需另行下载：

1. 运行 `innosetup-6.7.1.exe` 完成安装
2. 将 `ChineseSimplified.isl` 复制到 Inno Setup 安装目录下的 `Languages/` 子目录（默认路径：`C:\Program Files (x86)\Inno Setup 6\Languages\`）
3. 执行 `python build.py --installer` 即可生成中文安装包

> **说明：** `build.py` 会自动检测以下 Inno Setup 安装路径，无需手动配置环境变量：
> - `C:\Program Files (x86)\Inno Setup 6\`
> - `C:\Program Files\Inno Setup 6\`
> - `D:\Inno Setup 6\`
> - `E:\Inno Setup 6\`
> - 系统 PATH 中的 `ISCC.exe`
>
> 若安装在其他目录，可在执行前手动指定：
> ```bash
> set ISCC=你的安装路径\ISCC.exe
> ```

### 打包相关文件

| 文件 | 说明 |
|------|------|
| `config.py` | **唯一信源** — 应用名称、版本、描述、路径等全局配置 |
| `PixelFlow.spec` | PyInstaller 打包配置，从 `config.py` 读取应用名 |
| `installer.iss.template` | Inno Setup 模板，占位符由 `build.py` 自动替换生成 `installer.iss` |
| `installer.iss` | 由 `build.py` 生成的实际打包脚本（不纳入版本控制） |
| `ChineseSimplified.isl` | Inno Setup 简体中文语言文件，需手动复制到 Inno Setup 的 `Languages/` 目录 |
| `innosetup-6.7.1.exe` | Inno Setup 安装程序，随项目附带方便离线安装 |
| `build.py` | 自动化打包脚本，从 `config.py` 读取所有元信息，支持 `--installer` 和 `--clean` |

### 版本更新

只需修改 `config.py` 中的一处即可，所有打包脚本自动同步：

```python
# config.py
APP_NAME = "PixelFlow"
APP_VERSION = "1.0.0"      # ← 改这里
APP_DESCRIPTION = "图像处理工作台"
APP_PUBLISHER = "PixelFlow"
```

### 清理打包产物

```bash
python build.py --clean
```

---

## 扩展新功能

继承 `BaseProcessor` 并使用 `@register_processor` 装饰器，即可自动注册到功能菜单并支持预设系统：

```python
from PIL import Image
from PySide6.QtWidgets import QWidget
from core.base_processor import BaseProcessor, register_processor

@register_processor
class MyProcessor(BaseProcessor):
    name = "我的功能"
    description = "功能简要说明"
    icon = "🎨"
    preset_id = "my_feature"  # 预设目录名（英文唯一标识）

    def create_panel(self, parent=None) -> QWidget:
        """创建参数设置面板"""
        ...

    def gather_options(self) -> dict:
        """从面板收集当前参数"""
        ...

    def apply_options(self, options: dict):
        """将参数字典应用到面板（加载预设用）"""
        ...

    def default_options(self) -> dict:
        """返回出厂默认参数"""
        ...

    def get_output_format(self) -> str:
        """返回输出格式: png / jpg / webp"""
        ...

    def process(self, img: Image.Image, options: dict) -> tuple[Image.Image, dict]:
        """处理单张图片，返回 (处理后图片, 详情字典)"""
        ...
```

然后在 `main_window.py` 中添加一行导入触发注册：

```python
import core.processors.my_processor  # noqa: F401
```

重启应用后，新功能即出现在功能下拉菜单中，预设系统自动可用。

## License

MIT
