# 更新日志

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.0.1] - 2026-07-07

### 首个正式发布版本

#### 主动学习

- **后置学习分析**：LLM 回复完成后，插件自动分析对话内容，判断是否包含值得记忆的知识点。不再依赖 LLM 主动调用工具
- **全员学习触发**：所有用户按 `learn_weight` 概率触发学习提示注入；管理员明确要求学习时才学习
- **节流控制**：每 scope 30 秒最多分析一次，避免高频 LLM 调用

#### 验证系统

- **LLM 关键词提取**：验证前先让 LLM 从记忆内容中提取 3-5 个搜索关键词，用关键词组合构建搜索 query
- **多搜索源支持**：Tavily / BoCha / Brave 网页搜索 + B 站搜索，从 AstrBot 配置读取 API key
- **验证搜索源配置**：新增 `verifier_search_source` 配置项（auto / web / bilibili / web+bilibili / llm）
- **LLM-only 降级模式**：无外部搜索源时自动降级为纯 LLM 3 轮自辩论
- **置信度修复**：`partial` 不再降低置信度（改为轻微提升），`inconclusive` 保持不变，避免反复验证导致死亡螺旋
- **验证标准放宽**：`correct` 或 `partial` + 置信度 ≥ 0.5 即标记为已验证

#### 群黑话捕获

- **无钩子依赖**：通过 `on_llm_request` 实现群黑话捕获，不依赖 `on_message` 钩子，兼容所有 AstrBot 版本

#### Dashboard

- **批量验证**：选择多条记忆后一键批量验证，带进度显示
- **插件日志面板**：展示本插件最近 200 条日志，支持手动刷新和展开自动加载
- **验证详情面板**：验证后展示使用模型、关键词、搜索来源、所有 LLM 提示词和回复全文
- **设置页增强**：新增主动学习、文档分块、验证搜索源等配置分组

#### 架构

- **LLMService**：统一 LLM 调用抽象，封装 provider 解析、超时、异常降级
- **ConfigManager**：三层配置管理（AstrBot config → Dashboard → 默认值），原子写入
- **Importer**：导入逻辑分离（~650 行从 main.py 剥离）
- **移除 DuckDuckGo**：不再内置搜索引擎，网页搜索依赖 AstrBot 配置的 Tavily/BoCha/Brave

#### Provider 解析

- **多级 fallback**：Dashboard 设置 → AstrBot 配置 → provider_manager → cmd_config.json → cfg 全局配置
- **兼容 AstrBot v4.26.4**：`provider_manager.providers` 为空时从 `data/cmd_config.json` 兜底读取



## [2.6.6.0] - 2026-07-06

### 架构重构

- **提取 `llm_service.py`**：统一 LLM 调用抽象，封装 `generate()` 和 `resolve_provider_id()`，自动处理超时/异常降级。消除 `refiner.py` / `verifier.py` / `tools.py` / `main.py` 中分散的 `context.llm_generate` 直接调用
- **提取 `config_manager.py`**：统一配置管理，封装三层配置源（AstrBot config → Dashboard 设置 → 代码默认值），提供 `get()` / `update()` / `all()` 接口。消除配置读取逻辑分散在 `__init__`、`_apply_config_to_runtime`、`_web_save_settings` 的现状
- **提取 `importer.py`（~650 行）**：所有导入逻辑（纯文本 / MD / PDF / DOCX / TXT / ZIP / 内置 KB）从 `main.py` 分离到独立模块。`main.py` 的导入 API 层仅保留 ~60 行薄包装

### 优化

- **`_web_get_settings`** 新增返回 7 个字段：`enable_active_learn_hint`、`learn_weight`、`admin_ids`、`search_top_k`、`default_confidence`、`chunk_size`、`chunk_overlap`
- **Dashboard ⚙ 设置页面** 新增主动学习（开关/权重/搜索条数/置信度/管理员）、文档分块配置分组，支持滑块实时数值显示
- **`SettingsStore.update()` 与 `save()` 去重**：`ConfigManager.update()` 内联原子写入逻辑，消除重复代码
- **移除未使用的导入**：`io`、`uuid`、`zipfile`、`chunker` 模块级函数移至 `importer.py`
- **`_parse_md` 模块级函数移至 `importer.py`**，`main.py` 不再直接依赖

