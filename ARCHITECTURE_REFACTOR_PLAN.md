# PixelFlow 架构优化计划

> **文档性质**：UI / 应用层分层重构的执行计划、架构契约与验收标准  
> **创建日期**：2026-08-06  
> **最近修订**：2026-08-06（架构评审后修订）  
> **状态总览**：未开始  
> **关联文档**：[README.md](README.md) · [TECHNICAL.md](TECHNICAL.md)

---

## 0. 进度总览

| 阶段 | 名称 | 状态 | 计划产出 | 完成日期 |
|------|------|------|----------|----------|
| P-1 | 基线、契约与测试护栏 | ⬜ 未开始 | DTO · `FeatureDescriptor` · 依赖/线程契约 · 纯逻辑基线测试 | — |
| P0 | 导入逻辑与 Qt 适配分离 | ⬜ 未开始 | import / extract 纯逻辑 · Clipboard Adapter · 临时文件管理 | — |
| P1 | 文件列表状态模型 + 独立路由 | ⬜ 未开始 | `ImportCollection` · `FileListRoute` · widgets | — |
| P2 | 功能契约试点 + 处理 Tab / 预设 | ⬜ 未开始 | Basic 试点 · `ProcessTabRoute` · `PresetService` · Legacy Adapter | — |
| P3 | 通用批处理编排 + 输出策略 | ⬜ 未开始 | `BatchOrchestrator` · `RunRequest` · `OutputPolicy` · 任务状态机 | — |
| P4 | 其余功能级 Route / Service | ⬜ 未开始 | metadata / overlay / img2doc / transparent 迁移 | — |
| P5 | 应用壳、独立页面与配置中心 | ⬜ 未开始 | 瘦壳 · log / changelog · dev / matting 子路由 | — |
| P6 | 兼容清理、打包与文档收尾 | ⬜ 未开始 | 删除双轨 · 打包验证 · 文档与扩展指南 | — |

**状态图例**：⬜ 未开始 · 🔵 进行中 · ✅ 已完成 · ⏸ 暂停 · ❌ 取消

**整体完成度**：`0 / 8` 阶段

更新本表规则：

1. 某阶段开始实施时，将状态改为 🔵，并在 §8 进度日志追加一条。
2. 某阶段通过 §7 对应验收清单后，改为 ✅，填写完成日期。
3. 若中途调整范围，在 §8 记录原因，并同步改本节表格。

---

## 1. 背景与目标

### 1.1 现状问题

| 问题 | 表现 | 影响 |
|------|------|------|
| 上帝类窗口 | `ui/main_window.py` ≈ **3748 行** | 难读、难测、易回归 |
| 逻辑与视图耦合 | HTML/DOCX/PDF 抽图、粘贴解析、批处理编排全在窗口内 | 无法复用、无法单测 |
| 配置巨石 | `ui/settings_panel.py` ≈ **2143 行** | 开发环境与抠图配置缠在一起 |
| 扩展成本高 | 新功能仍可能改 `MainWindow` | 违背已有 Processor 插件化初衷 |
| 后端已分层、UI 未分层 | `core/processors` 插件化较好 | 架构不对称 |

### 1.2 已有可保留能力（不推倒重来）

- `BaseProcessor` / `BaseFileProcessor` 插件注册
- `ProcessWorker` / `FileProcessWorker` / `BatchSession`
- `core/matting/*`、`core/runtime/*` AI 隔离架构
- `PresetManager`、`AppLogManager`
- 毛玻璃 UI 规范与 `config.py` 样式常量

### 1.3 重构目标

将桌面应用按 **Java MVC 思路** 拆成：

| 概念 | PixelFlow 落地 | 职责 |
|------|----------------|------|
| **Route / Controller** | `ui/routes/*` | 创建页面、读写控件、连接局部信号；将原始状态转成普通 Python 数据，**不写业务规则** |
| **功能 Service** | `services/features/*` | 校验 / 规范化功能参数、预设迁移、构建功能运行参数；**不得依赖具体 Route / QWidget** |
| **通用应用 Service** | `services/common/*` | 导入、输出策略、批处理编排、预设、日志门面 |
| **领域 / 基础设施** | `core/*` | 图像算法、处理器、Qt Worker、抠图、运行时；迁移期间明确纯逻辑与 Qt 基础设施边界 |
| **应用壳 / Composition Root** | `ui/shell/main_window.py` + `app.py` | 布局骨架、显式创建依赖、挂载路由；不保存跨页面业务状态 |

**成功标准（全局）**：

1. 依赖方向稳定：`Route → Service → core`；Service 不 import 具体 Route / QWidget，Processor 不读取控件，Orchestrator 不操作 QWidget。
2. `MainWindow`（壳）目标 **≤ 300 行**（辅助指标，不以机械搬行数代替解耦）。
3. 新增功能不改壳与 `BatchOrchestrator` 核心，只需 FeatureDescriptor + Processor + FeatureRoute + FeatureService；图片 / 文件处理器均有扩展路径。
4. 通用能力（导入 / 输出 / 开始处理 / 续跑）变更集中在 `services/common` 与其基础设施适配器。
5. 关键跨层数据使用明确 DTO / 状态模型，不以 QWidget、QListWidgetItem 或散落的魔法字典键作为唯一真相。
6. 行为与重构前一致（见各阶段验收清单）；无明显性能回退。
7. 每阶段具备纯逻辑自动化测试或契约检查，并执行 import / PyInstaller 收集冒烟；P6 再做完整打包。
8. Qt 后台任务具备统一所有权、`job_id`、取消 / 关闭 / 迟到事件处理约定。

### 1.4 非目标（本计划不做）

