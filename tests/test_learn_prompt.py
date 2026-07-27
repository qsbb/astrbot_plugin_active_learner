"""学习提示回归测试。

背景（真实问题）：用户问某游戏角色怎么配队，记忆库无命中，插件注入了学习提示，
但当时四档提示词都只写「如果对方在向你科普/教你新东西就去学」。模型判断
「用户没在科普」后跳过检索，直接用训练数据里的印象拼出一套阵容，内容是错的。

因此提示词必须同时覆盖两个方向：
1. 用户在科普/纠正 → 存入记忆库；
2. 用户在提问而本地无记忆 → 先检索再回答，不要凭印象编。

这里不导入 main.py（它依赖 astrbot 运行时），改为直接绑定未包装的函数，
只提供 _get_learn_prompt 实际用到的两个属性。
"""

import ast
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_MAIN_PY = Path(__file__).resolve().parents[1] / "main.py"


def _load_get_learn_prompt():
    """从 main.py 源码中摘出 _get_learn_prompt 并独立编译。"""
    tree = ast.parse(_MAIN_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_get_learn_prompt":
            src = textwrap.dedent(ast.get_source_segment(
                _MAIN_PY.read_text(encoding="utf-8"), node
            ))
            namespace: dict = {}
            exec(compile(src, "<_get_learn_prompt>", "exec"), namespace)
            return namespace["_get_learn_prompt"]
    raise AssertionError("main.py 中未找到 _get_learn_prompt")


_get_learn_prompt = _load_get_learn_prompt()


class _Plugin:
    def __init__(self, weight: float, enabled: bool = True):
        self._learn_weight = weight
        self._enable_active_learn_hint = enabled


def _prompt(weight: float, enabled: bool = True):
    return _get_learn_prompt(_Plugin(weight, enabled))


ALL_ACTIVE_WEIGHTS = (0.3, 0.5, 0.7, 0.9, 1.0)


def test_hint_disabled_returns_none():
    assert _prompt(0.0) is None
    assert _prompt(-1.0) is None
    assert _prompt(0.7, enabled=False) is None


def test_every_tier_still_covers_user_teaching_direction():
    """方向一不能因为补方向二而丢失。"""
    for weight in ALL_ACTIVE_WEIGHTS:
        text = _prompt(weight)
        assert text, f"weight={weight} 应返回提示"
        assert "search_and_learn" in text
        assert ("科普" in text or "介绍某个知识" in text), f"weight={weight} 缺少科普方向"


def test_every_tier_covers_question_direction():
    """方向二：用户提问且自己没把握时也要检索，这是本次修复的核心。"""
    for weight in ALL_ACTIVE_WEIGHTS:
        text = _prompt(weight)
        assert ("提问" in text or "用户提问" in text), f"weight={weight} 缺少提问方向"
        assert ("不确定" in text or "没有把握" in text or "没把握" in text), (
            f"weight={weight} 未覆盖「自己没把握」"
        )


def test_default_tier_forbids_fabricating_and_permission_asking():
    """默认档（0.7）是线上实际生效档位，约束要最完整。"""
    text = _prompt(0.7)
    assert "不要凭训练数据里的印象拼凑答案" in text
    assert "不要编造" in text
    # 不能反过来问用户「要不要我搜一下」
    assert "要不要我搜一下" in text
    # 易随版本变化的内容要特别警惕（本次出错的正是配队推荐）
    assert "搭配" in text and "推荐方案" in text


def test_aggressive_tier_lists_question_and_volatile_triggers():
    text = _prompt(1.0)
    assert "用户提问，而你对答案没有把握 → 调用" in text
    assert "搭配、推荐方案" in text
    assert "你自己完全确定且明确知道的内容 → 不调用" in text
    assert "不要编造" in text


def test_tiers_are_distinct_and_mention_missing_memory():
    """各档文案应有区分度，且都说明「本地记忆没有相关记录」这一前提。

    档位边界为 <0.4 / <0.7 / <1.0 / >=1.0 共四档，
    因此 0.7 与 0.9 同档、文案相同属预期行为。
    """
    for weight in ALL_ACTIVE_WEIGHTS:
        assert "本地记忆没有相关记录" in _prompt(weight)

    tier_samples = [_prompt(0.3), _prompt(0.5), _prompt(0.7), _prompt(1.0)]
    assert len(set(tier_samples)) == 4, "四个档位的文案应互不相同"
    assert _prompt(0.7) == _prompt(0.9), "0.7 与 0.9 属同一档"


def test_active_learn_patterns_removed_from_triggers():
    """学习提示与问法无关，遗留的死词表不应再出现，以免误导。"""
    from astrbot_plugin_active_learner import triggers

    assert not hasattr(triggers, "ACTIVE_LEARN_PATTERNS")
    assert hasattr(triggers, "CHALLENGE_PATTERNS")