### 版本

主版本 +1（2.6.5.x → 2.6.6.x），表示架构级重构，无破坏性行为变更。

## [2.6.5.5] - 2026-07-06

### 新增

- **后置异步学习分析**：不再依赖 LLM 主动调用 `search_and_learn` 工具（LLM 始终不调），改为**回复完成后**由插件自动分析用户消息+LLM 回复，判断是否包含值得记忆的新知识点。如检测到新知识，自动精炼后存入记忆库。
  - 调用链路：`用户发消息 → LLM 回复 → on_llm_response → _post_learn_analysis → LLM 分析对话 → 存入记忆库`
  - 节流：每 scope 30 秒最多分析一次，避免高频 LLM 调用
  - 解析：LLM 输出 `TYPE: learn/skip + TOPIC/CONTENT/KEYWORDS` 结构化格式
  - 仅在管理员且 `learn_weight > 0` 时生效

## [2.6.5.4] - 2026-07-06

### 新增

- **配置双向同步**：`__init__` 现在合并 `_settings`（Dashboard 存储）到 `cfg`，覆盖 AstrBot 插件配置页的值。无论从哪边修改，运行时都使用最新值
- **`_apply_config_to_runtime` 补全**：新增 `learn_weight`、`search_top_k`、`default_confidence`、`chunk_size`、`chunk_overlap` 的运行时即时生效
- **工具提醒始终注入**：管理员对话中 `learn_weight >= 0.5` 时，即使记忆命中也会注入简短工具提醒 `（如果用户提供了你原本不掌握的新知识点，可调用 search_and_learn 工具学习）`
- **`learn_weight=1.0` 激进模式**：提示词包含结构化判断标准（不熟悉术语/纠正表述/主动科普 → 立即调用），force LLM 更积极调用工具

### 优化

- **`search_and_learn` 工具描述**：改为结构化列表（4 种必须调用的情况），标题标注「必用工具」，提高 LLM 调用意愿

## [2.6.5.3] - 2026-07-06

### 新增

- **配置统一管理**：新增 `learn_weight`（学习强度 0~1）、`search_top_k`（搜索返回条数）、`default_confidence`（默认置信度）、`chunk_size`/`chunk_overlap`（文档分块参数）配置项，全部可在 Dashboard「⚙ 设置」页面修改
- **主动学习权限管理**：`_is_admin_user()` 从 AstrBot 全局配置 `wl_admin` 和插件配置 `admin_ids` 读取管理员名单，仅管理员可触发 `search_and_learn`
- **管理员配置入口**：`_conf_schema.json` 新增 `admin_ids`（逗号分隔 QQ 号），可在 Dashboard 设置页直接编辑，无需手动改 `config.yml`
- **确认弹窗**：删除记忆时使用自定义模态框替代浏览器原生 `confirm()`，避免 Docker CSP 拦截

### 优化

- **主动学习提示强度**：`learn_weight` 控制提示语力度（0=关闭 / 0.1~0.4 温和 / 0.5~0.7 建议 / 0.8~1.0 强提示），`on_llm_request` 内根据权重选择提示模板
- **硬编码参数可配置**：`search_top_k` 替代 `memory_search` 中的 `top_k=5`；`default_confidence` 替代所有导入方法的 `final_confidence=0.6`；`chunk_size`/`chunk_overlap` 替代文档分块的 `500`/`50`
- **LLM 不调用工具时记录**：`on_llm_response` 输出 `ℹ️ 主动学习提示已注入，LLM 未调用 search_and_learn（无需学习）`

### 修复

- **知识库 500 错误**：`float(d.created_at)` 改为 `float(d.created_at.timestamp())`，修复 datetime 类型无法 `float()` 转换的问题
- **LLM 回复中泄露参考资料**：`on_llm_response` 中 `content_part` 的 References 标签被删除，改用 `extra_assistant_content_parts` 注入
- **主动学习不存储**：`SearchAndLearnTool.call()` 中的 `store.add_or_update()` 改为 `await asyncio.to_thread()`，避免线程池死锁
- **Docker 中 LLM Provider 获取失败**：`_resolve_default_provider_id()` 增加兜底读取插件配置 `llm_provider_id`