- 不重写 AI 抠图隔离架构（`core/matting`、`core/runtime`）
- 不更换 GUI 框架、不引入 Web 前端
- 不改变用户可见功能集合（除非顺带修明确 bug）
- 不强制一次 PR 完成全部阶段（按 P-1→P6 增量合并）

### 1.5 约束（必须遵守）

- 遵循项目 `.codebuddy/rules`：UI 毛玻璃规范、宽屏布局优先、`COMBOBOX_STYLE`、文件对话框默认桌面路径等
- **禁止**擅自 `pip install` / 启动项目（由开发者本地验证）
- 数据库类改动不适用本项目；预设仍为 JSON 文件
- 纯内部搬迁、无用户可见变化时不强制更新 `resources/CHANGELOG.md`；若带来用户可感知的稳定性、性能或扩展机制变化则按项目规则记录
- 重构以 **行为等价** 为第一优先级，避免「顺手大改业务」

---

## 2. 目标目录结构

```text
PixelFlow/
├── app.py
├── config.py
├── ARCHITECTURE_REFACTOR_PLAN.md    # 本计划（进度与验收）
│
├── core/                            # 领域层（尽量少 Qt）
│   ├── processors/                  # 图像/文件变换（逐步去掉 create_panel 内的 UI）
│   ├── matting/ · runtime/ · worker.py · ...
│   └── ...
│
├── services/                        # 应用逻辑层（不依赖具体 QWidget / Route）
│   ├── __init__.py
│   ├── contracts/                   # 跨层稳定契约 / DTO
│   │   ├── import_entry.py
│   │   ├── feature_descriptor.py
│   │   ├── output_policy.py
│   │   ├── run_request.py
│   │   └── job_event.py
│   ├── common/
│   │   ├── importing/               # 纯解析 / 抽取 + Qt Clipboard Adapter 分层
│   │   ├── file_import_service.py
│   │   ├── output_path_service.py
│   │   ├── batch_orchestrator.py
│   │   ├── worker_factory.py
│   │   ├── preset_service.py
│   │   └── log_service.py
│   └── features/
│       ├── legacy_adapter.py        # 兼容期包装旧 Processor UI API
│       ├── transparent_service.py
│       ├── basic_service.py
│       ├── metadata_service.py
│       ├── overlay_service.py
│       └── img2doc_service.py
│
└── ui/
    ├── shell/
    │   ├── main_window.py           # 瘦壳
    │   ├── app_context.py           # 只读依赖容器（禁止 Service Locator / 业务状态）
    │   ├── feature_registry.py      # 统一 FeatureDescriptor 注册表
    │   └── styles.py                # 原 _apply_style
    ├── widgets/
    │   ├── gradient_background.py
    │   ├── drop_list_widget.py
    │   └── ...
    └── routes/
        ├── file_list/
        ├── process/
        │   ├── process_tab_route.py
        │   ├── output_settings_route.py
        │   ├── action_bar_route.py
        │   └── features/            # 每功能一个路由
        ├── log/
        ├── settings/
        └── changelog/
```

> **迁移策略**：优先「纯逻辑抽离 + 稳定契约 + Legacy Adapter」，避免改算法语义。旧路径可暂时 re-export，但注册副作用只能保留一个权威入口，待稳定后再删。

### 2.1 强制依赖规则

```text
ui/routes ──→ services ──→ core pure API
    │              │
    └──→ Qt adapters / WorkerFactory

禁止：services → 具体 Route / QWidget
禁止：Processor → Route 或读取控件
禁止：BatchOrchestrator → QWidget / QListWidgetItem
禁止：core 纯逻辑模块 → ui
```

- `AppContext` 仅保存构造时注入的依赖，不提供字符串查找，不保存当前页面、当前控件或批处理可变状态。
- 跨区域事件按 `SelectionEvents`、`BatchEvents`、`NavigationEvents`、`LogEvents` 拆分为小型 typed signals；能显式连接时不使用全局广播。
- `Route` 在本文中表示“UI 区域 Controller”，不是 Web URL 路由。

### 2.2 核心数据契约

| 契约 | 核心字段 / 用途 |
|------|-----------------|
| `ImportEntry` | `entry_id`、绝对路径、相对路径、来源类型、去重键；作为文件列表唯一业务真相 |
| `FeatureDescriptor` | id/name/icon、输入类型、Processor/Route/Service 工厂、续跑与回读能力 |
| `OutputPolicy` | 路径模式、根目录、自动子目录、保留结构、原图覆盖、同名覆盖 |
| `RunRequest` | `job_id`、feature id、输入快照、规范化 options、OutputPolicy、原始序号 |
| `JobEvent` / `JobResult` | 携带 `job_id` 的进度、单项结果、日志和终态事件 |
| `BatchSnapshot` | 续跑所需的不可变请求快照 + 每项状态；由现有 `BatchSession` 演进 |

- 功能 options 兼容期可以保留 `dict`，但 `_output_format`、`_overwrite`、`_rel_path_map` 等跨层控制字段应逐步进入 DTO，不再新增魔法键。
- DTO 优先使用 `dataclass` + `Enum`，不得携带 QWidget、QModelIndex、QListWidgetItem 等 UI 对象。

---

## 3. 路由与 Service 对照表

### 3.1 通用路由

| 路由 ID | 页面 | 绑定 Service | 主要迁出源 |
|---------|------|--------------|------------|
| `file_list` | 左侧图片列表 | FileImport / ContainerExtract / Clipboard | `main_window` 左栏 + 导入/粘贴 |
| `process_tab` | 图像处理 Tab 容器 | Preset + 当前 Feature | 功能下拉、预设栏、面板栈 |
| `output_settings` | 输出设置 | OutputPath / ProcessScope | 路径模式、范围、保留结构等 |
| `action_bar` | 开始/取消/续跑/进度 | **BatchOrchestrator** | `_begin_process` 及 Worker 回调 |
| `log` | 后台日志 | LogService | 日志 Tab、清空、配置日志接入 |
| `settings` | 配置壳 | 子路由 | `open_settings`、懒加载 |
| `settings.dev` | 开发环境 | Runtime 门面 Service | `settings_panel` 开发页 |
| `settings.matting` | 抠图模型 | MattingModel 门面 Service | `settings_panel` 模型页 |
| `changelog` | 版本日志 | （只读文件即可） | `_load_changelog` |

