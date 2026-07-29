"""插件级共享常量。

单独成模块的原因：main.py 与 web_api.py / retrieval.py / learning.py 三个 mixin
需要引用同一批常量。若常量继续留在 main.py，mixin 反向 import main 会形成
mixin -> main -> mixin 的循环导入。本模块不导入包内任何其他模块，
因此可被任意模块安全引入。
"""

from __future__ import annotations

PLUGIN_NAME = "astrbot_plugin_active_learner"

# 插件版本。@register 装饰器与 metadata.yaml 必须保持一致，
# 这里作为唯一事实来源，避免版本号散落在多处后漏改。
PLUGIN_VERSION = "1.3.1"

_TOOL_NAMES = (
    "search_and_learn",
    "recall_memory",
    "verify_knowledge",
    "search_bilibili",
    "save_memory",
)

# 普通请求检索/注入预算：先全库 FTS，命中不足才限时调用 Embedding。
_FTS_SUFFICIENT_HITS = 3
# 纯 FTS 归一化后最佳命中通常约为 fts_weight；阈值仅排除明显弱命中，保守触发向量兜底。
_FTS_MIN_TOP_SCORE_RATIO = 0.5
_FTS_MIN_CONFIDENCE = 0.3
_EMBEDDING_TIMEOUT_SECONDS = 1.5
_RETRIEVAL_CONCURRENCY = 4
_EXTERNAL_SEARCH_DEADLINE_SECONDS = 10.0
_EXTERNAL_SEARCH_FIRST_RESULT_GRACE_SECONDS = 0.5
_MEMORY_INJECT_MAX_COUNT = 3
_MEMORY_INJECT_TOTAL_CHARS = 1800
_MEMORY_INJECT_ITEM_CHARS = 700

# ---------------------------------------------------------------------------
# 跨插件知识桥接契约（消费方：序 astrbot_plugin_identity_guardian）
# ---------------------------------------------------------------------------
# 语义化版本，仅描述 knowledge_contract() / recall() 这一对外接口：
# - major 递增：不兼容变更（删字段、改字段含义、改参数语义），消费方必须停用桥接；
# - minor 递增：向后兼容的新增（加可选参数、加返回字段），消费方可继续使用。
#
# 历史背景：0.x 时期序靠 duck-typing 探测 `recall` 方法，而知从未提供该方法，
# 桥接长期静默失效——既不报错也没有证据，排查成本极高。契约化后由消费方显式校验
# 版本并在失配时告警，失效原因可见。改动此处必须同步 CONVENTIONS.md 第 11 节。
KNOWLEDGE_CONTRACT_VERSION = "1.0"
KNOWLEDGE_CONTRACT_NAME = "active_learner.knowledge"

# 单次桥接检索返回的最大证据条数，防止消费方被灌入过量上下文。
_BRIDGE_RECALL_MAX_TOP_K = 10