## [2.6.5.2] - 2026-07-06

### 新增

- **记忆批量操作**：记忆表格新增多选框、全选/反选/取消选择、选中后批量删除。表格第一列为 checkbox，选中行高亮；顶部出现操作工具栏（显示已选条数 + 批量删除按钮）；分页切换后自动清空选择

## [2.6.5] - 2026-07-06

### 修复

- **LLM 将记忆参考输出到回复中**：改用 `【内部知识 #{id}】{topic} | {置信度}` 格式标注注入记忆，明确告诉 LLM 这是内部参考不要输出。末尾加指令「不要在回复中输出【内部知识】标记」。`on_llm_response` 清理逻辑不再需要，简化为 no-op

## [2.6.4] - 2026-07-06

### 修复

- **Dashboard 验证 400 错误（续）**：改用 `_resolve_plugin_provider_id()`（4 层 fallback 链路）解析 provider，替代原有的简化 fallback。包含：Dashboard 设置 → `_conf_schema.json` → 事件默认 → provider_manager 首条 → 配置字段

## [2.6.3] - 2026-07-06

### 修复

- **Dashboard 验证 400 错误**：Docker 部署下 `provider_manager.providers` 为空，`_resolve_default_provider_id()` 返回空串导致 400。增加最终兜底：直接取 `_conf_schema.json` 中的 `llm_provider_id` 配置
- 前端验证时把 Provider 下拉框的选值传给后端

## [2.6.2] - 2026-07-06

### 修复

- **内置知识库文档列表 500 错误**：`d.created_at` 是 `datetime.datetime` 对象，直接 `float()` 抛 `TypeError`。改为 `float(d.created_at.timestamp())`

## [2.6.1] - 2026-07-06

### 修复

- **内置知识库 500 错误诊断增强**：用户报告"点开内置知识库时读取文档列表失败：Request failed with status code 500"
  - `_web_builtin_kb_documents`：把 `km.get_kb(kb_id)` 移入 try/except，整个 body 包入异常捕获并 `logger.error(exc_info=True)`，让 AstrBot 日志能看到真实异常堆栈
  - `_web_builtin_kb_list`：`list_kbs` 异常增加 `logger.error(exc_info=True)`；`list_documents_by_kb` 从静默 `except: pass` 改为 `logger.debug(exc_info=True)`，避免吞掉真实错误
  - `_web_builtin_kb_import`：`km.get_kb(kb_id)` 移入独立 try/except + `logger.error(exc_info=True)`
- **前端错误提示增强**：list / documents / import 三个端点检测到 5xx 错误时，提示用户"详细错误已记录到 AstrBot 日志，可在 data/logs/ 查看"

## [2.6.0] - 2026-07-06

### 新增

- **群黑话被动捕获 + 定时批量学习**：极低 token 成本自动获取群聊黑话/术语
  - 新增 `@filter.on_message()` 钩子，纯字符串扫描群消息（**不调 LLM**），用正则提取候选黑话词
  - 候选词存入新表 `slang_candidates`，含出现次数、首次/最后出现时间、上下文片段
  - 每个 scope 距上次批量学习 ≥ `slang_capture_interval_hours`（默认 24h）且 pending 候选 ≥ `slang_capture_batch_size`（默认 5）时触发
  - **1 次 LLM 调用**批量处理 K 个候选词（不是 N 次），分别精炼后存入 `memories` 表
  - 候选词标记 `learned=1` 避免重复处理；解析失败的也标记避免无限重试
  - 进程内节流：每 scope 5 分钟最多查一次 DB 看是否该触发批量
- **5 个新配置项**：`enable_slang_capture`（默认关）/ `slang_capture_interval_hours` / `slang_capture_batch_size` / `slang_capture_min_occurrences` / `slang_capture_scope_only_group`
- **新模块 `slang_capture.py`**：纯函数实现候选词提取（10 个正则模式）+ 批量 prompt 构建 + 响应解析（`=== <phrase> ===` section 格式）

### 降级策略