### 3.2 功能路由

| 路由 ID | 功能 | Feature Service | 现有 Processor |
|---------|------|-----------------|----------------|
| `feature.transparent` | 透明图处理 | TransparentService | `transparent_processor` |
| `feature.basic` | 基础处理 | BasicService | `basic_processor` |
| `feature.metadata` | 元数据编辑 | MetadataService | `metadata_processor` |
| `feature.overlay` | 图片叠加 | OverlayService | `overlay_processor` |
| `feature.img2doc` | 排版导出 | Img2DocService | `img2doc_processor` |

### 3.3 跨 Route 协作（显式连接 + 小型事件接口）

```text
FileListRoute 选中变化
  → SelectionEvents.selection_changed(entry_id, path)
  → 当前 Feature Controller 调 FeatureService.load_selected(path)
  → 返回普通 state → FeatureRoute.apply_state(state)

ActionBarRoute「开始」
  → 从 FileListModel / OutputRoute / FeatureRoute 获取普通状态
  → 对应 Service 校验并构建 RunRequest(job_id, ...)
  → BatchOrchestrator.begin(request)
  → BatchEvents(JobEvent with job_id)
  → FileListRoute 更新 ✓✗；LogRoute 追加
```

`AppContext` 只提供这些依赖的只读引用；它本身不充当任意模块均可发布 / 订阅的全局总线。

---

## 4. 分阶段实施计划

### P-1 — 基线、契约与测试护栏

| 项 | 内容 |
|----|------|
| **目标** | 在搬迁代码前固化关键行为，并建立后续阶段共同遵守的数据与线程契约 |
| **范围** | 定义 §2.2 DTO、统一 `FeatureDescriptor`、依赖规则、任务状态机与 Worker 生命周期；建立纯逻辑测试目录；增加 `LegacyFeatureAdapter` 设计 |
| **基线测试** | HTML 引用 / 去重键 / 相对路径、`BatchSession` 状态、续跑序号、输出路径矩阵、预设往返、功能注册完整性 |
| **不做** | 不搬 UI，不改变用户行为，不要求一次消除旧 options dict |
| **风险** | 低；契约设计过度时应坚持最小字段，以现有行为为准 |

**完成定义（DoD）**：

- [ ] DTO 与依赖规则落地，且不 import QWidget 等 UI 类型
- [ ] `FeatureDescriptor` 同时覆盖图片与文件处理扩展路径
- [ ] 关键纯逻辑有可重复执行的基线测试（优先标准库 `unittest`，不擅自新增依赖）
- [ ] 定义 `job_id`、任务状态与关闭窗口策略
- [ ] 通过 §7.1 验收清单

---

### P0 — 导入逻辑与 Qt 适配分离

| 项 | 内容 |
|----|------|
| **目标** | 把导入拆为纯解析 / 抽取、Qt 边缘适配与协调服务；窗口只保留事件入口 |
| **范围** | `services/common/importing/*`：scanner、HTML/DOCX/PDF extractor、dedupe、`ClipboardPayload`；Qt Clipboard/QImage Adapter；临时导入文件存储；常量放 importing 子包，避免继续扩张 `config.py` |
| **迁出内容** | `_collect_import_groups`、`_scan_folder_*`、HTML 解析器、DOCX/PDF 抽图、路径 / 位图 / CF_HTML 规范化、去重键逻辑 |
| **边界** | `QClipboard/QMimeData/QImage` 只存在于 Qt Adapter；纯 extractor 输入输出普通 Python 数据 / bytes / Path |
| **不做** | 不改左栏 UI 结构；不改批处理 |
| **预估体量** | `main_window` 减少约 **800–1100 行** |
| **风险** | 中；除钉钉 HTML、最小图过滤外，还需保持后台抽取 Worker 所有权、取消和临时文件生命周期 |

**完成定义（DoD）**：

- [ ] 上述 service 文件存在且可被 `main_window` 调用
- [ ] `main_window` 中不再包含 HTMLParser 大段实现 / PDF XObject 遍历实现
- [ ] 拖放、添加文件/文件夹、粘贴、容器抽图行为与改前一致
- [ ] 通过 §7.2 验收清单

---

### P1 — 文件列表状态模型 + 独立路由

| 项 | 内容 |
|----|------|
| **目标** | 先建立与 QListWidget 解耦的导入集合，再迁移左栏 Route 与通用 widgets |
| **范围** | `ImportCollection` / `FileListModel`（以 `ImportEntry` 为真相）<br>`FileListRoute`、缩略图加载与预览<br>`GradientBackground` / `DropListWidget` 外置 |
| **状态原则** | 路径、相对路径、选中范围、任务状态不能只存于 `QListWidgetItem.data()`；控件是状态投影 |
| **不做** | 不拆开始处理；日志 / 版本页可继续保留在旧窗口，统一到 P5；不拆功能面板 |
| **风险** | 中；信号连接顺序、异步缩略图迟到、拖放 dual-drop、移除条目后的缓存一致性 |

**完成定义（DoD）**：

- [ ] 文件集合可在无 QWidget 情况下增删、去重、按范围生成输入快照
- [ ] 左栏增删改查、缩略图、预览独立在 `FileListRoute`
- [ ] 异步缩略图结果按 `entry_id` 校验，已删除条目的迟到结果被忽略
- [ ] 通过 §7.3 验收清单

