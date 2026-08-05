# PixelFlow 技术文档

本文档记录项目核心功能的实现原理、架构约定与扩展方式，供开发与维护参考。  
产品使用说明见 [README.md](README.md)，版本变更见 [resources/CHANGELOG.md](resources/CHANGELOG.md)。

---

## 1. 技术栈与入口

| 层级 | 技术 |
|------|------|
| UI | PySide6（Qt6） |
| 图像 | Pillow、numpy |
| AI 抠图 | BEN2 / RMBG 2.0 + PyTorch（**独立 uv 环境**，主程序不 import torch） |
| 元数据 / 办公导出 | 视功能依赖 openpyxl、python-pptx、python-docx、reportlab 等 |
| 打包 | PyInstaller（`PixelFlow.spec`）+ Inno Setup |

**入口：** `app.py`  
- 开发：直接运行 `app.py`  
- 打包：处理 Qt 插件路径、未捕获异常写入 `logs/app/`  
- 主窗口：`ui/main_window.py`  
- 配置中心：`ui/settings_panel.py`  
- 全局常量 / 样式：`config.py`

---

## 2. 目录结构（逻辑）

```
PixelFlow/
├── app.py                      # 入口
├── config.py                   # 路径、样式、常量
├── core/
│   ├── base_processor.py       # 图像处理器基类 + 注册表
│   ├── base_file_processor.py  # 文件类处理器基类
│   ├── worker.py               # 图像批处理 QThread
│   ├── file_worker.py          # 文件批处理 QThread
│   ├── batch_session.py        # 续跑 / 重试会话
│   ├── image_processor.py      # 通用图像算法（裁边、画布等）
│   ├── metadata_utils.py       # EXIF / tEXt / 格式识别
│   ├── preset_manager.py       # 预设 JSON
│   ├── log_manager.py          # 后台日志落盘
│   ├── processors/             # 功能插件
│   ├── matting/                # AI 抠图
│   │   ├── inference.py        # 主进程推理封装
│   │   ├── pipeline.py         # 三阶段流水线
│   │   ├── model_registry.py   # 模型元数据
│   │   ├── model_manager.py    # 权重 / 就绪
│   │   ├── hardware.py         # 硬件评估
│   │   └── workers/            # 子进程脚本（在隔离 venv 中跑）
│   └── runtime/
│       └── env_manager.py      # uv / Python / 模型 venv
├── ui/
├── models/matting/<id>/        # 模型权重（本地数据）
├── runtime/                    # AI 运行时数据（gitignore）
│   ├── uv/
│   ├── envs/<id>/.venv/
│   └── runtime_settings.json
├── presets/<preset_id>/        # 用户预设
├── resources/                  # 图标、CHANGELOG、截图
└── PixelFlow.spec              # 打包配置
```

### 路径约定（重要）

| 路径 | 含义 | Git |
|------|------|-----|
| `core/runtime/` | 运行时**源码**（`RuntimeManager`） | 必须提交 |
| 根目录 `/runtime/` | 本机 AI 数据（uv、venv、设置） | **忽略**（`.gitignore` 写 `/runtime/`） |
| `models/matting/` | 权重文件 | 通常不提交大文件 |

> 切勿把 ignore 写成 `runtime/`（无前导 `/`），会误伤 `core/runtime/`。

---

## 3. 插件化处理器架构

### 3.1 图像处理器（`BaseProcessor`）

每个功能一个类，放在 `core/processors/`，用 `@register_processor` 注册。

**必须实现：**

| 接口 | 作用 |
|------|------|
| `name` / `description` / `icon` / `preset_id` | 展示与预设目录名 |
| `create_panel()` | 参数面板 `QWidget` |
| `gather_options()` / `apply_options()` / `default_options()` | 参数字典 ↔ UI |
| `process(img, options) -> (Image, details)` | 单张处理 |
| `get_output_format()` | 输出扩展名相关 |

**可选：**

| 接口 | 作用 |
|------|------|
| `is_batch_processor = True` | 走 `process_batch`（合并导出类） |
| `process_many` / `matting_many` | micro-batch / 流水线 |
| `preferred_matting_batch_size` | 建议 GPU batch |
| `on_selected_image(path)` | 列表选中时回读（如元数据） |
| `on_panel_activated()` | 切回图像 Tab 时刷新提示 |

### 3.2 批处理数据流

```
MainWindow
  → 收集文件列表 + gather_options() 快照（含 _output_format）
  → ProcessWorker(QThread)
       ├─ is_batch_processor → process_batch(...)
       ├─ AI 抠图多图 → matting pipeline（见 §5）
       └─ 默认 → 逐张 process() → 保存
  → image_done / debug 信号 → 后台日志 + 列表状态
  → QThread.finished → 结算弹窗（须等 AI 子进程关闭）
```

