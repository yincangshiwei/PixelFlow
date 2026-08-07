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
- 主窗口壳：`ui/shell/main_window.py`（只装配路由与连线）  
- 配置中心：`ui/routes/settings/`（SettingsRoute / DevEnvRoute / MattingModelRoute）  
- 全局常量 / 样式：`config.py`、`ui/shell/styles.py`

### 分层与依赖规则（Route → Service → core 单向依赖）

- `ui/`：壳（`ui/shell/`）+ 页面路由（`ui/routes/`）+ 通用控件（`ui/widgets/`）+ Qt 平台适配（`ui/adapters/`）；Route 只与 Service 交互，不直接读写 core
- `services/`：不依赖 Route / QWidget。`services/features/`（功能 Service + catalog 权威注册）、`services/common/`（编排 / 预设 / 输出路径 / 导入）、`services/contracts/`（跨层契约）
- `core/`：纯处理逻辑，不 import ui

**硬性规则**（由 `tests/test_dependency_rules.py` 静态检查守护）：

| 规则 | 说明 |
|------|------|
| Route → Service → core 单向 | 禁止反向依赖；core 不 import `ui` 包 |
| services 不 import QtWidgets | `services/common/batch_orchestrator.py` 为唯一白名单（主线程 QObject 编排，允许 QtCore） |
| 处理器不 import PySide6 | 面板/参数收集在 FeatureRoute / FeatureService |
| Qt 剪贴板只在 `ui/adapters/clipboard_adapter.py` | 导入纯逻辑在 `services/common/importing/` |
| 壳不保存跨页面业务状态 | `ui/shell/` 只装配与连线；业务状态归 services |

### 跨层契约（`services/contracts/`）

| 契约 | 用途 |
|------|------|
| `ImportEntry` / `ImportCollection` | 导入条目与集合快照（列表项仅存条目 id） |
| `FeatureDescriptor` | 功能统一描述符（id 即 preset_id / 输入形态 / 工厂 / 能力声明） |
| `RunRequest` / `OutputPolicy` | 批处理请求与输出策略 |
| `JobEvent` | 批处理事件（**必带 `job_id`**，状态机：queued → running → finished/cancelled/failed） |
| `BatchSnapshot` | 断点续跑快照 |
| `AppContext` | 只读依赖容器（`ui/shell/app_context.py`，不依赖 Qt） |

### Qt 线程与生命周期约定

- **Worker 归属**：`BatchOrchestrator`（主线程 QObject）持有 Worker 直至 `finished`，禁止提前 delete；完成弹窗在 `QThread.finished` 之后
- **事件隔离**：所有 `JobEvent` 携带 `job_id`，编排器丢弃迟到事件，避免上一轮任务串扰
- **后台回调守卫**：UI 侧异步回调统一经 `_AsyncBridge` + `shiboken6.isValid` 守卫，防止控件已销毁时回调崩溃
- **关闭策略**：closeEvent 按「禁止新任务 → 请求取消 → 有界等待（15s）→ 超时则 ignore」执行

---

## 2. 目录结构（逻辑）