---

### P2 — 功能契约试点 + 图像处理 Tab / 预设

| 项 | 内容 |
|----|------|
| **目标** | 用 Basic 功能验证 Route / Service / Processor 单向边界，再建立功能切换和预设宿主 |
| **范围** | `BasicFeatureRoute`、`BasicService`、纯处理 Basic Processor<br>`ProcessTabRoute`、`PresetService`、统一 `FeatureDescriptor` 注册<br>`LegacyFeatureAdapter` 包装尚未迁移的四个功能 |
| **数据流** | Route `collect_raw_state()` → Service `validate_and_normalize(raw)` → 普通 options；Preset → Service normalize/migrate → Route `apply_state()` |
| **不做** | 不迁其余复杂功能；不实现完整 BatchOrchestrator |
| **风险** | 中；Basic 改为试点时必须保持格式 / 压缩 / DPI / 重命名联动 |

**完成定义（DoD）**：

- [ ] `BasicService` 不 import Route / QWidget，Basic Processor 不创建或读取控件
- [ ] 新旧功能均由统一描述符出现在功能菜单，注册只有一个权威入口
- [ ] 切换功能自动换面板并加载 default 预设
- [ ] 选择 / 保存 / 删除 / 恢复默认 / 定位 / 外部加载预设均可用
- [ ] 通过 §7.4 验收清单

---

### P3 — 通用批处理编排 + 输出策略

| 项 | 内容 |
|----|------|
| **目标** | 开始 / 续跑 / 重试 / 取消 / 进度 / 结算进入 `BatchOrchestrator`；输出与范围进入独立 Route + Service |
| **范围** | `batch_orchestrator.py`、`worker_factory.py`、`output_path_service.py`<br>`OutputSettingsRoute`、`ActionBarRoute`<br>`RunRequest` / `OutputPolicy` / `JobEvent` 与任务状态机 |
| **关键点** | Orchestrator 只接收快照，不读 QWidget；`BatchSession` 演进为 `BatchSnapshot`；`file_index_map`；Worker 完全退出后结算；Legacy Adapter 提供旧功能 options |
| **不做** | 不在编排器写具体功能特判；`keep_matting` 等由 FeatureService 构建运行参数 |
| **风险** | **高**；必须完整回归批处理、AI Worker 清理与续跑 |

**完成定义（DoD）**：

- [ ] `MainWindow` 内不再存在完整的 `_begin_process` 业务体（仅触发 Route / Orchestrator）
- [ ] Orchestrator、Worker 事件均携带 `job_id`，旧任务迟到事件不更新当前 UI
- [ ] 全量 / 仅选中 / 四种输出模式 / 保留结构 / 同名覆盖 / 续跑 / 重试失败行为不变
- [ ] 透明图「保留抠图结果」仍正确产出 `_matted`
- [ ] 通过 §7.5 验收清单

---

### P4 — 功能级路由与 Service

| 项 | 内容 |
|----|------|
| **目标** | 完成其余功能的 FeatureRoute + FeatureService；Processor 专注 `process` / `process_batch` |
| **范围** | metadata / overlay / img2doc / transparent 四个 Route 与 Service；移除这些 Processor 的 `create_panel` / 控件状态依赖；统一 `FeatureDescriptor` |
| **顺序建议** | metadata → overlay → img2doc → transparent（Basic 已在 P2 试点；透明图依赖配置链接与抠图 options，最后迁） |
| **风险** | 中高；元数据回读、排版叠加层、透明图 matting UI 联动 |

**完成定义（DoD）**：

- [ ] 五功能均可从统一注册表创建，无需改壳或 Orchestrator 核心
- [ ] Processor 不创建面板、不读取控件；`BaseProcessor` / `BaseFileProcessor` 的 UI 抽象方法进入废弃清理路径
- [ ] Service 不反向依赖 Route；选中回读通过普通数据返回并由 Route 应用
- [ ] 各功能核心场景通过 §7.6
- [ ] 扩展指南草案已能说明 Descriptor / Route / Service / Processor 四部分

---

### P5 — 应用壳、独立页面与配置中心

| 项 | 内容 |
|----|------|
| **目标** | 在业务边界稳定后完成瘦壳；迁移日志 / 版本页，并将 `settings_panel` 拆为配置壳 + 开发环境 + 抠图模型 Route |
| **范围** | `ui/shell`、只读 `AppContext`、局部 typed event hubs、`LogRoute`、`ChangelogRoute`<br>`SettingsRoute` / `DevEnvRoute` / `MattingModelRoute`<br>`runtime_facade.py`、`matting_config_service.py`（薄封装已有 manager） |
| **保持** | `pixelflow://settings/dev`、`pixelflow://settings/matting` 跳转；门禁；配置长任务日志仍进入后台日志 |
| **风险** | 中高；配置页同时存在 QThread 与 threading.Thread，必须统一任务所有权、页面销毁后的迟到回调和关闭策略 |

**完成定义（DoD）**：

- [ ] `ui/shell/main_window.py` 成为入口窗口，旧 `ui/main_window.py` 仅可保留无副作用 re-export
- [ ] `AppContext` 不保存可变页面 / 任务状态，不通过字符串定位任意 Service
- [ ] 日志、版本日志和配置页均不依赖 MainWindow 大段构建代码
- [ ] 原 `settings_panel.py` 可删或变无副作用 re-export
- [ ] 开发环境检测 / 安装 uv、镜像与代理、模型环境与权重流程可用
- [ ] 通过 §7.7 验收清单

---

### P6 — 收尾与文档同步