**线程安全约定：**

- Worker **不得**访问 QComboBox 等 UI 控件；输出格式在启动前写入 `options["_output_format"]`。
- 取消只置 `_cancelled`；常驻抠图进程在 Worker 线程 `finally` 中 `shutdown_matting_workers()`。
- 完成弹窗在 `QThread.finished` 之后，避免线程未退出即销毁导致崩溃。

### 3.3 当前处理器一览

| 模块 | 文件 | 类型 |
|------|------|------|
| 透明图处理 | `transparent_processor.py` | 逐张 + AI 流水线 |
| 基础处理 | `basic_processor.py` | 逐张（格式/压缩/DPI/重命名） |
| 元数据编辑 | `metadata_processor.py` | 多为 `process_batch` 按路径写，保留 EXIF |
| 图片叠加 | `overlay_processor.py` | 逐张 |
| 图片排版导出 | `img2doc_processor.py` | `is_batch_processor`（PPT/PDF/Word） |

### 3.4 新增功能步骤（简版）

1. 在 `core/processors/` 新建类，继承 `BaseProcessor` 并 `@register_processor`。  
2. 实现面板（遵循 UI 规范：毛玻璃、`COMBOBOX_STYLE`、宽屏水平优先布局）。  
3. 实现 `process` / 必要时 `process_batch`。  
4. 文件对话框默认目录用桌面：`Path.home() / "Desktop"`。  
5. 功能变更同步 `resources/CHANGELOG.md`。

---

## 4. 输出、范围与续跑

### 4.1 处理范围

- **全部文件** / **仅选中**：全局，对所有功能生效。  
- 主窗口 `_collect_process_files()` / `_collect_process_entries()` 取列表。  
- 仅选中且未选时禁止开始。

### 4.2 输出路径

| 模式 | 行为 |
|------|------|
| 桌面 / 自定义 | 可选 `PixelFlow_output/` 子目录 + **保留目录结构** |
| 原图覆盖 / 副本 | 不启用自动子文件夹与保留结构 |

文件夹导入时列表项带相对路径（`ROLE_REL_PATH`），开始时组成 `rel_path_map`，由 `resolve_file_out_dir()` 解析输出子目录。

### 4.3 批处理续跑 / 重试（`BatchSession`）

- 内存会话：逐文件 `pending / running / success / failed / cancelled`。  
- 快照：参数、输出设置、文件顺序。  
- **继续**：仅用户取消且有未完成时显示。  
- **重试失败**：仅存在 failed 时显示。  
- `is_batch_processor`（合并导出）不支持按文件续跑。  
- 续跑使用 `file_index_map` 保持重命名序号一致。

---

## 5. AI 抠图（透明图核心）

### 5.1 设计目标

1. 主程序 / 打包体积不捆绑 torch。  
2. 模型依赖隔离，可多模型扩展。  
3. 批量场景模型只加载一次。  
4. 大图性能优化且 **原图 RGB 质量不被 AI 管线重编码**（默认路径）。

### 5.2 隔离架构

```
主程序 (PySide6 + Pillow)
        │  stdin/stdout JSON 或 oneshot CLI
        ▼
runtime/envs/<model_id>/.venv/python.exe
        + core/matting/workers/<model>_worker.py
        + models/matting/<model_id>/ 权重
```

| 组件 | 职责 |
|------|------|
| `model_registry.py` | 模型 id、依赖包、worker 脚本、硬件建议 |
| `model_manager.py` | 权重路径、下载、就绪判断、设备偏好 |
| `env_manager.py` | 系统 Python、uv、每模型 venv 创建/校验 |
| `inference.py` | `remove_background` / batch / 常驻会话 |
| `ben2_worker.py` | BEN2：子进程内 load + infer / infer_batch |
| `rmbg2_worker.py` | RMBG 2.0（BiRefNet/transformers）：同上协议 |
| `pipeline.py` | 读图 ∥ 推理 ∥ 后处理流水线 |
| `hardware.py` | CPU/RAM/GPU 兼容性评估 |

**已注册模型：**

| id | 名称 | 来源 | worker | 备注 |
|----|------|------|--------|------|
| `ben2` | BEN2 | ModelScope `PramaLLC/BEN2` | `ben2_worker.py` | 支持边缘精炼 |
| `rmbg2` | RMBG 2.0 | ModelScope `briaai/RMBG-2.0` | `rmbg2_worker.py` | 无 refine；非商用 CC BY-NC |

**配置准备顺序：** 开发环境（Python + uv）→ 创建模型环境 → 下载权重 → 透明图勾选 AI 抠图。

### 5.3 常驻 Worker 协议

