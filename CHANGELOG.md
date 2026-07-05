# 更新日志

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

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
