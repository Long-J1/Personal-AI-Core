"""待验证候选卡片 + 处理摘要：展示层升级。

这个任务的核心不是"卡片长什么样"，而是两件事：

1. **一个内部字段都不许露出去。** 置信度、scope、source_type、假设编号
   一旦端到用户面前，产品就变成数据库查询工具。
2. **没有真实来源时不许编。** 卡片宁可空着，也不拿"可能""大概"凑数。

所以测试的重点是泄漏和诚实，不是渲染。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone as dtz

import pytest

from core.candidate_view import (
    STATE_TEXT,
    assert_no_internal_fields,
    build_card,
    build_cards,
    build_view,
)
from core.understanding_view import FORBIDDEN_IN_OUTPUT, human_date

NOW = "2026-10-02T12:00:00"
SH = dtz(timedelta(hours=8))


class FakeHypo:
    """最小 Hypothesis 替身。测试不该依赖真 store 的形状。"""

    def __init__(self, id="h1", statement="用户近期多次进行「写作业」相关活动，"
                                           "可能正处在相关事项的持续期。",
                 scope="activity:写作业", status="tentative",
                 created_at_iso="2026-10-01T20:00:00"):
        self.id = id
        self.statement = statement
        self.scope = scope
        self.status = status
        self.created_at_iso = created_at_iso


def detail(evidence=(), id="h1", scope="activity:写作业"):
    return {"id": id, "scope": scope, "evidence": list(evidence)}


def ev(text, when="2026-10-01T20:00:00", polarity="support"):
    return {"excerpt": text, "created_at_iso": when, "polarity": polarity}


# ============================================================ 1 人话，不是字段

def test_card_speaks_human_not_fields():
    card = build_card(FakeHypo(), detail(), now=NOW)
    blob = json.dumps(card, ensure_ascii=False)
    for w in ("confidence", "scope", "source_type", "source_id",
              "hypothesis_id", "activity:", "0.7", "0.72"):
        assert w not in blob, f"卡片泄漏内部字段 {w}：{blob}"
    assert assert_no_internal_fields([card]) == []


def test_topic_and_about_are_human():
    card = build_card(FakeHypo(), detail(), now=NOW)
    assert card["topic"] == "最近在做的事 · 写作业"
    assert card["about"] == "你最近经常在做「写作业」"
    assert "用户" not in card["about"], "要说「你」，不是「用户」"


def test_when_is_relative_human_word():
    card = build_card(FakeHypo(), detail(), now=NOW)
    assert card["when"] == "昨天"
    assert card["when"] != "2026-10-01"


def test_forbidden_list_covers_the_task_requirements():
    """禁用清单必须覆盖任务书点名的那些。"""
    for w in ("confidence", "source_type", "source_id", "hypothesis_id",
              "scope="):
        assert w in FORBIDDEN_IN_OUTPUT, f"禁用清单漏了 {w}"


# ============================================================ 2 不编

def test_no_evidence_says_so_instead_of_inventing():
    card = build_card(FakeHypo(), detail(), now=NOW)
    assert card["quotes"] == []
    assert card["has_evidence"] is False
    assert card["why_tail"], "即使没有依据，收尾说明也保留（它是产品语言）"


def test_silent_evidence_is_dropped_not_shown_empty():
    """没有原话的空壳证据不进产品面——展示"「」"比不展示更糟。"""
    card = build_card(FakeHypo(), detail([ev(""), ev("   ")]), now=NOW)
    assert card["quotes"] == []


def test_real_quote_survives_with_time():
    card = build_card(FakeHypo(),
                      detail([ev("我最近总是十点以后才开始写")]), now=NOW)
    assert len(card["quotes"]) == 1
    assert card["quotes"][0]["text"] == "我最近总是十点以后才开始写"
    assert card["quotes"][0]["when"] == "昨天"
    assert card["has_evidence"] is True


def test_internal_only_candidates_do_not_appear_at_all():
    """quota_overflow / background 这类内部候选，
    与其显示成看不懂的话，不如根本不出现。"""
    assert build_card(FakeHypo(scope="quota_overflow:dynamic"), detail(),
                      now=NOW) == {}
    assert build_card(FakeHypo(scope="background:behavior_change:学习"), detail(),
                      now=NOW) == {}


# ============================================================ 3 状态分档

@pytest.mark.parametrize("status,bucket", [
    ("tentative", "pending"),
    ("accepted", "watching"),
    ("confirmed", "settled"),
    ("rejected", "settled"),
    ("expired", "settled"),
    ("deleted", "settled"),
])
def test_status_maps_to_bucket(status, bucket):
    card = build_card(FakeHypo(status=status), detail(), now=NOW)
    assert card["bucket"] == bucket
    assert card["state_text"] == STATE_TEXT[bucket]


def test_unknown_status_does_not_promote_to_main_list():
    """没见过的状态宁可进历史，也不该占主列表。"""
    card = build_card(FakeHypo(status="some_new_state"), detail(), now=NOW)
    assert card["bucket"] == "settled"


def test_only_pending_is_actionable():
    assert build_card(FakeHypo(status="tentative"), detail(), now=NOW)["can_act"]
    for s in ("accepted", "confirmed", "rejected"):
        assert not build_card(FakeHypo(status=s), detail(),
                              now=NOW)["can_act"]


def test_wording_does_not_overstate_the_meaning_of_ok():
    """「对」是值得继续观察，不是"这就是事实"。

    用**正则**而不是精确字串：mutation J 把文案改成
    "我已确认这是事实，并设为你的长期记忆"——精确字串匹配不到它，
    于是这个测试对 J 变异完全无效（是我自己的实测发现）。
    """
    view = build_view([FakeHypo()], {"h1": detail()}, now=NOW)
    blob = json.dumps(view, ensure_ascii=False)
    heavy = [
        r"确认.{0,4}事实",          # 确认事实 / 已确认这是事实
        r"设为.{0,6}记忆",          # 设为记忆 / 设为你的长期记忆
        r"确认理解", r"确定.{0,3}事实",
        r"已经.{0,3}是事实", r"记为.{0,4}记忆",
        r"(?<!未)确认了",            # 确认了…
    ]
    for pat in heavy:
        m = re.search(pat, blob)
        assert not m, f"文案把语义说重了（{pat}）：{blob}"


# ============================================================ 4 列表组织

def test_main_list_only_holds_what_needs_attention():
    rows = [
        FakeHypo(id="a", status="tentative"),
        FakeHypo(id="b", status="accepted"),
        FakeHypo(id="c", status="confirmed"),
        FakeHypo(id="d", status="rejected"),
    ]
    dets = {r.id: detail(id=r.id) for r in rows}
    view = build_view(rows, dets, now=NOW)
    assert [c["id"] for c in view["pending"]] == ["a"]
    assert [c["id"] for c in view["watching"]] == ["b"]
    assert sorted(c["id"] for c in view["settled"]) == ["c", "d"]
    assert view["counts"] == {"pending": 1, "watching": 1, "settled": 2}


def test_each_candidate_is_its_own_unit_not_one_text_stream():
    """多候选必须是多个独立对象，不能拼成一条文字流。"""
    rows = [FakeHypo(id=f"h{i}", statement=f"用户第{i}次这样做")
            for i in range(5)]
    dets = {r.id: detail(id=r.id) for r in rows}
    cards = build_cards(rows, dets, now=NOW)
    assert len(cards) == 5
    assert len({c["id"] for c in cards}) == 5, "每条要有自己的身份"
    for c in cards:
        assert c["topic"] and c["about"], "每条都要有自己的标题和正文"


def test_empty_input_is_safe():
    view = build_view([], {}, now=NOW)
    assert view["pending"] == [] and view["counts"]["pending"] == 0


def test_detail_lookup_uses_the_right_id():
    """证据按 id 对上，别把 A 的证据贴到 B 身上。"""
    a = FakeHypo(id="a")
    b = FakeHypo(id="b")
    cards = build_cards([a, b], {
        "a": detail(id="a", evidence=[ev("这是A的原话")]),
        "b": detail(id="b", evidence=[ev("这是B的原话")]),
    }, now=NOW)
    by = {c["id"]: c for c in cards}
    assert by["a"]["quotes"][0]["text"] == "这是A的原话"
    assert by["b"]["quotes"][0]["text"] == "这是B的原话"


# ============================================================ 4b 真实形状
#
# 这一节全部用**真 store 返回的对象**。
# 上一版我用 FakeHypo(created_at_iso=...) 自造数据，全绿；
# 真跑一次才发现时间栏全是空的——真对象的字段叫 created_at，
# 不是 created_at_iso。单测构造的形状和真实形状不同时，单测是假的。

def test_real_hypothesis_object_gives_a_when():
    """真 Hypothesis 对象的时间字段叫 created_at。

    只认 created_at_iso 的话，这一栏永远空着，而单测照样绿——
    因为测试喂的是自己造的 dict。
    """
    from memory.hypothesis_store import HypothesisStore
    from memory.context_store import ContextStore
    import tempfile

    db = tempfile.mkdtemp() + "/t.db"
    ContextStore(db)
    h = HypothesisStore(db)
    hyp, _ = h.create(statement="用户近期多次在深夜写作业", scope="activity:写作业")
    assert not hasattr(hyp, "created_at_iso"), \
        "真实对象没有 created_at_iso（这条是前提，别改 store 就来迁就测试）"

    card = build_card(hyp, h.get_detail(hyp.id))
    assert card["when"], "真对象也要能拿到时间（不再是一片空白）"


def test_real_detail_shape_gives_a_when():
    import tempfile
    from memory.context_store import ContextStore
    from memory.hypothesis_store import HypothesisStore

    db = tempfile.mkdtemp() + "/t.db"
    ContextStore(db)
    h = HypothesisStore(db)
    hyp, _ = h.create(statement="用户近期多次在深夜写作业", scope="activity:写作业")
    d = h.get_detail(hyp.id)
    assert "created_at_iso" not in d, "get_detail 也不给 created_at_iso"
    card = build_card(hyp, d)
    assert card["when"], "get_detail 的形状也要能拿到时间"


def test_real_card_has_no_internal_fields():
    from memory.hypothesis_store import HypothesisStore
    from memory.context_store import ContextStore
    import tempfile

    db = tempfile.mkdtemp() + "/t.db"
    ContextStore(db)
    h = HypothesisStore(db)
    hyp, _ = h.create(statement="用户近期多次在深夜写作业",
                      scope="activity:写作业")
    card = build_card(hyp, h.get_detail(hyp.id))
    blob = json.dumps(card, ensure_ascii=False)
    assert assert_no_internal_fields([card]) == []
    for w in ("confidence", "scope", "source_type", "activity:"):
        assert w not in blob


def test_why_tail_absent_for_settled():
    """已确认的候选不该再写「暂时看作可能的变化」——语义说反了。"""
    card = build_card(FakeHypo(status="confirmed"), detail(), now=NOW)
    assert card["why_tail"] == ""
    for st in ("tentative", "accepted"):
        assert build_card(FakeHypo(status=st), detail(),
                          now=NOW)["why_tail"]


# ============================================================ 5 泄漏自检本身有效

def test_self_check_actually_catches_a_leak():
    """自检要是摆设，整条安全链就是空的。"""
    bad = [{"note": "confidence=0.72"}]
    leaks = assert_no_internal_fields(bad)
    assert leaks, "自检没抓到明写的置信度"
    bad2 = [{"note": "范围：interaction:提示"}]
    assert assert_no_internal_fields(bad2)
    bad3 = [{"note": "来源 correction"}]
    assert assert_no_internal_fields(bad3)


def test_self_check_passes_clean_payload():
    assert assert_no_internal_fields([{"topic": "学习", "about": "你最近经常熬夜"}]) == []


# ============================================================ 6 真链路

def test_real_store_produces_clean_cards():
    """真 store → 真投影，端到端不许漏。"""
    from memory.context_store import ContextStore
    from memory.hypothesis_store import HypothesisStore
    import tempfile

    db = tempfile.mkdtemp() + "/t.db"
    ContextStore(db)
    h = HypothesisStore(db)
    hyp, _ = h.create(
        statement="用户在题目类场景下更希望先得到提示而不是直接答案",
        scope="interaction:提示")
    entry, _ = ContextStore(db).add_entry(
        kind="interaction", content="别直接给我答案，先给提示",
        source="user_direct", source_grade="user_direct")
    h.add_evidence(hyp.id, "support", "context", entry.id,
                   excerpt="别直接给我答案，先给提示")
    d = h.get_detail(hyp.id)
    assert d["evidence"], f"证据没落库，测试就是假的：{d}"

    cards = build_cards([hyp], {hyp.id: d})
    assert len(cards) == 1
    blob = json.dumps(cards[0], ensure_ascii=False)
    assert assert_no_internal_fields(cards) == []
    for w in ("confidence", "scope=", "source_type", "interaction:"):
        assert w not in blob
    assert cards[0]["quotes"][0]["text"] == "别直接给我答案，先给提示"
    assert cards[0]["when"], "真链路里时间也必须有"


def test_api_endpoint_serves_cards(tmp_path):
    """端点真的能起来，且响应里没有内部字段。"""
    from fastapi.testclient import TestClient
    from interfaces.webapp import create_app
    from memory.context_store import ContextStore
    from memory.hypothesis_store import HypothesisStore
    from memory.store import MemoryStore

    db = tmp_path / "t.db"
    MemoryStore(db); ContextStore(db)
    h = HypothesisStore(db)
    h.create(statement="用户近期多次在晚上学习", scope="activity:学习")
    app = create_app(MemoryStore(db), hypothesis_store=h)
    r = TestClient(app).get("/api/candidates")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"pending", "watching", "settled", "counts"}
    blob = json.dumps(body, ensure_ascii=False)
    for w in ("confidence", "scope=", "source_type", "hypothesis_id"):
        assert w not in blob


def test_api_survives_when_hypothesis_store_is_broken(tmp_path):
    """投影坏掉不许把页面带崩——返回空面板即可。"""
    from fastapi.testclient import TestClient
    from interfaces.webapp import create_app
    from memory.store import MemoryStore

    db = tmp_path / "t.db"
    store = MemoryStore(db)

    class Boom:
        def list_hypotheses(self, **kw):
            raise RuntimeError("store exploded")
        def get_detail(self, i):
            raise RuntimeError("store exploded")

    app = create_app(store, hypothesis_store=Boom())
    r = TestClient(app).get("/api/candidates")
    assert r.status_code == 200
    assert r.json()["pending"] == []


# ============================================================ 7 UI 源码级守卫

def _html() -> str:
    import io
    import os
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "interfaces", "static", "index.html")
    return io.open(p, encoding="utf-8").read()


def test_ui_never_renders_internal_fields():
    """扫 index.html：模板里不许出现内部字段的渲染。"""
    bad = [
        (r"\$\{[^}]*\.confidence", "渲染置信度"),
        (r"\$\{[^}]*\.scope\b", "渲染 scope"),
        (r"\$\{[^}]*source_type", "渲染 source_type"),
        (r"\$\{[^}]*hypothesis_id", "渲染 hypothesis_id"),
        (r"\$\{[^}]*support_count", "渲染支持计数"),
        (r"\$\{[^}]*contradict_count", "渲染反证计数"),
    ]
    hits = []
    for pat, why in bad:
        for m in re.finditer(pat, _html()):
            line = _html()[:m.start()].count("\n") + 1
            hits.append(f"L{line} {why}")
    assert not hits, f"UI 仍在渲染内部字段：{hits}"


def test_ui_candidate_cards_have_no_raw_status_labels():
    """卡片的状态文案必须来自后端，不能在 JS 里拼内部状态名。"""
    html = _html()
    # 曾经的 HYPO_STATUS 映射（tentative: '待验证'…）已删除
    assert "HYPO_STATUS" not in html
    assert "HYPO_POL" not in html


def test_ui_trace_does_not_invent_thinking():
    """处理摘要不许硬塞"已分析你的问题"这种假步骤。"""
    html = _html()
    assert "'已分析你的问题'" not in html, \
        "那是编出来的思考，必须由真实数据决定"
    assert "traceSteps" in html
    assert "judgment" in html, "摘要应来自本轮真实 Judgment"


def test_ui_cards_are_a_grid_not_one_text_stream():
    """卡片化：必须有独立卡片的容器和网格。"""
    html = _html()
    assert 'class="ccards"' in html
    assert ".ccards" in html and "grid-template-columns" in html
    assert 'class="ccard' in html
    # 旧的纵向文字流容器不再用于待验证
    seg = html[html.index("function candCard"):html.index("function candWire")]
    assert "memlist" not in seg


def test_ui_trace_is_collapsed_by_default():
    """默认折叠：用原生 <details>，不带 open 属性。"""
    html = _html()
    i = html.index("function buildTrace")
    seg = html[i:i + 900]
    assert "createElement('details')" in seg
    assert ".open" not in seg, "不许默认展开"
    assert "createElement('summary')" in seg


def test_ui_trace_absent_when_nothing_reliable():
    """没有可靠来源就整个不显示，不返回空壳。"""
    html = _html()
    i = html.index("function buildTrace")
    seg = html[i:i + 1400]
    assert "if (!steps.length) return null;" in seg


def test_ui_narrow_screen_forces_single_column():
    html = _html()
    assert "@media (max-width: 560px)" in html
    i = html.index("@media (max-width: 560px)")
    seg = html[i:i + 260]
    assert "grid-template-columns: 1fr" in seg


def test_ui_no_stale_memlist_logic_left_in_wrapper():
    """不许有代码再用旧的 .mem 计数驱动待验证面板。

    这条是实测抓出来的真 bug：文件末尾有个"生命感"包装器，
    卡片化之后它还在数 .mem 并覆盖徽章，导致徽章恒为 0 而主列表仍有内容。
    单测抓不到这类"旧逻辑残留"，只能扫源码。
    """
    html = _html()
    stale = []
    for i, ln in enumerate(html.split("\n"), 1):
        if ".mem'" in ln or ".mem\"" in ln or "querySelectorAll('.mem" in ln:
            if "hypoList" in ln or "hypoBadge" in ln:
                stale.append(f"L{i}: {ln.strip()[:70]}")
    assert not stale, f"待验证面板还残留旧 .mem 逻辑：{stale}"


def test_ui_badge_is_written_by_candidates_loader():
    html = _html()
    seg = html[html.index("async function loadHypos"):]
    seg = seg[:seg.index("function candCard")]
    assert "hypoBadge" in seg, "徽章应由 loadHypos 自己写"
    assert "counts" in seg and "pending" in seg, \
        "徽章应来自后端 counts.pending，不该在前端数 DOM 节点"