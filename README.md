# AstrBot 主动学习记忆插件

## 功能

### 🔍 主动学习
- 当用户提问遇到未知知识时，自动搜索网络
- 从多个来源（DuckDuckGo + 中文搜索）收集信息
- 用LLM总结并提取关键词
- 存入记忆库，带置信度评分

### 📝 记忆存储
- 每条记忆包含：主题、内容、关键词、来源、置信度、时间戳
- 基于关键词的智能检索，支持模糊匹配
- 自动容量管理（默认500条），淘汰低质量记忆
- 持久化存储，重启不丢失

### 🔄 上下文注入
- 每次LLM请求前自动检索相关记忆
- 将匹配到的记忆作为上下文注入prompt
- 支持质疑检测，当用户质疑时自动标记

### ✅ 质疑验证
- 用户质疑某条记忆时，自动触发多源验证
- 从4个角度搜索验证（事实核查、是否正确、官方信息、真假验证）
- 用LLM交叉验证，更新置信度
- 验证通过提升置信度，验证失败降低置信度

## 指令

| 指令 | 说明 | 示例 |
|------|------|------|
| `/memory` | 查看记忆库统计 | `/memory` |
| `/memory list [页码]` | 列出记忆条目 | `/memory list 2` |
| `/memory search <关键词>` | 搜索记忆 | `/memory search Python` |
| `/memory info <主题>` | 查看记忆详情 | `/memory info Python` |
| `/memory forget <主题>` | 删除记忆 | `/memory forget Python` |
| `/memory verify <主题>` | 手动触发验证 | `/memory verify Python` |
| `/memory export` | 导出记忆库 | `/memory export` |

## LLM工具（自动调用）

插件注册了3个LLM工具，模型会在需要时自动调用：

1. **search_and_learn** - 搜索并学习新知识
2. **recall_memory** - 从记忆库检索已有知识
3. **verify_knowledge** - 验证知识的准确性

## 工作流程

```
用户提问
  ↓
[on_llm_request钩子] → 搜索记忆库 → 命中？ → 注入上下文
  ↓ (未命中)
LLM处理 → 调用search_and_learn工具
  ↓
多源搜索 → LLM总结 → 提取关键词 → 存入记忆库
  ↓
返回结果给用户

用户质疑
  ↓
[检测质疑关键词] → 标记被质疑的记忆
  ↓
调用verify_knowledge工具
  ↓
多角度搜索验证 → LLM交叉验证 → 更新置信度
  ↓
返回验证结果
```

## 配置

在 AstrBot 管理面板 → 插件管理 → active_learner 中可调整：

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `max_entries` | 记忆库最大条数 | 500 |
| `min_confidence` | 最低置信度阈值 | 0.3 |
| `search_threshold` | 关键词匹配阈值 | 0.45 |

### 搜索源配置

插件会自动发现已安装的搜索能力，优先级如下：

**同级（谁先返回用谁）：**
- AstrBot 内置搜索（Tavily/BoCha/百度/Brave）
- `bilibili_ai_bot` 插件的搜索（Tavily/Perplexity/博查）

**兜底：**
- DuckDuckGo（免费，无需 API Key）

无需额外配置，插件会自动扫描已激活的插件，找到第一个可用的搜索源。

## 安装

1. 将 `astrbot_plugin_active_learner` 文件夹放入 AstrBot 的 `data/plugins/` 目录
2. 安装依赖：`pip install -r requirements.txt`
3. 重启 AstrBot