启动（各模型 worker 协议相同）：

```text
python <model>_worker.py --serve --device auto --weights-dir ...
→ stdout: {"ok":true,"event":"ready","device":"cuda","vram_gb":8.0,"recommend_batch":2}
```

单张：

```json
{"cmd":"infer","input":"...","output":"...","refine":false,"return_mask":true}
```

批量（真 tensor batch）：

```json
{"cmd":"infer_batch","return_mask":true,"items":[{"input":"...","output":"..."}, ...]}
```

关闭：`{"cmd":"shutdown"}`  
批处理结束 / 取消：`shutdown_matting_workers()`。

### 5.4 性能三阶段

#### 阶段 1 — 预缩放 + 仅回传 Mask（默认，非 refine）

```
原图 RGB（内存保留）
  → LANCZOS 预缩放 1024×1024（对齐 BEN2 内部 rgb_loader）
  → worker 推理，只写单通道 mask.png
  → 主进程 BILINEAR 放大 mask → putalpha 贴回原图
```

- **原图像素不变**；mask 精度与 BEN2 原生 1024 路径同级。  
- 开启 **边缘精炼** 时仍走全尺寸 RGBA（精炼改边缘前景色）。

#### 阶段 2 — GPU 自适应 micro-batch

| 条件 | batch |
|------|-------|
| CPU / 边缘精炼 | 1 |
| VRAM &lt; 4 GB | 1 |
| 4–8 GB | 2 |
| ≥ 8 GB | 3（上限） |
| OOM | 降半，写入本会话 `_batch_caps` |

- Worker 内 `stack` 后一次 `forward`（非官方 list 假 batch）。  
- `BEN2.postprocess_image` 的 `im_size` 为 **(H, W) = (height, width)**，勿传反。

#### 阶段 3 — 三阶段流水线（多图 + 开启抠图）

```
[读图预取线程] → 队列(深度2) → [主线程推理 matting_many]
                                      ↓
                               队列(深度2)
                                      ↓
                         [后处理线程: trim/画布/保存]
```

- `TransparentImageProcessor.matting_many`：只抠图。  
- `_post_matting`：裁边 + 画布布局，在 post 线程执行。  
- 结果按文件顺序 `image_done`；失败可回退串行路径。  
- 单张或不抠图：不走流水线。

### 5.5 透明图后处理算法

**裁剪透明边缘 `trim_transparent`**

- 不用单纯 `alpha.getbbox()`（边缘断裂半透明噪点会撑满 bbox）。  
- 行列投影取最长连续主体 → 收紧精确 bbox；短空隙可桥接。  
- 依赖主环境 `numpy`。

**画布布局 `place_subject_on_canvas`（智能对象式）**

- trim 后全分辨率 RGBA 作为 asset。  
- 导出时预乘 Alpha 后 **一次** 栅格化到画布。  
- 主体按占比完整放入安全框并居中；始终可放大（批量标准化）。  
- 重采样：缩小 scale&lt;0.5 用 BOX，&lt;1 LANCZOS，放大 BICUBIC；细节恢复不锐化 Alpha。  
- 物理限制：画布/显示小于源时放大查看仍丢细节；源分辨率不足无法凭空变清。

### 5.6 后台日志中的 AI 信息

开启 AI 抠图时日志会包含：

- 启动：模型、设备、VRAM、micro-batch、路径（mask / 精炼）、是否流水线  
- 过程：batch 序号、文件范围、推理/后处理耗时  
- 完成行：`抠图→ben2 · batch=2 · CUDA · mask · 流水线`

---

## 6. 元数据编辑要点

| 约定 | 说明 |
|------|------|
| Windows「标记」 | EXIF **XPKeywords (0x9C9E)**，多值 `; ` 分隔；不是画面叠加标签 |
| 中文 | 优先 XP* UTF-16LE；标准 EXIF ASCII 字段仅写纯 ASCII |
| PNG | 同时写 tEXt 与 eXIf，兼顾通用工具与资源管理器 |
| 保存路径 | 元数据类应 `process_batch` 按路径写，避免默认 `img.save` 丢 EXIF |
| 格式识别 | `detect_true_format`（Pillow + 魔数），禁止只看扩展名；**MPO ≠ JPG** |
| 字段表 | `FORMAT_META_FIELDS`：bmp/gif 等无元数据字段时 UI 隐藏 |

---

## 7. 基础处理 — DPI 与压缩

- **DPI**：打印 density 元数据，**不缩放像素**；`img.save(..., dpi=(n,n))`。  
- 与压缩独立：开压缩跟质量；未开压缩时 JPG/WEBP 仍 quality=95；PNG 无损。  
- 仅改 DPI 时不强制转 mode，减少副作用。  
- 格式转换默认可不勾选；开压缩时通常自动需要有损格式。