- AstrBot 不支持 `@filter.on_message()` → 特性自动禁用，启动日志输出警告
- LLM 无响应 → 候选词保留 `learned=0`，下次批量重试
- 未配置 LLM Provider → 复用 `_resolve_plugin_provider_id` 4 层 fallback 链路

## [2.5.0] - 2026-07-06

### 新增

- **从 AstrBot 内置知识库批量导入**：Dashboard 顶部新增「📚 内置知识库」按钮，打开模态框：
  - 左侧显示所有知识库（KB）列表，含名称、描述、文档数
  - 选中后右侧显示该 KB 的文档列表（可滚动），每项含文件类型图标、chunk 数量、文件大小、创建时间
  - 多选复选框 + 全选/清空按钮 + 已选数量计数
  - 选择 Scope + 分块大小 + 重叠 + 是否精炼后批量导入
  - 每个文档的所有 chunks 合并为一段文本 → 重新按用户配置分块 → 复用 `_import_chunks_batch_data` 走「精炼 + 嵌入 + 入库」流程
  - 失败的文档单独列出，不阻塞其他文档导入
- **3 个新 Web API**：
  - `GET /builtin_kb/list` — 列出所有内置 KB
  - `GET /builtin_kb/<kb_id>/documents` — 列出某 KB 内的文档
  - `POST /builtin_kb/import` — 批量导入选中文档

### 重构

- 拆分 `_import_chunks_batch` 为 `_import_chunks_batch_data`（返回 dict）+ 包装层（返回 json_response），让内置 KB 导入可直接复用核心入库逻辑而不需要解析 JSON 响应

### 降级策略

- `kb_manager` 不可用（旧版 AstrBot）→ 返回 501 + 友好错误提示
- `vec_db.document_storage` 不可用 → 自动降级直接读 SQLite `<kb_id>/doc.db`

## [2.4.12] - 2026-07-06

### 修复

- **修复 `is_learn_trigger` 未定义导致插件加载/调用崩溃**：v2.4.6 去掉主动学习正则门槛时漏清理 tags 汇总逻辑，第 313 行仍引用已被删除的 `is_learn_trigger` 变量，触发 `NameError`。改为与第 290 行注入条件一致的 `self._enable_active_learn_hint and not hits` 判断

## [2.4.11] - 2026-07-06

### 新增

- **LLM Provider 配置界面下拉选择**：`_conf_schema.json` 中 `llm_provider_id` 字段添加 `"_special": "select_provider"` 标记，AstrBot 配置界面会自动渲染为下拉框，列出所有已注册的 LLM 模型
  - 用户无需手动填入 provider id，直接在下拉框中选择即可
  - 留空（默认）则回退到当前对话模型，行为与之前一致
  - 参考 `menglimi/astrbot_plugin_private_companion` 的实现约定

## [2.4.10] - 2026-07-06

### 新增

- **可视化配置编辑入口**：Dashboard 页面顶部新增「📋 配置」按钮，直接读取 `_conf_schema.json` 动态渲染所有 16 个字段的可视化表单
  - 后端新增 `/config_schema` GET API 返回 schema 与当前合并值
  - 前端按字段类型动态渲染：`bool` → 复选框，`int`/`float` → 数字输入框，`string` → 文本输入框
  - 每个字段卡片显示描述、技术名、hint、默认值
  - 保存即时生效（无需重启 AstrBot）：`_apply_config_to_runtime()` 把合并后的配置应用到所有运行时变量（max_entries、min_confidence、priority_topics、context_inject_count、embedding_enabled、hybrid_search_weight、decay_half_life_days、priority_boost_* 等）
  - 「↺ 恢复默认」一键填入所有 schema 默认值（仅填入表单，需点击「保存」才生效）
  - 原「⚙ 设置」按钮保留作为 Provider + 精炼开关的快速切换入口

## [2.4.9] - 2026-07-06

### 重构

- **拟人化统一记忆池**：scope 从硬过滤改为软权重，所有知识存于统一池中
  - FTS5 检索不再按 scope 过滤——所有记忆都可被搜到
  - 向量检索加载全部记忆的向量，不再按 scope 分片缓存
  - scope penalty 软权重：当前 scope ×1.0，global ×0.9，其他 scope ×0.8
  - 移除 `enable_scope_fallback` 硬过滤开关——不再需要回退，所有结果一律保留
