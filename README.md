# 凝心溯溪-知

> 凝心溯溪系列知识模块：面向知识学习、检索与验证，支持自动上下文注入、自主多源检索、手动 URL / MediaWiki 来源、按用户/群聊隔离的 SQLite 记忆库、交叉验证与版本化。

> **凝心溯溪系列** 当前完整插件清单为知、言、序、情、境、声、核、临：各插件职责独立、互不冲突，可按需组合使用，覆盖知识学习、对话调节、身份管理、关系状态、环境感知、语音、更新管理与具身桥接。

| 字 | 模块 | 说明 |
|----|------|------|
| [知](https://github.com/qsbb/astrbot_plugin_active_learner) | 知识学习 | 自动检索注入、多源学习、交叉验证（本插件） |
| [言](https://github.com/qsbb/astrbot_plugin_conversation_flow) | 对话调节 | 沉默判断、智能分段、插话衔接 |
| [序](https://github.com/qsbb/astrbot_plugin_identity_guardian) | 身份管理 | 关系感知、权限边界、群组行动 |
| [情](https://github.com/qsbb/astrbot_plugin_relationship) | 关系状态 | 情绪、好感、信任、熟悉度状态记录与只读建议 |
| [境](https://github.com/qsbb/astrbot_plugin_environment_awareness) | 环境感知 | 时间、天气、空气质量、预警与环境关心候选 |
| [声](https://github.com/qsbb/astrbot_plugin_voice_hub) | 语音合成 | 双 TTS 后端、多音色管理、AI 导演 |
| [核](https://github.com/qsbb/astrbot_plugin_update_manager) | 更新管理 | 安全检查、计划、串行更新与回滚 |
| [临](https://github.com/qsbb/astrbot_plugin_embodiment_bridge) | 具身桥接 | Quest 客户端桥接、实时对话与空间感知 |

## 当前实现信息

- 版本号以 `metadata.yaml` 的 `version` 为唯一事实源，代码侧引用 `constants.PLUGIN_VERSION`；逐版变更见 `CHANGELOG.md`。本文档不登记具体版本号，避免与发布分叉。
- AstrBot 兼容范围：`astrbot_version: ">=4.16,<5"`（已在 metadata 中声明；下界覆盖 `on_llm_response` 钩子引入版本）。
- 命令入口：`/memory` 命令组；主要包括 `stats`、`list`、`search`、`info`、`forget`、`verify`、`export`、`versions`、`refresh`。
- 事件钩子：`on_llm_request` priority=700（记忆召回注入与检索策略）；`on_llm_response` priority=700（引用页脚与后置异步学习分析，运行时探测钩子可用性，不可用时降级为请求侧内嵌）。
- LLM Provider 解析顺序：Dashboard 设置 → schema 字段 `llm_provider_id` → “核”统一模型路由（`series.model_router@1.0`，未安装或契约不可用时透明跳过）→ 当前对话默认 → 同步兜底。
- 页面/API 入口：插件注册多项 Web API；当前 README 不将其表述为固定管理页面，具体以运行时能力为准。

### 系列诊断日志

- 诊断会捕获本插件自有 logger 的 `DEBUG` 到 `CRITICAL` 事件；内存缓冲最多保留 1000 条，日志页单次最多读取 1000 条、浏览器最多暂存 10000 条。每条记录由“核”先显示插件中文名，再显示时间、级别和事件。
- 插件把必要的生命周期事件、警告和错误写入内存环形缓冲，并通过 `series.diagnostics@1.0` 只读契约供“核”的日志页汇总查看。
- 契约完整声明系列 ID、插件 ID、中文简称、内存存储及读取/清空能力；仓库元数据匹配后由“核”自动发现，不需要修改“核”。
- 这条诊断通道与 AstrBot 主日志隔离，不会转发诊断记录，也不会读取 AstrBot 全局日志；它只收集本插件自身已经产生的日志和明确诊断事件。
- 自动捕获事件会保留模块、函数、行号、异常类型，以及最长 2000 字符的脱敏日志正文；在“核”的日志页点击事件即可展开。插件不会额外读取聊天消息，但若本插件原有日志本身含有用户文本片段，该片段会在脱敏、截断后进入内存详情。
- 写入前会隐藏令牌、账号标识等敏感字段，并截断过长内容。缓冲仅存在于当前进程，清空、重启或热重载后自动消失并更换流标识。
- “核”不是运行依赖：没有安装或没有启用“核”时，知仍照常学习、检索和验证，只是缺少统一日志查看入口。

### 系列控制

- 插件提供 `series.control@1.0`，供“核”统一接管三个非秘密运行字段：`embedding_enabled`、`context_inject_count`（1~3）、`search_top_k`（1~20）。
- 接管覆盖层与插件自身配置分离，原子持久化在插件数据目录的 `series-control.json`；处于 managed 模式时覆盖值生效，“核”不可用、快照损坏或校验失败时回退插件原生（native）配置。
- 修改按 revision 乐观校验：未知字段、类型或取值越界、revision 冲突都会被拒绝并返回原因；应用失败自动回滚到修改前状态。关闭统一接管后无需重启即可恢复原生配置。
- “核”不是运行依赖：未安装或未启用“核”时，以上字段完全由本插件自身配置决定。

## 简介

`astrbot_plugin_active_learner` 是一个为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 设计的知识学习、检索与验证插件。它通过长期记忆能力，让机器人能够：

- **检索即注入**：每次 LLM 请求前，自动用 FTS5 全文检索相关记忆并注入上下文
- **自主检索**：由当前对话模型结合不确定性、时效性、实体完整度和答错代价，判断是否搜索外部来源
- **受控知识来源**：可在管理页手动添加固定网页或 MediaWiki 百科，与 Web、B 站来源统一调度
- **实体完整匹配**：本地候选必须覆盖主题中的全部核心实体，避免共同宽泛词把另一条高置信记忆误当答案
- **可选关联重建**：从现有记忆派生受预算约束的线索图，在普通召回不足的复杂问题中补全比较、因果、时间线和依赖关系
- **三层作用域隔离**：按"私聊 / 群聊 / 全局"三层 scope 隔离记忆，互不污染
- **质疑纠错**：用户质疑时触发多源搜索 + LLM 自辩论 + 交叉验证，并保留历史版本

适用场景：长期对话机器人、群聊问答助手、知识库型陪伴 AI。

## 核心特性

### 1. 自动记忆注入（无感召回）

挂在 `on_llm_request` 钩子上，每次 LLM 请求前：

1. 提取用户消息文本
2. 用 FTS5 在当前 scope 检索 Top-3 相关记忆
3. 把检索结果登记为结构化提示片段，并保留 `extra_user_content_parts` 直接注入作为独立运行 fallback
4. 同时检测质疑和对比对象缺失，并向主模型提供连续强度的语义检索策略

安装“言”时，序、知、情登记的片段由言按“安全边界 → 知识事实 → 关系表达”稳定排序、
按 key 与内容去重后合并为一次协同注入；未安装言时，本插件仍按原路径独立工作。

### 2. 自主检索与主动学习

LLM 工具 `search_and_learn`：

1. 并行查询已启用的手动 URL / MediaWiki、Web 搜索服务和可选 B 站补充来源
2. 收集多源搜索结果片段
3. 让当前 LLM 总结为 200 字以内的简洁知识
4. 自动提取关键词（中文 2 字以上、英文 3 字以上）
5. 计算初始置信度（基于来源数，0.3 ~ 0.85）
6. 写入 SQLite + FTS5 索引

检索决策不再依赖“搜一下 / 查一下”等硬编码词表，也不会为了判断是否搜索而额外调用一次 LLM。插件把 `learn_weight` 作为连续的“自主检索倾向”交给当前主模型；模型在本次正常推理中综合事实不确定性、时效性、实体是否完整匹配和答错代价后决定是否调用工具。

- 闲聊、创作、主观交流或已有可靠且实体完整的依据时不搜索。
- 具体但不确定、易变化、实体冲突或用户要求核实时优先搜索。
- 旧记忆过时或不覆盖当前实体时，模型可设置 `force_refresh=true` 跳过本地短路。
- 一次回答最多启动一条搜索链；`search_and_learn` 无结果或失败后，不再切换其它搜索工具连续重试，而是明确说明本次未能核实。

`learn_weight=0.7` 表示较积极的检索倾向，不代表 70% 的消息会搜索。完整语义策略只在没有可用记忆时注入；已有实体完整的可靠记忆时沿用轻量回复路径。普通回复不会多一次模型请求，因此无检索时的回复速度基本不变；真正决定搜索后，耗时来自所选外部来源和后续精炼。

领域白名单仍是更高层约束：关闭跨领域回复且问题不在白名单时，插件不会绕过限制发起搜索。

#### 手动 URL / MediaWiki 来源

在插件管理页打开“设置 → URL 来源”即可添加：

- **固定网页**：每次按需抓取指定页面，只有页面正文包含查询主题的核心词时才作为来源返回。
- **MediaWiki 百科**：填写 Wiki 首页、词条页或 `api.php` 地址，插件会推导 API 并动态搜索相关词条。

来源清单保存在插件数据目录的 `url_knowledge_sources.json`。最多保存 20 个来源，每次搜索最多并行查询 5 个已启用来源；单来源超时 5 秒，并受联网总开关和来源优先级控制。管理员应只添加自己信任的站点。

### 3. 三层作用域隔离的 SQLite 记忆库

| Scope 类型 | scope_id | 说明 |
|---|---|---|
| `private` | user_id | 私聊隔离，每个用户独立 |
| `group` | group_id | 群内共享，群成员均可读写 |
| `global` | `global` | 全局共享（仅管理员可写） |

私聊与群聊之间是硬边界：私聊只读取当前用户私聊库，群聊只读取当前群库，任何情况下
都不会跨到其他用户或其他群。全局库是唯一例外：`enable_scope_fallback`（默认开启）
允许私聊/群聊额外读取 global 共享知识；关闭后仅明确处于全局作用域时才读取全局库。
检索、详情、验证、遗忘、版本查询、导出与写入都复用同一作用域。

存储结构（`storage.py`）：
- `memories` 表：主题、内容、关键词、来源、置信度、验证状态、访问计数、时间戳
- `memories_fts` 虚拟表：FTS5 全文索引（`unicode61` 分词；中文子串召回由关键词与可选向量检索补充）
- `memory_versions` 表：质疑纠错 / 验证失败时的版本快照
- `memory_graph_edges` 表：由现有记忆确定性派生的线索与关系索引；内容仍只读取 `memories`
- 触发器自动同步 `memories` ↔ `memories_fts`

容量淘汰：超过 `max_entries` 时，按 `置信度×0.6 + 访问频率×0.4` 升序淘汰。

### 4. 质疑多源交叉验证

LLM 工具 `verify_knowledge` 或指令 `/memory verify <主题>` 触发：

1. **多源搜索**：手动 URL / MediaWiki + Web 主搜 + 真假验证搜 + B 站（按配置启用）
2. **LLM 自辩论 3 轮**：
   - Round A（支持方）：基于来源为原说法找支持证据
   - Round B（质疑方）：反驳支持方论证，挑事实错误 / 来源偏差 / 逻辑漏洞
   - Round C（仲裁）：输出 `VERDICT / CONFIDENCE / CONTENT / REASON`
3. **交叉验证**：不同来源类型 ≥2 种且仲裁结论为 correct/wrong，或来源数 ≥3 且结论为 correct，视为"一致"；纯 LLM 验证（来源不足或配置为 `llm`）直接按一致处理
4. **版本化**：内容有变化或置信度下降 >0.15 时，写入 `memory_versions` 留痕
5. **更新置信度**：
   - correct + 一致 → +0.20
   - correct + 不一致 → +0.10
   - correct + 纯 LLM → +0.15
   - wrong → -0.25（最低 0.1），并用仲裁给出的修正内容替换原文
   - partial → +0.05，有修正内容时同步更新
   - inconclusive → 不变
6. **verified 标记**：结论为 correct 或 partial 且更新后置信度 ≥ 0.5 时置为已验证

### 5. 群黑话被动学习（默认关闭）

- **被动捕获**：`enable_slang_capture=true` 后，在 `on_llm_request` 中用正则提取候选黑话词，不调用 LLM；按说话人去重、带防刷屏计数时间窗，候选攒够批量后合并为 1 次 LLM 批量学习。
- **命中注入**：消息中提到已学词条时，按确定性子串匹配把释义注入上下文（默认开启，每条消息最多 3 条），统一带「群黑话/通用梗 · 仅供参考」标签。
- **跨群晋升**：同一词条被足够多的不同群学到且至少一群判定为通用梗时，自动晋升到 global 作用域——这是 global 唯一的自动写入通道；管理员也可在管理页手动晋升、拒绝或拉黑候选。
- **管理审核**：管理页「🏷 黑话」页签维护候选队列、已学词条与拉黑名单。
- **LLM 查询**：`lookup_slang` 工具供模型查询已学黑话释义；只查被动学习成果，不联网、不学习新词。

## 安装

将本插件目录放入 AstrBot 的 `plugins/` 文件夹，重启 AstrBot 即可。

### 依赖

```
aiohttp>=3.8.0
numpy>=1.24.0
pypdf>=4.0.0
python-docx>=1.1.0
```

（已写入 `requirements.txt`，AstrBot 会自动安装）

可选依赖（显式启用 B 站补充搜索）：

B 站来源默认关闭。只有 `enable_bilibili=true` 时，它才会参与通用搜索、主动学习与事实核验并注册独立工具；仅安装依赖或 B 站插件不会绕过开关。默认来源顺序为 `url,web,bilibili`，视频标题与简介排在手动资料和网页资料之后，只作为补充证据。

本插件 B 站搜索功能采用三级降级链路，按以下优先级依次尝试：

1. **`astrbot_plugin_bilibili_ai_bot` 插件（推荐，优先使用）**
   - 仓库地址：https://github.com/chenluQwQ/astrbot_plugin_bilibili_ai_bot
   - 安装该插件、完成 `/bili登录` 并开启 `enable_bilibili` 后，本插件会优先通过它执行 B 站搜索
   - 启动时若检测到该插件已加载，会在日志中输出「已连接 astrbot_plugin_bilibili_ai_bot」

2. **`bilibili-api-python` 库（次选）**

   ```bash
   pip install bilibili-api-python
   ```

3. **`site:bilibili.com` 网页搜索（兜底）**

> 以上三种方式任一可用即可，不安装也能用——会自动回退到网页搜索。

## 配置

在 AstrBot 管理面板或 `_conf_schema.json` 中配置：

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `max_entries` | int | 500 | 单 scope 最大记忆条数，超出按置信度+访问频率淘汰 |
| `min_confidence` | float | 0.3 | 最低置信度阈值，低于此值优先淘汰 |
| `enable_active_learn_hint` | bool | true | 启用主模型语义检索策略；不会增加一次额外 LLM 调用 |
| `learn_weight` | float | 0.7 | 自主检索倾向（0~1），是连续参考值而非搜索概率 |
| `search_top_k` | int | 5 | `search_and_learn` 与 `/memory search` 返回的最大结果数（1~20） |
| `default_confidence` | float | 0.6 | 手动导入或未经 LLM 精炼时新记忆的默认置信度 |
| `chunk_size` | int | 500 | 导入长文档时的分块大小（字符） |
| `chunk_overlap` | int | 50 | 相邻分块的重叠字符数 |
| `enable_bilibili` | bool | false | 启用 B 站补充来源；关闭时不注册工具，也不参与搜索、学习或核验 |
| `verifier_search_source` | string | `auto` | 验证搜索源：`auto` / `web` / `bilibili` / `web+bilibili` / `llm`（纯 LLM 不搜索） |
| `enable_web_search` | bool | true | 手动 URL、MediaWiki、Web 与 B 站来源的联网总开关 |
| `external_search_first_result_grace_seconds` | float | 0.5 | 多源并行搜索时，首个结果返回后最多再等其它来源的秒数 |
| `web_search_only_highest_priority` | bool | false | 只使用顺序中第一个当前可用的来源 |
| `knowledge_source_priority` | string | `url,web,bilibili` | 外部来源顺序；`url` 代表管理页维护的手动来源 |
| `knowledge_domain_scope` | string | 空 | 兴趣领域白名单（逗号分隔）；为空表示不限制 |
| `enable_cross_domain` | bool | true | 允许跨领域回复；关闭后未命中白名单的问题提示模型明确回复不了解 |
| `cross_domain_exclude_admin` | bool | true | 跨领域搜索限制排除管理员 |
| `llm_provider_id` | string | 空 | 插件专用 LLM Provider；留空回退统一模型路由与当前对话模型，Dashboard 同名字段优先 |
| `llm_max_concurrency` | int | 1 | 精炼、验证与后台学习共用的插件内部 LLM 并发上限（1~4） |
| `embedding_enabled` | bool | true | 启用向量检索；无可用 Embedding Provider 时自动降级为纯 FTS5 |
| `hybrid_search_weight` | string | `0.4,0.6` | 全文检索与语义向量的分数权重（`fts,vec`，相加为 1） |
| `decay_half_life_days` | int | 30 | 记忆分数衰减半衰期（天）；30 天未访问则分数减半 |
| `enable_scope_fallback` | bool | true | 私聊/群聊额外读取 global 共享知识；不同私聊、不同群聊始终隔离 |
| `graph_memory_enabled` | bool | false | 启用关联图补充检索；默认关闭，不改变原召回行为 |
| `graph_reconstruction_mode` | string | `fast` | `off` 关闭；`fast` 仅本地检索；`smart` 在复杂问题上最多额外调用一次 LLM 排序 |
| `graph_max_hops` | int | 2 | 最大 1-2 跳；始终遵守当前 scope 与全局知识开关 |
| `priority_topics` | string | 空 | 关心的领域（逗号分隔），命中记忆获得分数加权并优先注入 |
| `auto_learn_topic_limit` | int | 100 | 「🎯 主动学习」按领域批量学习的条数上限（1~500） |
| `context_inject_count` | int | 3 | 每次对话最多注入的记忆条数；大于 3 自动收紧为 3 |
| `priority_boost_max` | float | 1.3 | 关心领域命中时的初始分数乘子（1.0 等于关闭） |
| `priority_boost_min` | float | 1.0 | 连续未命中关心领域时 boost 衰减的下限 |
| `priority_boost_decay` | float | 0.85 | 每次未命中关心领域时 boost 的衰减系数 |
| `enable_slang_capture` | bool | false | 启用群黑话被动捕获；候选攒够批量后 1 次 LLM 批量学习 |
| `admin_ids` | string | 空 | 管理员 QQ 号（逗号分隔）；非空时仅管理员可触发主动学习，同时读取全局 `wl_admin` |
| `slang_capture_interval_hours` | int | 24 | 每 scope 黑话批量学习的最小间隔（小时） |
| `slang_capture_batch_size` | int | 5 | 触发一次批量学习的候选词数量 |
| `slang_capture_min_occurrences` | int | 2 | 候选词计入批量学习的最低出现次数 |
| `slang_capture_scope_only_group` | bool | true | 仅捕获群聊消息；关闭后同时捕获私聊 |
| `slang_capture_min_speakers` | int | 2 | 候选词最低说话人数，防单人刷屏投毒 |
| `slang_capture_count_interval_seconds` | int | 300 | 同一说话人对同一候选词重复计数的最小间隔（秒） |
| `enable_slang_recall` | bool | true | 消息命中已学黑话时直接注入释义（确定性匹配，不调 LLM） |
| `slang_recall_max_per_msg` | int | 3 | 每条消息最多注入的黑话释义条数 |
| `slang_promotion_enabled` | bool | true | 启用黑话跨群晋升 global（global 唯一自动写入通道） |
| `slang_promotion_min_groups` | int | 2 | 晋升 global 所需的最低群数 |
| `slang_promotion_external_verify` | bool | false | 晋升前外部验证（实验）：联网能搜到该词也允许晋升 |

## 使用

### 管理页面

在 AstrBot 管理面板的插件页面中打开「凝心溯溪-知 · 知识管理」，可以：

- 查看记忆统计，按 scope 筛选、搜索、分页浏览全部记忆，并执行详情查看、验证、删除与批量操作
- 导入文本 / Markdown / ZIP / PDF / Word / TXT，或从 AstrBot 内置知识库挑选文档导入
- 在「⚙ 设置 → URL 来源」中维护手动网页 / MediaWiki 来源，调整常用设置；「📋 配置」页可编辑全部配置项，保存即时生效
- 展开「插件日志」查看本插件日志，查看上次验证详情与诊断信息
- 通过「🎯 主动学习」按关心领域批量学习并实时查看进度
- 在「🏷 黑话」页签审核候选队列、已学词条与拉黑名单，支持手动晋升全局与拉黑

### LLM 工具（自动注册，对话中自然触发）

| 工具 | 作用 |
|---|---|
| `search_and_learn` | 搜索 URL / 百科 / Web / B 站来源 → LLM 总结 → 存入记忆库；`force_refresh=true` 时跳过旧记忆 |
| `recall_memory` | 从记忆库检索已学知识 |
| `verify_knowledge` | 多源搜索 + LLM 自辩论 + 交叉验证某条记忆 |
| `save_memory` | 标记对话中值得记忆的知识点，插件后台调用 LLM 精炼后异步入库 |
| `lookup_slang` | 查询已被动学习的群黑话 / 梗释义（不联网、不学习新词） |
| `search_bilibili` | 搜索 B 站视频（仅 `enable_bilibili=true` 时注册） |

你不需要手动调用，LLM 会在合适时机自己调用。

### 指令（用户直接操作）

```
/memory stats                 # 查看当前作用域记忆库统计
/memory list [页码]           # 分页列出记忆
/memory search <关键词>       # 搜索记忆
/memory info <主题>           # 查看某条记忆详情
/memory forget <主题>         # 软删除（留版本痕）
/memory verify <主题>         # 手动触发多源验证
/memory export                # 导出当前 scope 记忆为 JSON
/memory versions <主题>       # 查看历史版本
/memory refresh <主题>        # 刷新访问时间，恢复衰减分数
```

### 示例对话

```
用户：什么是量子纠缠？
（LLM 调用 search_and_learn → 总结存库 → 回答）
机器人：[已学习"量子纠缠"，置信度 72%] ...

用户：你说的不对吧，量子纠缠不能传递信息
（命中质疑模式 → 提示 LLM 调用 verify_knowledge）
机器人：[验证中... 多源搜索 → 自辩论 → 仲裁]
        验证结论：⚠️ 部分正确
        更新后置信度：45%
```

## 项目结构

```
astrbot_plugin_active_learner/
├── main.py              # 插件主入口、钩子、指令组
├── models.py            # Scope / MemoryEntry / MemoryVersion / SearchHit
├── storage.py           # SQLite + FTS5 存储层（含触发器、淘汰、版本化、黑话表）
├── retrieval.py         # 本地召回、来源调度与语义检索策略
├── runtime.py           # 实体覆盖、请求状态、外部搜索并发控制
├── searcher.py          # Web 搜索服务 + URL 抓取
├── url_sources.py       # 手动网页 / MediaWiki 来源注册与持久化
├── bili_source.py       # B 站搜索源（可选）
├── constants.py         # 版本常量 PLUGIN_VERSION
├── verifier.py          # 多源验证 + LLM 自辩论 + 交叉验证
├── tools.py             # LLM FunctionTool 定义（B 站工具按配置注册）
├── llm_service.py       # 插件内部 LLM 统一调用与 provider 解析
├── model_router.py      # “核” series.model_router@1.0 只读路由适配
├── series_control.py    # series.control@1.0 接管覆盖层适配
├── refiner.py           # 对话片段 / 导入文档的 LLM 精炼
├── importer.py          # 文本 / Markdown / ZIP / PDF / Word 导入
├── chunker.py           # 导入分块
├── embedder.py          # 可选向量检索（无 Embedding Provider 时降级）
├── graph_memory.py      # 关联图线索索引（默认关闭）
├── learning.py          # 后置学习与群黑话批量学习（LearningMixin）
├── slang_capture.py     # 群黑话被动捕获
├── slang_recall.py      # 黑话命中注入与 lookup_slang 回复
├── slang_promotion.py   # 黑话跨群晋升 global
├── plugin_logger.py     # 插件日志与系列诊断内存缓冲
├── request_context.py   # 凝心溯溪统一请求上下文（系列共享副本）
├── config_manager.py    # 配置读取与热应用
├── web_api.py           # 管理页 API、配置热应用与 URL 来源管理
├── pages/manager/       # 管理页前端
├── triggers.py          # 质疑检测正则模式
├── tests/               # 回归测试
├── _conf_schema.json    # 配置 schema
├── metadata.yaml        # AstrBot 插件元数据
├── requirements.txt     # 依赖
└── __init__.py
```

## 工作流程图

```
用户消息
   │
   ▼
[on_llm_request 钩子]
   ├─ FTS5 / 向量检索记忆 → 核心实体覆盖检查 → 注入上下文
   ├─ 命中质疑模式？→ 提示 verify_knowledge
   └─ 注入连续强度的语义检索策略（不增加 LLM 请求）
   │
   ▼
LLM 在当前推理中自主判断（可能调用工具）
   ├─ search_and_learn  → URL / 百科 / Web / B站 → LLM 总结 → 写库
   ├─ recall_memory     → 检索记忆返回
   ├─ verify_knowledge  → 多源搜 → 自辩论 → 交叉验证 → 更新+版本化
   ├─ save_memory       → 标记知识点 → 后台精炼异步入库
   ├─ lookup_slang      → 查询已学黑话释义
   └─ search_bilibili   → B 站 API 或网页回退（按开关注册）
   │
   ▼
SQLite 持久化（memories + memories_fts + memory_versions）
```

## 设计取舍

- **为什么保留 FTS5 并可选向量检索**：FTS5 零依赖、精确词命中快；有 Embedding Provider 时再合并语义召回。两者都受 scope 硬隔离和实体完整覆盖约束。
- **为什么不再用搜索关键词触发器**：自然语言表达无法靠有限词表稳定覆盖，且容易把普通陈述误判成命令。让当前主模型在原本的一次推理内评估风险，既减少硬规则误判，也不增加普通回复的模型往返。
- **为什么注入到 `extra_user_content_parts` 而非 `system_prompt`**：后者会破坏 LLM 的 prompt 缓存，每次都重新编码全部 system prompt。
- **为什么 LLM 自辩论要 3 轮而不是 1 轮**：单轮 LLM 容易"附和"用户或编造来源；支持方 vs 质疑方对抗能显著降低单边幻觉。
- **为什么软删除留版本痕**：用户可能误删，且验证失败的历史记录对追溯有用。

## 兼容性

- AstrBot 新版（`self.config` 自动注入）与旧版（`context.get_config()`）均兼容
- LLM provider 不可用时所有工具会优雅降级（返回提示文本而非报错）
- `extra_user_content_parts` 不可用时降级到 `system_prompt` 注入
- 显式启用 B 站来源后，独立 B 站工具在库未安装时自动回退到网页搜索；关闭开关时不会因检测到依赖而自行启用
- 安装凝心溯溪-言时会参与结构化提示片段编排；未安装或版本不兼容时继续使用直接注入 fallback
- “核”未安装或 `series.control@1.0` / `series.model_router@1.0` 契约不可用时，运行字段与 LLM Provider 解析均回退本插件自身配置与 AstrBot 原生默认

## 数据存储位置

- 数据库：`<AstrBot 数据目录>/astrbot_plugin_active_learner/memory.db`
- 手动来源：`<AstrBot 数据目录>/astrbot_plugin_active_learner/url_knowledge_sources.json`
- 系列控制覆盖层：`<AstrBot 数据目录>/astrbot_plugin_active_learner/series-control.json`
- 导出文件：`<AstrBot 数据目录>/astrbot_plugin_active_learner/memory_export_<scope>_<id>.json`

## 维护约定

任何可观察功能、配置项或安全边界的增删改，必须在同一批变更中同步 README、CHANGELOG 的
`Unreleased`、配置 schema 与回归测试。版本号在实现、文档和验证完成后由发布者确认。

## License

本插件遵循 MIT License。

## 仓库

源码：https://github.com/qsbb/astrbot_plugin_active_learner
