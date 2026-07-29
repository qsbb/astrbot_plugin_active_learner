"""自主检索策略回归测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_active_learner.retrieval import RetrievalMixin


class _Plugin(RetrievalMixin):
    def __init__(self, weight: float, enabled: bool = True):
        self._learn_weight = weight
        self._enable_active_learn_hint = enabled


def _prompt(weight: float, enabled: bool = True, has_memory_hits: bool = False):
    return _Plugin(weight, enabled)._get_learn_prompt(
        has_memory_hits=has_memory_hits
    )


ACTIVE_WEIGHTS = (0.1, 0.3, 0.5, 0.7, 0.9, 1.0)


def test_hint_disabled_returns_none():
    assert _prompt(0.0) is None
    assert _prompt(-1.0) is None
    assert _prompt(0.7, enabled=False) is None


def test_every_weight_uses_semantic_decision_without_hard_keyword_rules():
    for weight in ACTIVE_WEIGHTS:
        text = _prompt(weight)
        assert "自主检索策略" in text
        assert "自行判断" in text
        assert "不是概率" in text
        assert "关键词触发" not in text
        assert "必须立即调用" not in text


def test_weight_is_continuous_instead_of_discrete_tiers():
    prompts = [_prompt(weight) for weight in ACTIVE_WEIGHTS]
    assert len(set(prompts)) == len(ACTIVE_WEIGHTS)
    assert "0.70" in _prompt(0.7)


def test_policy_preserves_normal_reply_speed_and_stops_tool_cascade():
    text = _prompt(0.7)
    assert "本次推理中" in text
    assert "一次回答最多启动一条搜索链" in text
    assert "不要再换用其他搜索工具连续重试" in text


def test_policy_reports_memory_state_to_semantic_judge():
    assert "没有可靠的本地候选记忆" in _prompt(0.7, has_memory_hits=False)
    assert "已有本地候选记忆" in _prompt(0.7, has_memory_hits=True)


def test_active_learn_patterns_removed_from_triggers():
    from astrbot_plugin_active_learner import triggers

    assert not hasattr(triggers, "ACTIVE_LEARN_PATTERNS")
    assert hasattr(triggers, "CHALLENGE_PATTERNS")