| 项 | 内容 |
|----|------|
| **目标** | 删除死代码、Legacy Adapter 与临时 re-export；文档、注册和打包路径对齐；本计划进度全部 ✅ |
| **范围** | 更新 `README.md` 项目结构、`TECHNICAL.md` 架构章<br>检查 `PixelFlow.spec` hiddenimports / 数据包并完整打包<br>仅在有用户可感知变化时更新 `resources/CHANGELOG.md`<br>确认依赖规则与 `MainWindow` 辅助行数指标达标 |
| **风险** | 低 |

**完成定义（DoD）**：

- [ ] 文档结构与真实目录一致
- [ ] 打包配置覆盖 `services/`、`ui/shell`、`ui/routes`
- [ ] §0 总览全部 ✅，§7.8 全局验收通过
- [ ] 本文件状态总览更新为「已完成」

---

## 5. 关键设计约定

### 5.1 BatchOrchestrator 职责边界

**输入**：已由 Route / Service 构建并校验完成的 `RunRequest`，Orchestrator 不主动读取任何页面状态。

**负责**：

- 校验：无并发任务、请求输入非空、任务状态允许启动
- 会话：创建 / 恢复 `BatchSnapshot`，维护 `job_id` 与状态机
- 启动：通过 `WorkerFactory` 选择 `ProcessWorker` 或 `FileProcessWorker`
- 转发：把 Worker progress / item_done / finished / debug 统一包装为带 `job_id` 的 `JobEvent`
- 生命周期：取消请求、Worker 强引用、`QThread.finished` 后结算、窗口关闭协调

**不负责**：

- 不读取或操作 QListWidget / QWidget
- 不收集 FeatureRoute 参数，不实现具体 `process()` 算法
- 不包含 `transparent_image` 等功能 id 特判
- 不自行拼接 `_output_format`、`keep_matting` 等功能参数

### 5.2 FeatureService 最小接口（约定）

```text
descriptor: FeatureDescriptor
default_state() -> dict
normalize_preset(data: dict) -> dict
validate_and_normalize(raw_state: dict) -> ValidationResult[dict]
build_run_options(options: dict, output_policy: OutputPolicy) -> dict
load_selected(path: str) -> dict | None              # 可选，返回普通状态数据
format_start_log(options: dict) -> str | None         # 可选
create_processor() -> BaseProcessor | BaseFileProcessor
```

Route 侧对应接口：

```text
build_widget(parent) -> QWidget
collect_raw_state() -> dict
apply_state(state: dict) -> None
show_validation_errors(errors) -> None
```

**禁止**：Service 接收 Route 实例、直接读取控件或调用 `route.apply_*`。Route 负责 UI 映射，Service 负责普通数据。

### 5.3 FeatureDescriptor 与统一注册

```text
id / name / icon / input_kind
processor_factory / route_factory / service_factory
supports_resume / supports_selected_load / capabilities
preset_schema_version
```

- 壳、处理 Tab、预设和编排均读取同一描述符，不维护多份功能顺序。
- 兼容期由 `LegacyFeatureAdapter` 包装尚未迁移的 Processor；旧模块 re-export 不得再次触发注册。
- PyInstaller 可保留显式 hiddenimports，但应由注册清单统一校验，避免新增功能只在开发环境可见。

### 5.4 Qt 任务生命周期契约

```text
IDLE → STARTING → RUNNING → CANCELLING → FINISHED
                           └──────────→ FAILED
```

1. 每次批处理生成唯一 `job_id`；progress / item_done / log / finished 均携带它。
2. Orchestrator 强持有 Worker，直到 `QThread.finished`；`cancel()` 只表示请求取消，不表示已结束。
3. UI 只接受当前 active `job_id` 的状态事件，迟到事件只允许落日志，不得覆盖当前进度 / 列表状态。
4. 完成结算必须在 Worker `finished` 后执行，确保 AI 子进程已在 Worker `finally` 清理。
5. 窗口关闭时：禁止新任务 → 请求取消 → 等待后台任务有界退出 → 再释放窗口；禁止直接 terminate 正在清理的 Worker。
6. 导入抽取、缩略图、配置扫描、环境创建和模型下载也必须指定 owner；owner 销毁后回调需通过 token / weak guard 丢弃。

### 5.5 兼容期策略

| 阶段 | 策略 |
|------|------|
| P-1–P1 | `main_window` 可保留，内部逐步委托给 service / model |
| P2 | Basic 走新 Route/Service；其余功能由 `LegacyFeatureAdapter` 包装旧 `create_panel` |
| P3 | Orchestrator 同时支持新功能契约与 Legacy Adapter，不反向读取旧面板 |
| P4 | 五功能全部走新契约；旧 Processor UI API 标记 deprecated |
| P5 | 新壳成为组合根，旧窗口只保留无副作用 re-export |
| P6 | 删除 Legacy Adapter、deprecated UI API 与无用 re-export |

### 5.6 打包注意

- 新增包：`services`、`ui.shell`、`ui.routes`、`ui.widgets`
- 每阶段做 import 与 PyInstaller 模块收集冒烟；P6 才执行完整 onedir / 安装包验收
- 动态 / 工厂注册功能需校验 hiddenimports，避免开发环境存在、打包后缺失
- `core/matting/workers` 仍须随包（既有规则不变）

---

## 6. 计划进度维护说明

### 6.1 谁更新

实施重构的开发者（或 AI 辅助提交时）在 **每个阶段开始 / 结束** 更新本文档。

### 6.2 更新哪些位置

1. **§0 进度总览表**：状态、完成日期、整体完成度  
2. **对应阶段 DoD 复选框**：`[ ]` → `[x]`  
3. **§8 进度日志**：追加一条（日期、阶段、做了什么、遗留问题）  
4. 若调整范围：改 §4 该阶段「范围/不做」，并在 §8 说明  