```
PixelFlow/
├── app.py                      # 入口
├── config.py                   # 路径、样式、常量
├── core/                       # 处理核心（纯逻辑，不 import ui）
│   ├── base_processor.py       # 图像处理器基类（纯处理，无 UI）
│   ├── base_file_processor.py  # 文件类处理器基类（预留）
│   ├── worker.py               # 图像批处理 QThread
│   ├── file_worker.py          # 文件批处理 QThread
│   ├── batch_session.py        # 续跑 / 重试会话
│   ├── image_processor.py      # 通用图像算法（裁边、画布等）
│   ├── metadata_utils.py       # EXIF / tEXt / 格式识别
│   ├── preset_manager.py       # 预设 JSON 读写
│   ├── log_manager.py          # 后台日志落盘
│   ├── processors/             # 功能处理器（插件）
│   ├── matting/                # AI 抠图
│   │   ├── inference.py        # 主进程推理封装
│   │   ├── pipeline.py         # 三阶段流水线
│   │   ├── model_registry.py   # 模型元数据
│   │   ├── model_manager.py    # 权重 / 就绪
│   │   ├── hardware.py         # 硬件评估
│   │   └── workers/            # 子进程脚本（在隔离 venv 中跑）
│   └── runtime/
│       ├── env_manager.py      # uv / Python / Git / 模型 venv / PATH 同步
│       └── gpu_catalog.py      # 主流 GPU ↔ PyTorch CUDA 标签清单
├── services/                   # 服务层（不依赖 Route / QWidget）
│   ├── contracts/              # 跨层契约（ImportEntry / FeatureDescriptor / RunRequest / JobEvent …）
│   ├── features/               # 功能 Service + catalog（功能注册唯一权威入口）
│   └── common/                 # 编排（BatchOrchestrator）/ 预设 / 输出路径 / 导入
├── ui/                         # 表现层（Route → Service → core）
│   ├── shell/                  # 主窗口壳 / AppContext / 全局样式 / 导入协调
│   ├── routes/                 # 页面路由（file_list / process / settings / log / changelog）
│   ├── widgets/                # 通用控件
│   └── adapters/               # Qt 平台适配（剪贴板等）
├── models/matting/<id>/        # 模型权重（本地数据）
├── runtime/                    # AI 运行时数据（gitignore）
│   ├── uv/                     # 便携 uv.exe（可一键安装或手动放置）
│   ├── envs/<id>/.venv/        # 每模型隔离环境（Python 严格 3.10–3.12）
│   └── runtime_settings.json   # python_path / uv_path / pip_index_url / github_proxy
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

## 3. 插件化功能架构（Route → Service → core）

### 3.1 图像处理器（`BaseProcessor`，纯处理）

每个功能一个类，放在 `core/processors/`。处理器**只做处理逻辑**，不创建面板、不读控件。

**必须实现：**

| 接口 | 作用 |
|------|------|
| `name` / `description` / `icon` / `preset_id` | 展示与预设目录名（`preset_id` 同时作为功能 id） |
| `default_options()` | 出厂默认参数 |
| `process(img, options) -> (Image, details)` | 单张处理 |

**可选：**

| 接口 | 作用 |
|------|------|
| `is_batch_processor = True` | 走 `process_batch`（合并导出类） |
| `process_many` / `matting_many` | micro-batch / 流水线 |
| `preferred_matting_batch_size` | 建议 GPU batch |
| `set_base_image_size(w, h)` | 运行期上下文（如叠加的宫格坐标定位） |

### 3.2 功能注册与参数流（FeatureDescriptor / Service / Route）

功能的唯一权威注册入口是 `services/features/catalog.py`：每个功能由 `FeatureDescriptor`（id / 名称 / 输入形态 / 工厂 / 能力声明）描述，菜单顺序、预设、编排均读取同一注册表，不再有多份功能清单。

| 组成 | 位置 | 职责 |
|------|------|------|
| FeatureRoute | `ui/routes/process/features/` | 参数面板 QWidget；`collect_raw_state()` 收集原始控件状态、`apply_state(state)` 应用预设态 |
| FeatureService | `services/features/` | `validate_and_normalize()` 规范化参数；`normalize_preset()` 预设态转换；`build_run_options()` 构建运行参数（`_output_format` / `keep_matting`）；`load_selected()` 选中图回读（如元数据） |
| FeatureDescriptor | `services/contracts/` | 能力声明：`input_kind`（IMAGE / FILE / BATCH_MERGED）、`supports_resume`、`supports_selected_load` |

参数流：`Route.collect_raw_state() → Service.validate_and_normalize() → options → Service.build_run_options() → RunRequest`。

### 3.3 批处理数据流

```
ActionBarRoute（开始处理）
  → ProcessTabRoute.build_run_options()（Route 收集 + Service 规范化）
  → BatchOrchestrator（主线程 QObject，持 Worker 至 finished；事件带 job_id）
  → ProcessWorker(QThread)
       ├─ is_batch_processor → process_batch(...)
       ├─ AI 抠图多图 → matting pipeline（见 §5）
       └─ 默认 → 逐张 process() → 保存
  → JobEvent 信号 → 后台日志 + 列表状态 + 进度条
  → QThread.finished → 结算弹窗（须等 AI 子进程关闭）
