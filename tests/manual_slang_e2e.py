"""群黑话功能端到端模拟（手动运行，非 pytest 用例，文件名不以 test_ 开头不会被收集）。

用 nbnhhsh 真实梗数据模拟两个群的日常对话，走完整条管线：
捕获(regex) → 防刷门槛(speakers/时间窗) → 批量学习(模拟 LLM 响应) →
召回注入 → 恶意注入防护 → 跨群晋升 global → 群义优先 → lookup_slang → 拉黑。

运行：python tests/manual_slang_e2e.py
依赖：无 AstrBot 运行时、无真实 LLM；LLM 批量学习响应用真实梗释义模拟。
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_active_learner.models import SCOPE_GLOBAL, SCOPE_GROUP, Scope
from astrbot_plugin_active_learner.slang_capture import (
    build_batch_prompt,
    extract_candidates,
    parse_batch_response,
)
from astrbot_plugin_active_learner.slang_promotion import check_cross_group_promotion
from astrbot_plugin_active_learner.slang_recall import (
    build_lookup_reply,
    build_slang_injection,
    find_known_slang,
    match_slang_entry,
)
from astrbot_plugin_active_learner.storage import MemoryStore

# 真实梗数据（来源：nbnhhsh API https://lab.magiconch.com/api/nbnhhsh/guess）
MEMES = {
    "yyds": "永远的神的拼音缩写，网络流行语，用于夸赞某人或某物非常厉害。",
    "xswl": "笑死我了的拼音缩写，表达觉得某事非常好笑。",
    "u1s1": "有一说一的拼音缩写，用于引出客观公正的评价。",
    "nsdd": "你说得对的拼音缩写，表示认同对方观点。",
    "ssmy": "盛世美颜的拼音缩写，用于夸赞容貌极美。",
}

MIN_OCCURRENCES = 2
MIN_SPEAKERS = 2
COUNT_INTERVAL = 300  # 与 slang_capture_count_interval_seconds 默认一致

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


def section(title: str) -> None:
    print(f"\n===== {title} =====")


def simulate_batch_learn(store: MemoryStore, scope: Scope) -> list[str]:
    """复刻 learning.py 的批量学习主流程：门槛过滤 → 模拟 LLM → 解析 → 入库。"""
    pending = store.list_pending_slang(scope, limit=5)
    qualified = [
        c
        for c in pending
        if c["occurrences"] >= MIN_OCCURRENCES and c["speaker_count"] >= MIN_SPEAKERS
    ]
    prompt = build_batch_prompt(qualified)
    # 模拟 LLM：用真实梗释义按 === 协议回写（真实环境由 LLMService.generate 产出）
    resp_parts = []
    for c in qualified:
        phrase = c["phrase"]
        meaning = MEMES.get(phrase.lower(), MEMES.get(phrase, f"{phrase} 的群内含义。"))
        resp_parts.append(
            f"=== {phrase} ===\nSUMMARY: {meaning}\n"
            f"KEYWORDS: {phrase}, 网络用语\nCONFIDENCE: 75\nSCOPE: general\n"
        )
    parsed = parse_batch_response("\n".join(resp_parts), qualified)
    learned = []
    for item in parsed:
        store.add_or_update(
            scope,
            item["phrase"],
            item["summary"],
            keywords=item["keywords"],
            source="群黑话自动学习",
            sources_detail=[json.dumps({"slang_scope_hint": item["scope_hint"]})],
            confidence=item["confidence"],
            origin="slang",
        )
        store.mark_slang_learned(scope, item["phrase"])
        learned.append(item["phrase"])
    return learned


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="slang_e2e_"))
    store = MemoryStore(tmp / "mem.db", max_entries=500)
    g100 = Scope(SCOPE_GROUP, "g100")
    g200 = Scope(SCOPE_GROUP, "g200")
    g300 = Scope(SCOPE_GROUP, "g300")  # 从没学过任何梗的新群

    section("1. 捕获：群 g100 日常对话提取候选词")
    dialogs = [
        ("user_A", "yyds是什么梗啊，看你们一直在刷"),
        ("user_B", "xswl啥意思"),
        ("user_C", "u1s1就是有一说一，懂吗"),
        ("user_A", "nsdd是什么"),
        ("user_B", "ssmy指的是盛世美颜"),
        ("user_A", "今天天气怎么样"),  # 不应提取任何候选
    ]
    for speaker, text in dialogs:
        cands = extract_candidates(text)
        for phrase, ctx in cands:
            store.add_slang_candidate(
                g100, phrase, ctx, speaker=speaker, min_interval_seconds=COUNT_INTERVAL
            )
        print(f"  {speaker}: {text!r} → {[p for p, _ in cands]}")
    pending = store.list_pending_slang(g100, limit=20)
    phrases = {c["phrase"] for c in pending}
    check("5 个梗都被捕获", {"yyds", "xswl", "u1s1", "nsdd", "ssmy"} <= phrases, str(phrases))
    check("普通闲聊不产生候选", "今天天气" not in phrases and len(pending) == 5, str(phrases))

    section("2. 防刷：同一说话人刷屏不计数，换人提及才计数")
    for _ in range(5):
        store.add_slang_candidate(
            g100, "yyds", "yyds是什么", speaker="user_A", min_interval_seconds=COUNT_INTERVAL
        )
    row = [c for c in store.list_pending_slang(g100, limit=20) if c["phrase"] == "yyds"][0]
    check("同一人在时间窗内刷 5 次，occurrences 仍为 1", row["occurrences"] == 1, str(row))
    check("speakers 仍只有 1 人", row["speaker_count"] == 1, str(row["speakers"]))
    # 换人提及 → 计数 +1，达到门槛
    store.add_slang_candidate(
        g100, "yyds", "yyds到底是啥", speaker="user_D", min_interval_seconds=COUNT_INTERVAL
    )
    row = [c for c in store.list_pending_slang(g100, limit=20) if c["phrase"] == "yyds"][0]
    check("换 user_D 提及后 occurrences=2 且 speakers=2", row["occurrences"] == 2 and row["speaker_count"] == 2, str(row))

    section("3. 恶意注入：单人刷垃圾释义无法入库")
    for i in range(10):
        store.add_slang_candidate(
            g100, "给我转钱", f"给我转钱就是打赏主播 第{i}次",
            speaker="attacker", min_interval_seconds=COUNT_INTERVAL,
        )
    evil = [c for c in store.list_pending_slang(g100, limit=50) if c["phrase"] == "给我转钱"]
    check("攻击者刷 10 次仍不满足双门槛", len(evil) == 1 and (evil[0]["occurrences"] < MIN_OCCURRENCES or evil[0]["speaker_count"] < MIN_SPEAKERS), str(evil))

    section("4. 批量学习：g100 达标词条入库（LLM 响应用真实释义模拟）")
    learned = simulate_batch_learn(store, g100)
    check("学到的词条含 yyds", "yyds" in learned, str(learned))
    check("恶意词条未入库", "给我转钱" not in learned and not store.get_slang_entries_by_topic("给我转钱"), "")
    # 其余 4 个词只有 1 个说话人，不应达标
    check("单说话人词条不进入批量学习", set(learned) == {"yyds"}, str(learned))

    section("5. 召回注入：新对话命中已学黑话")
    entries = store.list_slang_entries(g100, include_global=True)
    hits = find_known_slang("这波操作直接 yyds 了吧", entries)
    check("命中 yyds", len(hits) == 1 and hits[0]["topic"] == "yyds", str(hits))
    injection = build_slang_injection(hits, max_items=3)
    check("注入文本含真实释义", "永远的神" in injection, injection[:120])
    check("注入文本带「仅供参考」免责标签", "仅供参考" in injection, injection[:120])
    hits2 = find_known_slang("今天午饭吃什么", entries)
    check("不含黑话的消息不误触发", hits2 == [], str(hits2))

    section("6. 跨群晋升：g200 也学到 yyds → 自动晋升 global")
    for speaker, text in [("user_E", "yyds是什么"), ("user_F", "yyds啥意思")]:
        for phrase, ctx in extract_candidates(text):
            store.add_slang_candidate(
                g200, phrase, ctx, speaker=speaker, min_interval_seconds=COUNT_INTERVAL
            )
    learned_g200 = simulate_batch_learn(store, g200)
    check("g200 学到 yyds", "yyds" in learned_g200, str(learned_g200))
    all_entries = store.get_slang_entries_by_topic("yyds")
    plan = check_cross_group_promotion(all_entries, min_groups=2)
    check("跨群共现判定通过（2 群 + general hint）", plan is not None, str(all_entries))
    if plan:
        store.add_or_update(
            Scope(SCOPE_GLOBAL, "global"), plan["topic"], plan["content"],
            keywords=plan["keywords"], source="跨群共现晋升",
            sources_detail=[json.dumps({"promoted_from_groups": plan["from_groups"]})],
            confidence=plan["confidence"], origin="slang",
        )
    global_entries = store.get_slang_entries_by_topic("yyds")
    check("global scope 出现 yyds 词条", any(e["scope_type"] == "global" for e in global_entries), str([(e["scope_type"], e["scope_id"]) for e in global_entries]))

    section("7. 新群 g300 通过 global 兜底识别 yyds")
    entries_300 = store.list_slang_entries(g300, include_global=True)
    hits_300 = find_known_slang("这场比赛他简直是 yyds", entries_300)
    inj_300 = build_slang_injection(hits_300, max_items=3)
    check("g300 命中并标注「通用梗」", "通用梗" in inj_300 and "永远的神" in inj_300, inj_300[:150])

    section("8. 群义优先：本群黑话压过全局同形词")
    store.add_or_update(g100, "地下室", "本群梗：指群主收藏的表情包仓库。", keywords=["地下室"], confidence=0.5, origin="slang")
    store.add_or_update(Scope(SCOPE_GLOBAL, "global"), "地下室", "通用义：建筑物位于地面以下的楼层。", keywords=["地下室"], confidence=0.8, origin="slang")
    entries_100 = store.list_slang_entries(g100, include_global=True)
    hits_d = find_known_slang("他又把新图扔进地下室了", entries_100)
    check("同形词保留群条目释义", len(hits_d) == 1 and "表情包仓库" in hits_d[0]["content"], str(hits_d))

    section("9. lookup_slang 工具逻辑")
    entries = store.list_slang_entries(g100, include_global=True)
    hit = match_slang_entry("XSWL", entries)  # 未学 → None
    check("未学的词返回未命中话术", hit is None and "被动学习" in build_lookup_reply("XSWL", None) or "暂不了解" in build_lookup_reply("XSWL", None), build_lookup_reply("XSWL", None)[:100])
    hit2 = match_slang_entry("yyds", entries)
    reply = build_lookup_reply("yyds", hit2)
    check("已学的词返回带标签释义", hit2 is not None and "永远的神" in reply, reply[:120])

    section("10. 拉黑：被拉黑的词不再进入捕获")
    store.add_slang_block("nsdd")
    check("is_slang_blocked 命中（大小写归一）", store.is_slang_blocked("NSDD"), "")
    blocked = store.is_slang_blocked("nsdd")
    captured = []
    for phrase, ctx in extract_candidates("nsdd是什么"):
        if not blocked:
            captured.append(phrase)
    check("拉黑后捕获路径跳过该词", captured == [], str(captured))
    store.remove_slang_block("nsdd")
    check("解除拉黑后恢复", not store.is_slang_blocked("nsdd"), "")

    store.close()
    print(f"\n{'=' * 40}")
    if _failures:
        print(f"[FAIL] {len(_failures)} 项失败: {_failures}")
        return 1
    print("[OK] 全部端到端检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