### 6.3 状态流转

```text
⬜ 未开始 → 🔵 进行中 → ✅ 已完成
                ↘ ⏸ 暂停（写明原因）
                ↘ ❌ 取消（写明原因）
```

### 6.4 与 Git / CHANGELOG

- 建议每完成一个阶段至少一个可运行提交（或 PR）
- 纯内部架构搬迁记录在本文 §8；仅当用户可感知的稳定性、性能或扩展机制发生变化时，按项目规则更新 `resources/CHANGELOG.md`
- **不要**在未验收时把 §0 标为 ✅

---

## 7. 计划完成校验说明

> **用法**：每阶段合并前执行“自动化 / 契约检查 + 手工 GUI 回归 + import / 打包收集冒烟”。  
> 全部勾选后方可将该阶段标为 ✅；项目启动、完整打包仍由开发者按项目规则执行。

### 7.0 通用校验门槛（每一阶段都要做）

| # | 校验项 | 方法 | 通过标准 |
|---|--------|------|----------|
| G1 | 语法 / 导入 | 在项目 venv 中导入变更模块与入口 | 无 ImportError / SyntaxError / 重复注册 |
| G2 | 自动化基线 | 执行本阶段相关纯逻辑测试 | 全部通过；未授权时不新增测试依赖 |
| G3 | 启动 | 开发者本地 `python app.py` | 主窗口正常显示，无启动 traceback |
| G4 | 样式回归 | 目视主界面 | 毛玻璃主题、下拉框不透明背景、主按钮渐变仍在 |
| G5 | 依赖方向 | 搜索 / 审查 imports | Service 不依赖具体 Route/QWidget；Orchestrator 不依赖 UI；无新增循环依赖 |
| G6 | 线程生命周期 | 审查并手工验证本阶段后台任务 | owner、取消、finished 后结算、迟到事件策略符合 §5.4 |
| G7 | 打包收集冒烟 | 检查 spec / Analysis / hiddenimports（完整打包可到 P6） | 新包与动态注册模块可被收集，AI workers 路径不变 |
| G8 | 无擅自依赖 | 查 diff | 未新增未授权第三方依赖；未误改 `excludes` 导致缺模块 |
| G9 | 文档进度 | 打开本文件 | §0 / DoD / §8 已按 §6 更新 |

---

### 7.1 P-1 验收清单 — 基线与契约

| # | 场景 | 通过标准 |
|---|------|----------|
| P-1-1 | DTO 纯度 | contracts 可在无 QApplication 情况下导入，不携带 Qt UI 对象 |
| P-1-2 | FeatureDescriptor | 同时描述 image / file / batch-document 能力，功能 id 唯一 |
| P-1-3 | 状态基线 | `BatchSession` 状态、取消、续跑、失败重试和原序号测试通过 |
| P-1-4 | 导入基线 | HTML / 路径 / 去重 / 相对路径样例测试覆盖当前边界 |
| P-1-5 | 输出基线 | 四种输出模式、保留结构和双重覆盖语义的矩阵测试通过 |
| P-1-6 | 生命周期契约 | `job_id`、状态机、关闭窗口和迟到事件规则已写入代码接口或架构文档 |

**P-1 阶段通过条件**：G1–G9 + 上表全部通过。

---

### 7.2 P0 验收清单 — 导入 / 抽图 / 剪贴板

| # | 场景 | 操作步骤 | 预期结果 |
|---|------|----------|----------|
| P0-1 | 添加文件 / 文件夹 | 添加多张图片及多层目录 | 全部递归进入列表；相对路径保持一致 |
| P0-2 | 拖放 | 拖入图片 / 文件夹到列表或窗口 | 仅 Drop 接受，导入成功 |
| P0-3 | DOCX / PDF 抽图 | 导入含图文档 | 只加入抽出图片；PDF 非整页渲染；小装饰图过滤一致 |
| P0-4 | HTML 抽图 | 本地 / 剪贴板 HTML | http(s)、相对路径、base64、srcset 等行为一致 |
| P0-5 | 粘贴 | 路径、截图、CF_HTML | 焦点规则正确；生成图片并进入列表 |
| P0-6 | 去重 | 重复文件 / URL / 钉钉 HTML | URL path 与内容 hash 规则保持一致，无不合理重复 |
| P0-7 | Qt 边界 | 审查 imports | QClipboard / QMimeData / QImage 只在 Qt Adapter，不进入纯 extractor |
| P0-8 | 临时文件 | 粘贴截图 / 远程图后清空或关闭 | 当前会话需要时文件仍可用，释放时机明确且无残留泄漏 |
| P0-9 | 代码位置 | 搜索 HTMLParser / PDF XObject | 实现主体在 `services/common/importing`，不在窗口函数内 |

**P0 阶段通过条件**：G1–G9 + 上表全部通过。

---

### 7.3 P1 验收清单 — 文件列表状态模型

| # | 场景 | 操作步骤 | 预期结果 |
|---|------|----------|----------|
| P1-1 | 增删 / 去重 | 添加、移除选中、清空、重复导入 | `ImportCollection` 与可见计数一致 |
| P1-2 | 选中预览 | 点击及多选列表项 | 预览与当前条目一致，处理范围快照正确 |
| P1-3 | 相对路径 | 导入多层文件夹后查看模型 | `ImportEntry.relative_path` 保持，不依赖 item role 才能恢复 |
| P1-4 | 缩略图迟到 | 加载中移除 / 清空条目 | 迟到结果被 entry_id guard 忽略，不更新错误条目 |
| P1-5 | 拖放入口 | 拖到窗口与列表不同区域 | 统一进入导入协调服务，不发生双重导入 |
| P1-6 | 无 UI 模型测试 | 不创建 QWidget 操作集合 | 可增删、选取范围、生成输入快照 |