```

**线程安全约定：**

- Worker **不得**访问 QComboBox 等 UI 控件；输出格式在启动前写入 `options["_output_format"]`。
- 取消只置 `_cancelled`；常驻抠图进程在 Worker 线程 `finally` 中 `shutdown_matting_workers()`。
- 完成弹窗在 `QThread.finished` 之后，避免线程未退出即销毁导致崩溃。
- 事件统一携带 `job_id`，编排器丢弃迟到事件，避免上一轮任务串扰。

### 3.4 当前功能一览

| 模块 | 处理器 | Service / Route | 类型 |
|------|--------|-----------------|------|
| 透明图处理 | `transparent_processor.py` | `transparent_service` / `transparent_route` | 逐张 + AI 流水线 |
| 基础处理 | `basic_processor.py` | `basic_service` / `basic_route` | 逐张（格式/压缩/DPI/重命名） |
| 元数据编辑 | `metadata_processor.py` | `metadata_service` / `metadata_route` | `process_batch` 按路径写，保留 EXIF |
| 图片叠加 | `overlay_processor.py` | `overlay_service` / `overlay_route` | 逐张 |
| 图片排版导出 | `img2doc_processor.py` | `img2doc_service` / `img2doc_route` | `is_batch_processor`（PPT/PDF/Word） |

### 3.5 新增功能步骤（简版）

1. 在 `core/processors/` 新建处理器类，继承 `BaseProcessor`（纯处理，不含控件）。  
2. 在 `services/features/` 新建 FeatureService（参数规范化 / 预设态转换 / `build_run_options`），并暴露 `descriptor`。  
3. 在 `ui/routes/process/features/` 新建 FeatureRoute 面板（遵循 UI 规范：毛玻璃、`COMBOBOX_STYLE`、宽屏水平优先布局），实现 `collect_raw_state()` / `apply_state()`。  
4. 在 `services/features/catalog.py` 的 `_SERVICE_TYPES` 与 `EXPECTED_FEATURE_ORDER` 注册（菜单顺序以 catalog 为准），并在 `ProcessTabRoute` 注入 Route 工厂。  
5. 实现 `process` / 必要时 `process_batch`；文件对话框默认目录用桌面：`Path.home() / "Desktop"`。  
6. 打包时在 `PixelFlow.spec` 的 `hiddenimports` 补充新处理器模块；功能变更同步 `resources/CHANGELOG.md`。

---

## 4. 输出、范围与续跑

### 4.1 处理范围

- **全部文件** / **仅选中**：全局，对所有功能生效。  
- 处理条目取自 `ImportCollection` 快照（`services/common/importing/`），列表项仅存条目 id。  
- 仅选中且未选时禁止开始。

### 4.2 输出路径

| 模式 | 行为 |
|------|------|
| 桌面 / 自定义 | 可选 `PixelFlow_output/` 子目录 + **保留目录结构** |
| 原图覆盖 / 副本 | 不启用自动子文件夹与保留结构 |

文件夹导入时条目带相对路径（`ImportEntry.relative_path`），由 `OutputPathService.resolve_file_out_dir()` 解析输出子目录。

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
| `model_registry.py` | 模型 id、依赖包、`python_version`、worker 脚本、硬件建议 |
| `model_manager.py` | 权重路径、下载、就绪判断、设备偏好 |
| `env_manager.py` | 系统 Python、uv、Git、PATH 刷新、每模型 venv 创建/校验、PyTorch CUDA/CPU 安装 |
| `gpu_catalog.py` | 主流 NVIDIA 系列 ↔ 优选/备选 `cuXXX` 标签与驱动门槛 |
| `inference.py` | `remove_background` / batch / 常驻会话 |
| `ben2_worker.py` | BEN2：子进程内 load + infer / infer_batch |
| `rmbg2_worker.py` | RMBG 2.0（BiRefNet/transformers）：同上协议 |
| `pipeline.py` | 读图 ∥ 推理 ∥ 后处理流水线 |
| `hardware.py` | CPU/RAM/GPU 兼容性评估 |

**已注册模型：**

| id | 名称 | 来源 | worker | 备注 |
|----|------|------|--------|------|
| `ben2` | BEN2 | ModelScope `PramaLLC/BEN2` | `ben2_worker.py` | 支持边缘精炼；代码包 git+https fork |
| `rmbg2` | RMBG 2.0 | ModelScope `briaai/RMBG-2.0` | `rmbg2_worker.py` | 无 refine；非商用 CC BY-NC |

**配置准备顺序：** 开发环境（uv 必需；本机 Python 3.10–3.12 可选）→ 创建模型环境 → 下载权重 → 透明图勾选 AI 抠图。

### 5.2.1 模型 venv 与 Python 版本（重要）

| 约定 | 说明 |
|------|------|
| 目标版本 | 注册表 `python_version`，默认 **3.12**；允许范围 **仅 3.10–3.12** |
| 创建方式 | 优先 `uv venv --python 3.12`（uv 可**托管下载** CPython）；本机已有精确 3.12 时可用其路径 |
| 禁止 | 不得用本机 **3.13 / 3.14** 作 base：当前 PyTorch 官方 wheel 无 `cp313`/`cp314`，会报 ABI 不匹配 |
| 历史环境 | 已有 venv 若版本越界，「创建/修复」会**自动删建**，不必只靠强制重建 |
| `resolve_base_python` | **不再**放宽到 `>3.12`（曾导致有 CUDA 却误退 CPU） |

**PyTorch 安装策略（`ensure_model_env`）：**

1. `nvidia-smi` 探测 GPU → `gpu_catalog.match_gpu` 得优选/备选 `cuXXX`。  
2. 按候选标签从 PyTorch 官方索引安装 CUDA 版；失败再试下一标签。  
3. 全部 CUDA 失败 → 回退 CPU 版；日志区分 **ABI/网络/架构** 与「GPU 未匹配」（勿混为一谈）。  
4. 安装后 `probe_torch_build`：校验 import、cuda 构建、以及 wheel 是否含本机 compute capability（如 sm_120）。

### 5.2.2 开发工具检测与 PATH 同步

用户在 **PixelFlow 运行期间** 安装 Git / 手动放置 uv 时，安装器只更新注册表 PATH，进程内仍是启动时的旧 PATH。因此：

| API | 作用 |
|-----|------|
| `refresh_process_path()` | 从 Windows 注册表合并最新用户/系统 PATH 到 `os.environ` |
| `ensure_exe_dir_on_path(exe)` | 将已找到的 git/uv 目录前置进 PATH，供后续 `uv pip` / 子进程 `which` |
| `resolve_uv(force=True)` / `resolve_git(force=True)` | 刷新 PATH + 清空 probe 缓存后重探 |
| `invalidate_caches(uv=…, git=…)` | 同时清实例缓存与模块级 `_uv_probe_cache` / `_git_probe_cache` |

**UI：** 开发环境提供「重新检测 uv」「重新检测 Git」「重新检测 Python」；手动安装后**一般无需重启应用**。  
**创建/修复环境前：** 强制 `resolve_uv(force=True)`、`resolve_git(force=True)`，与开发环境页状态同源，避免「页面显示已就绪、创建时却报未安装」。

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

**裁剪透明边缘 `trim_transparent`（`core/image_processor.py`）**

- 不用单纯 `alpha.getbbox()`（边缘断裂半透明噪点会撑满 bbox）。  
- **常规策略（多物品安全）：** 对 `alpha > alpha_threshold` 的行列投影桥接主体内部细缝，收集所有「显著连续段」取**并集**，再在并集范围内收紧精确 bbox。  
  - 显著段：长度 ≥ max(3px, 最长段 × 12%)，滤掉四角短噪点。  
  - 短空隙桥接：约 0.15% 边长，限制在 1–8px。  
- **边界低 Alpha 底噪处理：** AI mask 缩放后，Alpha 1~几十的残留可能沿整条图像边界铺开，使常规投影仍覆盖整图。满足以下条件时，改用 `alpha > max(alpha_threshold, 64)` 的可信掩码定位：  
  1. 用户阈值掩码已触碰图像边界；  
  2. 边界上没有 Alpha 大于可信阈值的像素（说明是低透明底噪，而不是真实物体贴边）；  
  3. 图内存在可信内容。  
- 可信掩码仍经过「所有显著段并集」，因此双物品/多物品都会保留；得到范围后向四周外扩一个 `bridge`，保留少量抗锯齿半透明边缘。最终仍用用户阈值掩码在候选范围内收紧。  
- 若边界存在可信 Alpha 内容，视为真实贴边物体，继续使用常规策略，不启用底噪过滤。  
- **历史坑：** 曾只取「最长连续投影段」。双物品中间有透明缝时会只保留一侧主体（另一侧被裁掉）。多 SKU / 对放产品图必须用并集，禁止再改回 longest-only。  
- 用户阈值 `alpha_threshold` 始终生效；依赖主环境 `numpy`。

**画布布局 `place_subject_on_canvas`（智能对象式）**

- trim 后全分辨率 RGBA 作为 asset。  
- 导出时预乘 Alpha 后 **一次** 栅格化到画布。  
- 主体按占比完整放入安全框并居中；始终可放大（批量标准化）。  
- 重采样：缩小 scale&lt;0.5 用 BOX，&lt;1 LANCZOS，放大 BICUBIC；细节恢复不锐化 Alpha。  
- 物理限制：画布/显示小于源时放大查看仍丢细节；源分辨率不足无法凭空变清。

**画布颜色 `_ColorBlock` / `hex_to_rgba`（易踩坑）**

| 约定 | 说明 |
|------|------|
| 存储格式 | `#RRGGBB` 或不透明省略 Alpha；半透明/透明为 `#RRGGBBAA`（与 PIL 一致） |
| 默认值 | 不透明白 `#FFFFFF`（`default_options` / 面板初始） |
| Qt StyleSheet | 8 位 hex 按 **`#AARRGGBB`** 解析，色块显示须经 `rgba_to_css_hex`，禁止直接把存储串塞进 `background:` |
| 取色对话框 | `QColorDialog` + `ShowAlphaChannel` + **`DontUseNativeDialog`**（规避部分 Windows 原生框 Alpha 异常） |
| 全透明再选色 | 当前 Alpha=0 时，对话框**初始 Alpha 提到 255**，避免用户只点色板得到 `#FFFFFF00`（透明白，看起来像没生效） |
| 透底需求 | 取色框把 Alpha 拉到 0，或点自定义快捷「全透明」→ `#00000000` |
| 旧值兼容 | 曾用 `QColor.name(HexArgb)` 写入 `#AARRGGBB`；`hex_to_rgba` 对「末字节=FF 且引导字节&lt;FF」的 8 位串按 ARGB 解（如 `#00FFFFFF`） |

