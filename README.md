# 凝心溯溪-忆

> 凝心溯溪系列记忆模块：自动检索注入上下文、主动多源学习新知识、按用户/群聊双层隔离的 SQLite 记忆库、质疑时多源交叉验证与版本化。

> **凝心溯溪系列** 是一套功能互补的 AstrBot 插件集合，旨在构建从记忆学习、对话调节、身份管理到语音合成的完整对话能力链。各插件职责独立、互不冲突，可按需组合使用。

| 字 | 模块 | 说明 |
|----|------|------|
| [忆](https://github.com/qsbb/astrbot_plugin_active_learner) | 记忆学习 | 自动检索注入、多源学习、交叉验证（本插件） |
| [言](https://github.com/qsbb/astrbot_plugin_conversation_flow) | 对话调节 | 沉默判断、智能分段、插话衔接 |
| [序](https://github.com/qsbb/astrbot_plugin_identity_guardian) | 身份管理 | 关系感知、权限边界、群组行动 |
| [声](https://github.com/qsbb/astrbot_plugin_voice_hub) | 语音合成 | 双 TTS 后端、多音色管理、AI 导演 |

## 简介

`astrbot_plugin_active_learner` 是一个为 [AstrBot](https://github.com/AstrBotDevel/AstrBot) 设计的"主动学习 + 长期记忆"插件。它让机器人摆脱"金鱼记忆"，能够：

- **检索即注入**：每次 LLM 请求前，自动用 FTS5 全文检索相关记忆并注入上下文
- **主动学习**：当用户问到知识盲区时，自动搜索网络（多源）→ LLM 总结 → 存入记忆库
- **双层隔离**：按"私聊 / 群聊 / 全局"三层 scope 隔离记忆，互不污染
- **质疑纠错**：用户质疑时触发多源搜索 + LLM 自辩论 + 交叉验证，并保留历史版本

适用场景：长期对话机器人、群聊问答助手、知识库型陪伴 AI。

## 核心特性

### 1. 自动记忆注入（无感召回）

挂在 `on_llm_request` 钩子上，每次 LLM 请求前：

1. 提取用户消息文本
2. 用 FTS5 在当前 scope 检索 Top-3 相关记忆
3. 通过 `extra_user_content_parts` 注入（不破坏 system prompt 缓存）
4. 同时检测是否为"质疑句"和"主动学习触发句"，分别附加提示

### 2. 主动学习（关键词触发 + LLM 工具调用）

LLM 工具 `search_and_learn`：

1. 调用 `WebSearcher` 搜 DuckDuckGo（可选 B 站）
2. 收集多源搜索结果片段
3. 让当前 LLM 总结为 200 字以内的简洁知识
4. 自动提取关键词（中文 2 字以上、英文 3 字以上）
5. 计算初始置信度（基于来源数，0.3 ~ 0.85）
6. 写入 SQLite + FTS5 索引

触发方式：
- **关键词提示**：消息命中 `什么是/解释一下/不懂/科普一下/为什么...` 等模式且记忆库无答案时，提示 LLM 调用工具
- **LLM 自主调用**：LLM 自己判断需要学习时直接调用

### 3. 双层隔离的 SQLite 记忆库

| Scope 类型 | scope_id | 说明 |
|---|---|---|
| `private` | user_id | 私聊隔离，每个用户独立 |
| `group` | group_id | 群内共享，群成员均可读写 |
| `global` | `global` | 全局共享（仅管理员可写） |

存储结构（`storage.py`）：
- `memories` 表：主题、内容、关键词、来源、置信度、验证状态、访问计数、时间戳
- `memories_fts` 虚拟表：FTS5 全文索引（`unicode61` 分词，原生支持中文按字匹配）
- `memory_versions` 表：质疑纠错 / 验证失败时的版本快照
- 触发器自动同步 `memories` ↔ `memories_fts`

容量淘汰：超过 `max_entries` 时，按 `置信度×0.6 + 访问频率×0.4` 升序淘汰。

### 4. 质疑多源交叉验证

LLM 工具 `verify_knowledge` 或指令 `/memory verify <主题>` 触发：

1. **多源搜索**：Web 主搜 + 真假验证搜 + B 站（可选）
2. **LLM 自辩论 3 轮**：
   - Round A（支持方）：基于来源为原说法找支持证据
   - Round B（质疑方）：反驳支持方论证，挑事实错误 / 来源偏差 / 逻辑漏洞
   - Round C（仲裁）：输出 `VERDICT / CONFIDENCE / CONTENT / REASON`
3. **交叉验证**：≥2 种来源类型 且 结论为 correct/wrong 才算"一致"
4. **版本化**：内容差异 >30 字符或置信度下降 >0.15 时，写入 `memory_versions` 留痕
5. **更新置信度**：
   - correct + 一致 → +0.15
   - correct + 不一致 → +0.05
   - wrong → -0.3（最低 0.1）
   - partial → -0.1（最低 0.2）
   - inconclusive → 不变
6. **verified 标记**：仅当 `correct + 一致 + 置信度 ≥ 0.6` 三条同时成立才置为已验证

## 安装

将本插件目录放入 AstrBot 的 `plugins/` 文件夹，重启 AstrBot 即可。

### 依赖

```
aiohttp>=3.8.0
```

（已写入 `requirements.txt`，AstrBot 会自动安装）

可选依赖（启用 B 站搜索）：

本插件 B 站搜索功能采用三级降级链路，按以下优先级依次尝试：

1. **`astrbot_plugin_bilibili_ai_bot` 插件（推荐，优先使用）**
   - 仓库地址：https://github.com/chenluQwQ/astrbot_plugin_bilibili_ai_bot
   - 安装该插件并完成 `/bili登录` 后，本插件会自动接管 B 站搜索请求
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
| `enable_active_learn_hint` | bool | true | 命中"什么是/不懂/解释一下"等关键词时提示 LLM 学习 |
| `enable_bilibili` | bool | false | 启用 B 站搜索源（需自行安装 `bilibili-api-python`） |
| `debate_rounds` | int | 2 | 质疑验证的自辩论轮数（2 = 支持方→质疑方→仲裁） |
| `ddg_fallback` | bool | true | 无内置搜索时用 DuckDuckGo 兜底 |

## 使用

### LLM 工具（自动注册，对话中自然触发）

| 工具 | 作用 |
|---|---|
| `search_and_learn` | 搜索网络 → LLM 总结 → 存入记忆库 |
| `recall_memory` | 从记忆库检索已学知识 |
| `verify_knowledge` | 多源搜索 + LLM 自辩论 + 交叉验证某条记忆 |
| `search_bilibili` | 搜索 B 站视频（可选） |

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
├── storage.py           # SQLite + FTS5 存储层（含触发器、淘汰、版本化）
├── searcher.py          # DuckDuckGo HTML 搜索 + URL 抓取
├── bili_source.py       # B 站搜索源（可选）
├── verifier.py          # 多源验证 + LLM 自辩论 + 交叉验证
├── tools.py             # 4 个 LLM FunctionTool 定义
├── triggers.py          # 主动学习 / 质疑检测正则模式
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
   ├─ FTS5 检索记忆 → 注入上下文
   ├─ 命中质疑模式？→ 提示 verify_knowledge
   └─ 命中学习模式且无记忆？→ 提示 search_and_learn
   │
   ▼
LLM 推理（可能调用工具）
   ├─ search_and_learn  → 多源搜 → LLM 总结 → 写库
   ├─ recall_memory     → 检索记忆返回
   ├─ verify_knowledge  → 多源搜 → 自辩论 → 交叉验证 → 更新+版本化
   └─ search_bilibili   → B 站 API 或网页回退
   │
   ▼
SQLite 持久化（memories + memories_fts + memory_versions）
```

## 设计取舍

- **为什么用 FTS5 而不是向量检索**：FTS5 零依赖、原生支持中文 unicode61 分词、单文件 SQLite 部署简单，对中小型记忆库（<1万条）足够好。
- **为什么注入到 `extra_user_content_parts` 而非 `system_prompt`**：后者会破坏 LLM 的 prompt 缓存，每次都重新编码全部 system prompt。
- **为什么 LLM 自辩论要 3 轮而不是 1 轮**：单轮 LLM 容易"附和"用户或编造来源；支持方 vs 质疑方对抗能显著降低单边幻觉。
- **为什么软删除留版本痕**：用户可能误删，且验证失败的历史记录对追溯有用。

## 兼容性

- AstrBot 新版（`self.config` 自动注入）与旧版（`context.get_config()`）均兼容
- LLM provider 不可用时所有工具会优雅降级（返回提示文本而非报错）
- `extra_user_content_parts` 不可用时降级到 `system_prompt` 注入
- B 站库未安装时自动回退到网页搜索

## 数据存储位置

- 数据库：`<AstrBot 数据目录>/astrbot_plugin_active_learner/memory.db`
- 导出文件：`<AstrBot 数据目录>/astrbot_plugin_active_learner/memory_export_<scope>_<id>.json`

## License

本插件遵循 MIT License。

## 仓库

源码：https://github.com/qsbb/astrbot_plugin_active_learner