**P1 阶段通过条件**：G1–G9 + 上表全部通过。

---

### 7.4 P2 验收清单 — 功能契约 / 处理 Tab / 预设

| # | 场景 | 操作步骤 | 预期结果 |
|---|------|----------|----------|
| P2-1 | Basic 新契约 | 改格式 / 压缩 / DPI / 重命名参数 | Route 只收集状态，Service 规范化，Processor 不读控件 |
| P2-2 | 切换功能 | 下拉切换五功能 | 新 Basic 与 Legacy 功能面板均正常，说明文案更新 |
| P2-3 | 默认 / 选择预设 | 重启、切换、选择预设 | 自动 normalize 并应用；旧预设兼容 |
| P2-4 | 保存 / 删除 / 恢复 | 完成预设 CRUD | default 不可删，目录和列表正确 |
| P2-5 | 外部加载 | 加载跨功能预设 | 重名可重命名 / 覆盖，schema / feature id 校验明确 |
| P2-6 | 定位预设 | 打开目录 | 打开当前功能预设目录 |
| P2-7 | 注册完整性 | 检查 FeatureRegistry | id 唯一；图片 / 文件功能顺序稳定；只注册一次 |
| P2-8 | Basic 冒烟 | 跑一批基础处理 | 行为与迁移前一致，仍可由旧批处理入口执行 |

**P2 阶段通过条件**：G1–G9 + 上表全部通过。

---

### 7.5 P3 验收清单 — 批处理编排 / 输出

| # | 场景 | 操作步骤 | 预期结果 |
|---|------|----------|----------|
| P3-1 | 全部 / 仅选中 | 分别开始处理 | 输入来自 RunRequest 快照；未选中时拒绝开始 |
| P3-2 | 四种输出模式 | 桌面、自定义、原图覆盖 / 副本 | 输出位置、UI 显隐与覆盖语义正确 |
| P3-3 | 保留结构 / 同名覆盖 | 文件夹导入后重复处理 | 子目录、覆盖或 `_1/_2` 行为正确 |
| P3-4 | 取消 + 继续 | 批处理中取消 | 进入 CANCELLING；Worker finished 后可继续，原序号保持 |
| P3-5 | 重试失败 / 选中失败 | 制造失败后操作 | 只重跑 / 选中失败项，状态和按钮数量正确 |
| P3-6 | 批量合并功能 | 图片排版导出 | 不支持续跑提示；一次导出成功 |
| P3-7 | 保留抠图 | 透明图开启 AI + 保留 | `*_matted.png` 正确，Orchestrator 无透明图 id 特判 |
| P3-8 | job_id 隔离 | 任务完成后开启新任务并模拟迟到事件 | 旧事件不覆盖新任务进度 / 列表状态 |
| P3-9 | AI 清理 | 完成 / 取消 AI 任务 | 仅在 QThread.finished 后结算，常驻子进程已释放 |
| P3-10 | 关闭窗口 | 运行中请求关闭 | 禁止新任务、请求取消、有界等待，不销毁运行中 QThread |
| P3-11 | 代码位置 | 查 `_begin_process` / QWidget imports | 编排在 Orchestrator；Orchestrator 不读取 QWidget |

**P3 阶段通过条件**：G1–G9 + 上表全部通过（AI 场景无环境时可记录阻塞，但代码审查必须完成）。

---

### 7.6 P4 验收清单 — 五功能路由

对每个功能至少完成「改参 → 预设往返 → 跑通一批」：

| # | 功能 | 最小场景 | 预期 |
|---|------|----------|------|
| P4-1 | 基础处理 | 转 JPG + 质量压缩 + 可选 DPI | 输出格式与体积/ DPI 符合预期 |
| P4-2 | 元数据编辑 | 写标题/标记；选中回读 | Windows 属性可见或与改前一致；回读不丢字段 |
| P4-3 | 图片叠加 | 固定文字 + 可选 Logo | 位置/颜色正确 |
| P4-4 | 排版导出 | 少量图导出 PDF 或 PPTX | 文件可打开，布局合理 |
| P4-5 | 透明图 | 裁剪 + 画布布局（AI 可选） | 画布尺寸与主体占比正确 |
| P4-6 | 扩展性 | 阅读注册表 | 新增功能步骤不需改 `BatchOrchestrator` 核心 |
| P4-7 | 依赖方向 | 抽查 import | Route → Service → core；Service / Processor 不 import 具体 Route |
| P4-8 | Processor 纯化 | 搜索 `create_panel` / QWidget | 功能 Processor 不创建面板、不读取控件 |

**P4 阶段通过条件**：G1–G9 + 上表全部通过。

---

### 7.7 P5 验收清单 — 壳 / 独立页面 / 配置中心

| # | 场景 | 操作步骤 | 预期结果 |
|---|------|----------|----------|
| P5-1 | 开发环境页 | 检测 Python / uv / Git | 状态展示正确 |
| P5-2 | 镜像与代理 | 修改并保存 | `runtime_settings.json` 更新；不改全局 pip/gitconfig |
| P5-3 | 抠图模型页 | 门禁 | 开发环境未就绪时引导跳转 |
| P5-4 | 创建环境/下载 | 按现有流程（耗时可只测入口与日志） | 日志进入后台日志；成功/失败提示合理 |
| P5-5 | 深链 | 透明图提示打开配置 | `open_settings(0|1)` 或协议仍有效 |
| P5-6 | 壳与页面 | Tab 切换、日志清空、版本日志 | 页面正常；MainWindow 只做装配与导航 |
| P5-7 | 代码结构 | 查看 shell / settings 路由 | 开发 / 模型页分离，旧巨石已拆，AppContext 无业务状态 |
| P5-8 | 配置任务迟到 | 页面切换 / 关闭时完成扫描或下载回调 | 已销毁 owner 不被访问，日志与状态不串任务 |