相关 API：`hex_to_rgba` / `rgba_to_hex` / `rgba_to_css_hex`（`image_processor.py`）；UI 在 `transparent_processor._ColorBlock`。

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

**开发环境门禁（进入抠图模型配置页）：**

| 条件 | 是否可进入模型配置 |
|------|-------------------|
| 已检测到 **uv** | 是（可无本机 3.10–3.12；创建 venv 时由 uv 托管下载 Python） |
| 无 uv | 否；引导安装 uv 或本机 Python 3.10–3.12 |
| Git | **不**作为门禁；仅在创建含 `git+https` 的模型环境时校验 |

**开发环境 · uv：**

- 一键安装到 `runtime/uv/`（Windows：GitHub releases zip；优先走 GitHub 代理）。  
- **重新检测 uv**：`refresh_process_path` + 清缓存后重探；支持用户手动把 `uv.exe` 放到 `runtime/uv/` 后立即生效。  
- 探测顺序：已保存路径 → `runtime/uv/uv.exe` → PATH / `where`。

**开发环境 · 依赖镜像：**  
`runtime/runtime_settings.json` 字段 `pip_index_url`（默认清华 `https://pypi.tuna.tsinghua.edu.cn/simple`）。  
`RuntimeManager.ensure_model_env` 对**非 torch** 包执行 `uv pip install -i <url>`；CUDA 版 torch 走官方 `https://download.pytorch.org/whl/cuXXX` 索引，**不**与普通 PyPI 混用（避免装成 `+cpu`）。  
**不**修改用户全局 pip/uv 配置。留空则非 torch 包不附加 `-i`。