---

## 8. UI 与配置中心

### 8.1 主界面 Tab

| 索引 | Tab |
|------|-----|
| 0 | 图像处理 |
| 1 | 后台日志 |
| 2 | 配置 |
| 3 | 版本日志 |

配置子菜单：开发环境 (0) → 抠图模型配置 (1)。  
`MainWindow.open_settings(row)` 支持从透明图链接跳转：  
`pixelflow://settings/dev` / `pixelflow://settings/matting`。

**开发环境 · 依赖镜像：**  
`runtime/runtime_settings.json` 字段 `pip_index_url`（默认清华 `https://pypi.tuna.tsinghua.edu.cn/simple`）。  
`RuntimeManager.ensure_model_env` 执行 `uv pip install` 时附加 `-i <url>`，**不**修改用户全局 pip/uv 配置。  
留空则不附加 `-i`。git+https 包（如 BEN2）仍走 Git，不受 PyPI 镜像影响。

**BEN2 安装源：** 代码包 `git+https://github.com/yincangshiwei/BEN2.git`（fork）；权重仍 ModelScope `PramaLLC/BEN2`。

### 8.2 UI 规范摘要

完整规范见项目 rules（毛玻璃深色主题）。要点：

- 半透明 `rgba()` 面板，禁止常驻 `QGraphicsBlurEffect`。  
- 所有 `QComboBox` 使用 `config.COMBOBOX_STYLE`。  
- 宽屏优先：参数横向排列，避免右侧大块留白。  
- 主操作按钮 `objectName="btn_start"`。

### 8.3 预设

- 目录：`presets/<preset_id>/*.json`。  
- `PresetManager` 管理默认 / 用户预设；启动加载 `default.json`。  
- 加载外部预设对话框默认目录可为 `mgr.preset_dir`（例外于「默认桌面」规则）。

---

## 9. 打包注意

- `PixelFlow.spec` 的 `excludes` 只排除**确认无用**的标准库。  
- 不可排除被间接依赖的模块，例如：  
  - `http` / `email` / `xml` — python-pptx、python-docx、openpyxl 等需要  
- 打包需包含 `core/matting/workers/`。  
- 不确定时 **宁可保留、不排除**，避免运行时 `ModuleNotFoundError`。

---

## 10. 扩展 AI 模型清单

新增抠图模型时：

1. 在 `model_registry.py` 注册：`id`、依赖、`worker_script`、权重文件名、硬件建议。  
2. 实现 `core/matting/workers/<name>_worker.py`（支持 `--serve` + JSON 更佳）。  
3. 配置页可创建环境、下载权重。  
4. 透明图模型下拉自动来自 `list_models()`。  
5. 若支持 batch，在 worker 实现 `infer_batch` 并在 ready 中上报 `recommend_batch`。

---

## 11. 关键 API 速查

| API | 位置 | 说明 |
|-----|------|------|
| `remove_background(img, ...)` | `inference.py` | 单张抠图 → RGBA |
| `remove_background_batch(imgs, ...)` | `inference.py` | 批量 + 自适应 batch |
| `ensure_matting_session_ready(id)` | `inference.py` | 预热常驻进程 |
| `recommend_matting_batch_size(...)` | `inference.py` | 建议 batch |
| `shutdown_matting_workers()` | `inference.py` | 关闭全部会话 |
| `run_matting_pipeline(...)` | `pipeline.py` | 三阶段流水线 |
| `trim_transparent(img, thr)` | `image_processor.py` | 稳健裁透明边 |
| `place_subject_on_canvas(...)` | `image_processor.py` | 智能对象式布局 |
| `detect_true_format(path)` | `metadata_utils.py` | 真实格式 |
| `resolve_file_out_dir(...)` | `worker.py` | 输出子目录 |

---

## 12. 开发约束（与 AI 协作时）

项目规则中与实现强相关的约定：

1. **不擅自** `pip install` / 启动项目，由用户自行运行。  
2. 数据库类变更只提供 SQL，不直接改库（若未来引入 DB）。  
3. 功能增删改同步 `resources/CHANGELOG.md`（按日期）。  
4. `QFileDialog` 默认目录为用户桌面（预设加载除外）。  
5. UI 遵循毛玻璃与宽屏布局规范。

---

## 13. 修订说明

本文档随架构演进更新。重大行为变更请同时：

1. 修改对应代码与注释  
2. 更新 `resources/CHANGELOG.md`  
3. 同步修订本节相关章节  

*文档对应实现阶段：处理器插件架构 · 批处理续跑 · AI 抠图阶段 1/2/3 · 元数据与透明图布局。*