**P5 阶段通过条件**：G1–G9 + 上表全部通过。

---

### 7.8 P6 / 全局最终验收

| # | 校验项 | 通过标准 |
|---|--------|----------|
| F1 | 依赖方向 | Service 不依赖 Route/QWidget；Processor 不读控件；Orchestrator 不操作 UI；无循环依赖 |
| F2 | 壳行数（辅助） | `ui/shell/main_window.py` 目标 **≤ 300 行**；超标不直接判失败，但须说明不可再拆的装配职责 |
| F3 | 无上帝类回潮 | 单一 UI 文件原则上 **< 1500 行**；AppContext / Orchestrator 不聚合无关职责 |
| F4 | 契约与测试 | DTO、注册、导入、输出、续跑与任务状态自动化基线全部通过 |
| F5 | 目录与文档 | README / TECHNICAL 结构图、扩展步骤与仓库一致 |
| F6 | 打包 | 开发者执行完整打包，产物可启动；动态功能注册完整；AI workers 仍在 |
| F7 | 全功能冒烟 | §7.1–7.7 核心项在最终分支上再跑一轮代表场景 |
| F8 | 兼容清理 | Legacy Adapter、deprecated Processor UI API、重复注册和无用 re-export 已删除 |
| F9 | 本计划 / CHANGELOG | §0 全部 ✅、§8 有完成记录；仅在符合项目规则时有 CHANGELOG 条目 |

**整计划关闭条件**：F1–F9 全部通过，将文首「状态总览」改为 **已完成**，并填写完成日期。

---

### 7.9 验收记录模板（可复制到 §8）

```markdown
### YYYY-MM-DD — Px 验收

- 执行人：
- 环境：Windows / macOS，Python x.x，是否有抠图环境：
- 通用门槛 G1–G9：通过 / 失败（说明）
- 阶段清单：通过 n/m；失败项：
- 遗留问题：
- 结论：✅ 阶段完成 / ❌ 未通过
```

---

## 8. 进度日志

> 按时间倒序或正序追加均可，建议 **正序**。每条尽量短。

### 2026-08-06 — 计划建立

- 状态：文档创建，全部阶段 ⬜ 未开始
- 内容：确认以 Route + 通用/功能 Service + 瘦壳 MainWindow 为方向；制定初版 P0–P6 与验收清单

### 2026-08-06 — 架构评审后修订

- 状态：实施尚未开始；计划扩展为 P-1–P6 共 8 阶段
- 内容：补充 DTO / FeatureDescriptor / 依赖规则 / Qt 生命周期；Service 禁止依赖 Route；Basic 先试点，BatchOrchestrator 后置；测试与打包收集检查前置
- 下一步：从 **P-1** 开始，先固化基线与契约，不直接搬迁 UI

---

## 9. 风险与回滚

| 风险 | 缓解 | 回滚 |
|------|------|------|
| 续跑/序号错乱 | P-1 固化基线，P3 重点测原序号与快照 | 回滚 P3，保留已稳定契约 / 导入 / 列表 |
| 循环导入 | 强制依赖规则；Service 不 import Route；AppContext 只读注入 | 调整依赖方向，不用延迟全局查找掩盖循环 |
| AppContext / Orchestrator 上帝化 | 限定接口与状态所有权；按 typed event 拆分 | 将无关职责退回 Feature / common service |
| 线程迟到 / 关闭崩溃 | `job_id`、owner guard、QThread.finished 后结算、有界关闭 | 回滚对应 Route / Worker 接入 |
| 打包漏模块 | 每阶段做模块收集冒烟，P6 完整打包 | 补 hiddenimports / 注册清单并增加检查 |
| 范围膨胀顺手改业务 | DoD 强调行为等价 | 评审拒绝无关 diff |
| 双轨期 API 混乱 / 重复注册 | Legacy Adapter 为唯一兼容入口；re-export 无副作用；P6 删除 | 恢复单一权威注册入口 |

**回滚原则**：以阶段为单位回滚；不要在 P4 出问题后无条件回滚已稳定的 P0 导入逻辑。

---

## 10. 参考：原 main_window 职责地图（迁移索引）

| 原区域（约） | 目标位置 | 阶段 |
|--------------|----------|------|
| L1–1150 抽图/HTML/PDF | `services/common/importing/*` | P0 |
| ThumbnailLoader | `ui/routes/file_list/` + entry_id guard | P1 |
| GradientBackground / DropListWidget | `ui/widgets/` | P1 |
| 左栏 UI + 导入入口 | `ImportCollection` + FileListRoute + import services | P0–P1 |
| 基础处理面板 / Processor UI | Basic Route + Service + pure Processor | P2 |
| 预设栏 / 功能切换 | PresetService + ProcessTab + FeatureRegistry | P2 |
| 输出路径 / 范围 | Output Route + OutputPolicy Service | P3 |
| `_begin_process` 及回调 | BatchOrchestrator + ActionBar + WorkerFactory | P3 |
| 其余功能面板 `create_panel` | `routes/process/features/*` + FeatureService | P4 |
| `_apply_style` | `ui/shell/styles.py` | P5 |
| 日志 / changelog | Log / Changelog Route | P5 |
| settings_panel | settings 子 Route | P5 |

---

## 11. 修订记录

| 日期 | 修订说明 |
|------|----------|
| 2026-08-06 | 初版：目标架构、P0–P6、进度维护、完成校验清单 |
| 2026-08-06 | 架构评审修订：增加 P-1；明确 DTO、单向依赖、统一注册、Qt 生命周期；调整为 Basic 契约试点先于 BatchOrchestrator；前置测试与打包收集门槛 |