**开发环境 · Git / GitHub 代理：**

- 检测：刷新后的 PATH + `where git` + Windows 常见安装路径（`Program Files\Git\cmd` 等）。  
- **重新检测 Git**：刷新注册表 PATH 并注入 git 目录；装完 Git **一般无需重启** PixelFlow。  
- BEN2 等 `git+https`：创建环境前 `resolve_git(force=True)`；失败提示引导「重新检测」而非笼统要求重启。  
- `runtime_settings.json` 字段 `github_proxy`（默认 `https://ghfast.top/`，留空=直连）。  
- 安装时改写 `git+https://github.com/...` 为代理前缀，并通过临时 `GIT_CONFIG_*` insteadOf 注入子进程，**不**改用户全局 gitconfig；同时把当前（已刷新的）`PATH` 并入该子进程 env。  
- 下载 uv（GitHub releases）时同样优先走该代理。  
- git+https 包不受 PyPI 镜像影响，需 Git +（可选）GitHub 代理。

**开发环境 · 其它：**

- **VC++ 2015–2022 x64**：Windows 上 torch 原生扩展依赖；过旧/损坏常见 `WinError 1114` / `c10.dll`。提供检测与下载入口；修复后通常需**重启电脑**（与 Git/uv 的「无需重启应用」不同）。  
- **GPU 匹配 / AI 帮装**：按 `gpu_catalog` 展示系列与 CUDA 标签建议；可生成可复制的帮装说明（通用流程 + 本机快照附录）。

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