- **设计理念**：知识本就不需要隐私隔离（个人信息归 livingmemory 管）。A 学的"量子纠缠"，B 问时也能检索到，只是权重稍低。更像人类记忆——不会"换了房间就忘记"

## [2.4.8] - 2026-07-06

### 重构

- **`save_memory` 改为两步异步流程**：LLM 只需标记知识点 + 传入对话片段，插件异步调用 LLM 精炼后存入记忆库。LLM 不再需要自己组织内容，降低工具调用门槛
- 新增 `KnowledgeRefiner.refine_snippet()`：从对话片段中蒸馏出结构化知识卡（摘要 + 关键词 + 置信度）
- 工具参数从 `content`（需要 LLM 自己组织）改为 `snippet`（传对话原文即可）
- 异步存储全流程日志可追踪：`save_memory 开始精炼` → `✅ save_memory 已存储`

## [2.4.7] - 2026-07-06

### 新增

- **`save_memory` 工具**：LLM 可在对话中直接存储知识性内容到记忆库，无需搜索网络。当 LLM 通过推理、综合信息产生值得记录的知识时调用。明确标注"仅存通用知识（概念、原理、事实），不存个人信息/偏好/日程"——避免与 `astrbot_plugin_livingmemory` 的生活记忆功能冲突

## [2.4.6] - 2026-07-06

### 变更

- **主动学习不再依赖关键词匹配**：去掉 `ACTIVE_LEARN_PATTERNS` 正则门槛，当记忆库无结果时一律注入 `[学习提示]`，让 LLM 自主判断是否调用 `search_and_learn`。覆盖自然对话中用户提及不熟悉话题的场景（如"昨天看了量子纠缠的论文"），不再要求显式问"什么是X"
- `search_and_learn` 工具描述改为"当用户提及你不熟悉的话题、或你不确定如何回答时直接调用"，鼓励 LLM 主动搜索

## [2.4.5] - 2026-07-06

### 新增

- **诊断面板**：web 页面新增可折叠的「🔧 诊断信息」面板，显示数据库路径、schema 版本、总记忆数、embedder 状态、已注册工具、scope 列表
- **`/debug` web API**：返回数据库和插件运行时诊断信息
- **启动日志增强**：启动时打印记忆总数和 schema 版本（`记忆=N条 | schema=v1`）
- **检索日志增强**：`on_llm_request` 每次检索后打印 `记忆检索: N hits`，检索异常从 debug 提升到 warning

## [2.4.4] - 2026-07-06

### 新增

- **关心领域动态衰减**：priority boost 现在按检索次数衰减。命中关心领域时重置到 `priority_boost_max`，未命中时每次乘以 `priority_boost_decay` 衰减到 `priority_boost_min`。连续问非关心领域时逐步淡化优先，回到关心领域时立即恢复
- 3 个新配置项：`priority_boost_max`（初始/重置，默认 1.3）、`priority_boost_min`（下限，默认 1.0）、`priority_boost_decay`（每次衰减系数，默认 0.85）

## [2.4.3] - 2026-07-06

### 变更

- **抑制 LLM 预告话术**：注入上下文时附带 `[行为规范]` 指令，要求 LLM 有记忆直接答、需调用工具时直接调用，不要预告"让我查查看"、"我搜一下"、"让我想想"等话术
- 4 个 LLM 工具描述统一改为"直接调用"措辞，移除"当你不确定时使用"等鼓励 LLM 先表达不确定性的表述

## [2.4.2] - 2026-07-06

### 变更