1. 在 `model_registry.py` 注册：`id`、依赖、`python_version`（建议 `"3.12"`，勿超 3.12）、`worker_script`、权重文件名、硬件建议。  
2. `env_packages` 中的 `torch`/`torchvision` 由 `RuntimeManager` 按 GPU 选 CUDA/CPU 索引安装，勿写死 `+cuXXX` 到普通 PyPI 规格。  
3. 若依赖 `git+https`，创建环境会要求本机 Git；注册表注明即可。  
4. 实现 `core/matting/workers/<name>_worker.py`（支持 `--serve` + JSON 更佳）。  
5. 配置页可创建环境、下载权重；透明图模型下拉自动来自 `list_models()`。  
6. 若支持 batch，在 worker 实现 `infer_batch` 并在 ready 中上报 `recommend_batch`。

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
| `get_runtime_manager()` | `env_manager.py` | 运行时单例 |
| `RuntimeManager.ensure_model_env(...)` | `env_manager.py` | 创建/修复隔离 venv + 装依赖 |
| `RuntimeManager.resolve_uv/git(force=)` | `env_manager.py` | 探测 uv / Git（force 刷新 PATH） |
| `refresh_process_path()` | `env_manager.py` | 注册表 PATH → 当前进程 |
| `ensure_exe_dir_on_path(exe)` | `env_manager.py` | 可执行目录注入 PATH |
| `plan_torch_install()` / `match_gpu(...)` | `env_manager` / `gpu_catalog` | CUDA/CPU 安装方案 |

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