- **B 站搜索接入 astrbot_plugin_bilibili_ai_bot**：`BiliSource` 现优先通过 `Context` 查找已加载的 `BiliBiliBot` 插件实例，调用其 `search_bilibili_videos(keyword, ps)` 方法搜索视频
- 三级降级链路：BiliBot 插件 → `bilibili-api-python` 库 → `WebSearcher` 搜 `site:bilibili.com`
- 接口（`is_available()` / `search()` / `search_fallback()`）保持不变，[tools.py](file:///tools.py) 和 [verifier.py](file:///verifier.py) 无需改动
- 懒查找：首次调用 `search()` 或 `is_available()` 时才遍历已加载插件，避免加载顺序依赖

## [2.4.1] - 2026-07-06

### 新增

- **关心领域优先检索**：配置 `priority_topics`（逗号分隔，如 `Python,量子计算,历史`），topic 或 keywords 命中任一关键词的记忆获得 1.3x 分数加权，优先注入上下文
- **可配置注入条数**：配置 `context_inject_count`（1-10，默认 3），控制每次对话注入 LLM 的记忆条数，避免过多占用上下文窗口

### 修复

- 修复空数据库首次加载时 `schema_version` 表为空导致 `MAX(version)` 返回 NULL，触发 `'<' not supported between instances of 'NoneType' and 'int'` 的加载失败

## [2.4.0] - 2026-07-06

### 新增

- **向量混合检索**：FTS5 bm25 + 余弦相似度，权重 0.4/0.6，自动 min-max 归一化
  - 新模块 `embedder.py`：封装 AstrBot `EmbeddingProvider`，自动取第一个可用 provider（零配置）
  - 单条查询带 LRU 缓存（256 条），scope → numpy 矩阵内存缓存，写时失效
  - 无 provider 时自动降级为纯 FTS5
- **跨 scope 回退检索**：private → group → global，带 1.0/0.8/0.6 分数惩罚
- **软衰减遗忘**：记忆分数随访问时间指数衰减（半衰期默认 30 天），查询时动态计算，无需后台任务
- **文档分块**：PDF / Word / TXT / Markdown 长文档自动分块入库，每个 chunk 独立 ID
  - 新模块 `chunker.py`：`chunk_text` / `chunk_markdown` / `chunk_pdf` / `chunk_docx`
  - 滑动窗口 + overlap（默认 500 字符，重叠 50）
  - Markdown 优先按 `##` 拆 section，保留标题作为 chunk 前缀
  - `make_chunk_id(scope, parent_doc_id, chunk_idx)` 隔离 chunk ID，避免折叠 bug
- **引用溯源**：LLM 回答末尾自动追加 📚 参考资料 footer
  - 优先用 `on_llm_response` hook（如 AstrBot 支持），否则在 `on_llm_request` 注入时内嵌
  - 注入文本格式改为 `[记忆#{id}] topic（tag）: content`
- **`/memory refresh <topic>`** 命令：刷新某条记忆的访问时间，恢复衰减分数
- 3 个新导入 handler：`import_pdf` / `import_docx` / `import_txt`（base64 上传 + 分块 + 批量精炼 + 批量嵌入）
- 配置加 4 字段：`embedding_enabled`、`hybrid_search_weight`、`decay_half_life_days`、`enable_scope_fallback`
- `refiner.refine_import_batch`：批量精炼，每个 chunk 一次 LLM 调用，单 chunk 失败不影响其他

### 变更

- 版本号 `2.3.0 → 2.4.0`
- `on_llm_request` 检索从 `store.search`（纯 FTS5）改为 `store.search_hybrid`（FTS5 + 向量 + 衰减 + scope 回退）
- `_web_import_md` 长文档支持分块：单 chunk 走原路径（向后兼容），多 chunk 走批量路径
- 数据库 schema 迁移：新增 `schema_version` 表、`memories_embedding` 表、`memories` 表加 `parent_doc_id` / `last_accessed_at` 列
- 注入日志增加 `last_accessed_at` 更新（用于衰减计算）

### 优化

- 检索线程安全：查询向量在 `storage._lock` 外计算，避免阻塞写入
- Embedding provider 自动取第一个可用（零配置）
- 批量嵌入预算 256 条，避免 API 限流

### 依赖

- 新增 `numpy>=1.24.0`（向量计算）
- 新增 `pypdf>=4.0.0`（PDF 文本提取）
- 新增 `python-docx>=1.1.0`（Word 文档提取）

## [2.3.0] - 2026-07-06

### 新增

- **Dashboard 设置页**：管理页顶栏新增「⚙ 设置」按钮，弹出设置 modal
  - 可选 LLM Provider：下拉列出所有可用 Provider（含 id/name/type），选择后插件所有 LLM 调用（搜索学习/导入精炼/验证）优先使用该 Provider
  - 3 个精炼开关：搜索学习时精炼 / 导入时精炼 / 验证时精炼（验证开关本期预留，不影响行为）
  - 设置持久化到 `active_learner_settings.json`，优先级高于 `_conf_schema.json` 中的 `llm_provider_id`
  - 未选 Provider 时显示橙色警示条「⚠ 未选择 Provider，精炼将降级为原内容直存」
- 3 个新 web API：`providers` / `settings` (GET/POST)
- 新模块 `settings_store.py`：插件自管设置存储（线程锁 + 原子 os.replace 写入）
- 新模块 `refiner.py`：`KnowledgeRefiner` 把搜索结果或原始导入蒸馏为结构化记忆（摘要+关键词+置信度+依据）
  - `refine_search_results`：2 步精炼（抽取关键事实 + 结构化为知识卡）
  - `refine_import`：1 步精炼（原始文本直接蒸馏）
  - 无 Provider 或解析失败时 `refined=False` 降级返回原内容

### 变更

- 版本号 `2.2.0 → 2.3.0`
- 搜索学习流程：搜索结果 → LLM 2 步精炼（抽取事实 + 结构化）→ 存库；无 Provider 时降级为原搜索摘要
- 3 个导入 handler（text/md/zip）：增加 `refine` 参数，默认 True；调用 `refiner.refine_import` 蒸馏后存库；source 字段追加 `+精炼`/`+未精炼` 标记
- 3 个导入表单前端各加「LLM 精炼后入库」复选框
- `tools.py` 中 `SearchAndLearnTool` / `VerifyKnowledgeTool` 的 Provider 解析改为 `plugin._resolve_plugin_provider_id`（4 层 fallback）
- 删除 `tools.py` 中 `_llm_summarize` 函数（已被 `refiner.refine_search_results` 取代）
- `memory verify <topic>` 命令也改用 `_resolve_plugin_provider_id`
- `_conf_schema.json` 新增第 7 个字段 `llm_provider_id`（字符串，可空）

### 改进

- Provider 解析 4 层 fallback：Dashboard 设置 → schema 字段 → 事件 scope 默认 → 同步默认，兼容多版本 AstrBot
- 每个 Provider 候选都先经 `_provider_exists` 校验，避免选了已删除的 provider
- 设置存储与 `_conf_schema.json` 解耦：AstrBot 无 schema 写回 API，使用插件自管 JSON 文件

## [2.2.0] - 2026-07-06

### 新增

- **Dashboard 记忆导入功能**：在「记忆管理」页面顶栏点击「⬆ 导入」打开导入模态框，支持三种导入方式
  - 文本导入：直接输入主题 + 内容，POST 到 `import_text`
  - Markdown 导入：上传单个 `.md` 文件，自动剥离 YAML frontmatter、提取首个 `# 标题` 作为主题，POST 到 `import_md`
  - ZIP 批量导入：上传 ZIP 压缩包，遍历其中所有 `.md` 文件作为独立记忆导入，POST 到 `import_zip`，返回每个文件的成功/失败明细
  - 三种导入方式都支持选择 scope 类型（`global` / `private` / `group`）和 scope ID
- 3 个新的 web API 路由：`import_text` / `import_md` / `import_zip`
- 模块级 `_parse_md(content)` 辅助函数：去 YAML frontmatter + 提取首个 `# 标题`

### 变更

- 版本号 `2.1.0 → 2.2.0`
- 前端 `app.js` 新增 `bindImportEvents()`、3 个表单提交处理、Tab 切换逻辑
- `style.css` 新增 `.tabs` / `.tab-btn` / `.tab-panel` / `.scope-row` / `.hint` / `button.primary` / `textarea` / `.import-result` 样式

### 改进

- **运行时日志升级**：上下文注入日志从 `debug` 升级到 `info`，并汇总标签如 `注入上下文 [3条记忆/质疑提示/学习提示] (scope: private:u123)`，方便在 AstrBot 主面板直接看到插件运行情况
- `tools.py` 中 4 个 LLM 工具的关键节点都改为 `info` 级别输出：`搜索「xxx」` / `不知道「xxx」` / `知道「xxx」` / `已学习「xxx」` / `验证「xxx」` / `搜索 B站: xxx` 等

## [2.1.0] - 2026-07-06

### 新增

- **Dashboard 管理页面**：在 AstrBot WebUI 嵌入独立的「记忆管理」页面（`pages/manager/`），无需切换聊天身份即可跨 scope 浏览、检索、验证、删除、导出记忆
  - 8 个后端 web API：`stats` / `scopes` / `memories` / `memory/<id>` / `memory/<id>/versions` / `memory/<id>/forget` / `memory/<id>/verify` / `export`
  - 前端页面：scope 选择器、6 个统计卡片、关键词搜索、记忆表格、详情 modal（含版本历史）、触发验证、软删除、JSON 导出
  - 浅色/深色主题自适应（`prefers-color-scheme`）
- `MemoryStore` 新增 3 个跨 scope 查询方法：`list_scopes()` / `global_stats()` / `list_all_memories(page, per_page, keyword)`
- `.astrbot-plugin/i18n/zh-CN.json` 提供 page title/description 给 WebUI shell

### 变更

- 版本号 `2.0.0 → 2.1.0`
- `from astrbot.api.web` 用 `try/except` 防御导入，老版本 AstrBot（< v4.26）可正常加载插件，只是没有 Dashboard 页面

### 修复

- **修复 LLM 工具注册崩溃**：`@pydantic.dataclasses.dataclass` 装饰器会重新生成 `__init__`，覆盖手写的 `def __init__(self, plugin)`，导致 `SearchAndLearnTool(plugin)` 把 plugin 当成 `name: str` 字段的位置参数，校验失败 `'types.UnionType' object is not callable`，4 个 LLM 工具全没注册。改为无参构造 + 在 `create_tools` 工厂里用 `object.__setattr__` 注入 plugin 引用
- **修复工具调用返回值崩溃**：`ToolExecResult` 在 AstrBot 中是类型别名（`str | 其他`）而非 class，不能 `ToolExecResult("文本")` 构造调用，会抛 `TypeError: 'types.UnionType' object is not callable`。改为直接返回 string
- `BiliSource` 补 `is_available()` 实例方法，与 `main.py` / `tools.py` / `verifier.py` 中的 `self.bili_source.is_available()` 调用方式一致

## [2.0.0] - 2026-07-05

### 新增

- **SQLite + FTS5 存储后端**：替换原 JSON 文件存储，支持全文检索
- **双层 scope 隔离**：`private`（私聊）/ `group`（群聊）/ `global`（全局）三种作用域，互不串扰
- **质疑多源交叉验证**：3 轮 LLM 自辩论 + 来源一致性检查 + 置信度自动调整
- **版本化记忆**：每次内容更新或软删除都写入 `memory_versions` 表留痕，可追溯历史
- **主动学习触发**：基于关键词模式识别，自动建议或触发学习
- **4 个 LLM FunctionTool**：`search_and_learn` / `recall_memory` / `verify_knowledge` / `search_bilibili`（按需启用）
- **B 站搜索源**：可选启用 `bilibili-api-python`，未安装时自动回退到 `site:bilibili.com` 网页搜索
- **`/memory` 命令组**：8 个子命令（`stats` / `list` / `search` / `info` / `forget` / `verify` / `export` / `versions`）
- **自动记忆注入**：通过 `extra_user_content_parts` 把相关记忆注入 LLM 上下文
- 模块化拆分：`main.py` / `storage.py` / `models.py` / `tools.py` / `searcher.py` / `bili_source.py` / `verifier.py` / `triggers.py`

### 变更

- 配置项从原版的扁平结构改为 `_conf_schema.json` 声明式
- 数据库表 `memories` + `memories_fts`（FTS5 虚拟表）+ 3 个同步触发器

## [1.0.0] - 2026-07-04

### 新增

- 项目初始版本
- 基础记忆库功能（JSON 文件存储）
- 单 scope 记忆管理
- 基本的搜索和学习能力
