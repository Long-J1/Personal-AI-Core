"""第一次被理解：产品语言投影 + API（V0.5.7）。

这个功能的全部价值在**翻译**——把内部模型（confidence / source_type /
hypothesis_id / scope 前缀）翻译成人话。所以最要紧的测试不是"能不能
返回数据"，而是**一个内部字段都不许漏出去**。

另外锁死两条：
- quota_overflow 是 V0.5.3 的"不可验证"边界，展示层也不许露出；
- 没有用户原话的东西不许出现在时间线上。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from core.understanding_view import (
    FORBIDDEN_IN_OUTPUT,
    assert_no_internal_fields,
    build_note,
    build_timeline,
    build_understanding,
    human_date,
    onboarding_state,
    plain_statement,
    topic_name,
)
from interfaces.webapp import create_app
from memory.context_store import ContextStore
from memory.hypothesis_store import HypothesisStore
from memory.store import MemoryStore
from models.mock_adapter import MockChatModel

NOW = "2026-10-02T12:00:00"


@pytest.fixture
def world(tmp_path):
    db = tmp_path / "t.db"
    return {"db": db, "ctx": ContextStore(db), "hyp": HypothesisStore(db),
            "store": MemoryStore(db)}


def hypo(scope="activity:写作业", statement=None, status="tentative",
         created="2026-10-01T20:00:00", evidence=(), history=()):
    return {
        "scope": scope,
        "statement": statement or (
            "用户近期多次进行「写作业」相关活动，可能正处在相关事项的持续期。"),
        "status": status,
        "created_at_iso": created,
        "evidence": list(evidence),
        "history": list(history),
    }


def hypo_alt_time(created="2026-10-01T20:00:00", evidence=()):
    """同样一条假设，但时间字段叫 created_at（get_detail 的真实形状）。"""
    return {
        "scope": "activity:写作业",
        "statement": "用户近期多次进行「写作业」相关活动，持续期。",
        "status": "tentative",
        "created_at": created,
        "evidence": list(evidence),
        "history": [],
    }


# ============================================================ 平白话翻译

def test_statement_is_translated_to_second_person():
    assert plain_statement(
        "用户近期多次进行「写作业」相关活动，可能正处在相关事项的持续期。"
    ) == "你最近经常在做「写作业」"

    assert plain_statement(
        "用户在「希望你」方面多次表达过相处偏好，可能希望 AI 保持一致。"
    ) == "你在「希望你」上希望我保持一致"

    assert plain_statement("用户喜欢喝红茶") == "你喜欢喝红茶"


def test_plain_statement_never_leaves_user_as_subject():
    """兜底规则：认不出的句式也不能把"用户"两个字甩给用户看。"""
    for s in ("用户偏好简洁", "用户可能会拒绝冗长的回答", "用户"):
        assert not plain_statement(s).startswith("用户")


def test_plain_statement_is_deterministic():
    """展示层不许调模型——一次抖动就会改写用户看到的历史。"""
    s = "用户近期多次进行「学习」相关活动。"
    assert plain_statement(s) == plain_statement(s) == "你最近经常在做「学习」"


# ============================================================ 主题名

@pytest.mark.parametrize("scope,expected", [
    ("activity:写作业", "最近在做的事 · 写作业"),
    ("interaction:希望你", "相处方式 · 希望你"),
    ("inferred:dynamic", "最近的情况"),
    ("inferred:interaction", "相处方式"),
    ("inferred:profile", "关于你"),
    ("profile", "关于你"),
    ("", "关于你"),
])
def test_topic_name_is_product_language(scope, expected):
    assert topic_name(scope) == expected


def test_quota_overflow_never_reaches_the_user():
    """V0.5.3 的安全边界：不可验证的材料不露出。"""
    assert topic_name("quota_overflow:dynamic") == ""
    assert build_understanding(hypo(scope="quota_overflow:dynamic")) == {}
    tl = build_timeline([hypo(scope="quota_overflow:dynamic")], [])
    assert tl["items"] == [] and tl["count"] == 0


# ============================================================ 时间

def test_human_date_is_relative_and_readable():
    assert human_date("2026-10-02T09:00:00", NOW) == "今天"
    assert human_date("2026-10-01T09:00:00", NOW) == "昨天"
    assert human_date("2026-09-29T09:00:00", NOW) == "3 天前"
    assert human_date("2026-09-01T09:00:00", NOW) == "9 月 1 日"


def test_human_date_never_returns_iso_string():
    for d in ("2026-10-02T09:00:00", "2026-01-05T00:00:00"):
        out = human_date(d, NOW)
        assert "T" not in out and "-" not in out


def test_bad_date_does_not_crash():
    assert human_date("不是日期", NOW) == ""
    assert human_date(None, NOW) == ""


# ============================================================ 状态

def test_state_words_are_plain():
    row = build_understanding(hypo(status="tentative"))
    assert row["state"] == "learning" and row["state_text"] == "还在摸索"
    row = build_understanding(hypo(status="confirmed"))
    assert row["state"] == "stable" and row["state_text"] == "稳定理解"
    row = build_understanding(hypo(status="rejected"))
    assert row["state"] == "dropped" and row["state_text"] == "不再使用"


def test_change_is_surfaced_in_plain_words():
    """用户改主意了——时间线要能说清"为什么调整了"。"""
    row = build_understanding(hypo(
        evidence=[{"polarity": "support", "excerpt": "晚上十点后学习效率最高",
                   "created_at_iso": "2026-10-01T20:00:00"}],
        history=[{"action": "change", "iso": "2026-10-02T09:00:00",
                  "detail": json.dumps({"excerpt": "最近改成早上学习"},
                                       ensure_ascii=False)}],
    ))
    assert row["changed"] is True
    assert row["state"] == "changed"
    assert "最近改成早上学习" in row["change_note"]
    assert "今天" in row["change_note"]          # 用人话，不给 ISO


def test_no_change_means_no_change_note():
    row = build_understanding(hypo(
        evidence=[{"polarity": "support", "excerpt": "我喜欢猫",
                   "created_at_iso": "2026-10-01T20:00:00"}]))
    assert row["changed"] is False and row["change_note"] == ""


# ============================================================ 内部字段绝不泄漏

def test_no_internal_field_leaks_into_timeline():
    payload = build_timeline([
        hypo(evidence=[{"polarity": "support", "excerpt": "晚上十点后学习效率最高",
                        "created_at_iso": "2026-10-01T20:00:00",
                        "source_type": "correction"}],
             history=[{"action": "change", "iso": "2026-10-02T09:00:00",
                       "detail": json.dumps({"excerpt": "改成早上了"},
                                            ensure_ascii=False)}]),
    ], [])
    leaked = assert_no_internal_fields(payload)
    assert leaked == [], f"内部字段泄漏到用户可见层：{leaked}"


def test_assert_helper_actually_detects_leaks():
    """自检工具本身要有效，否则它只是一句安慰。"""
    assert assert_no_internal_fields({"a": 1}) == []
    assert "confidence" in assert_no_internal_fields({"c": "confidence"})
    assert "quota_overflow" in assert_no_internal_fields(
        {"topic": "quota_overflow:x"})


def test_timeline_keys_are_all_product_facing():
    """时间线每一项只应该有这几个键——多一个都可能是内部字段。"""
    tl = build_timeline([hypo(
        evidence=[{"polarity": "support", "excerpt": "晚上十点后学习效率最高",
                   "created_at_iso": "2026-10-01T20:00:00"}])], [])
    allowed = {"kind", "topic", "about", "state", "state_text",
               "formed_on", "changed", "change_note", "quotes"}
    for item in tl["items"]:
        assert set(item) == allowed, f"意外的键：{set(item) - allowed}"
        assert set(item["quotes"][0]) == {"when", "text", "kind"}


def test_change_note_never_falls_back_to_raw_log_detail():
    """回归：log.detail 里没有可读原话时，绝不能把原始 JSON 塞给用户。

    add_evidence 写的 log.detail 只有 source_type/source_id。早期版本
    在取不到 excerpt 时直接回退到 detail 原文，于是
    「你在昨天说过 {"source_type": "correction", "source_id": "760c5e8a…"」
    被原样显示出来——这条是被 assert_no_internal_fields 当场抓到的。
    """
    row = build_understanding(hypo(
        evidence=[{"polarity": "contradict", "excerpt": "我改成早上学习了。",
                   "created_at_iso": "2026-10-02T09:00:00"}],
        history=[{"action": "contradict", "iso": "2026-10-02T09:00:00",
                  "detail": json.dumps(
                      {"source_type": "correction",
                       "source_id": "760c5e8a815a5d479b5d75c18f6eafd7",
                       "reason": "自然变化信号：用户表示自己变了"},
                      ensure_ascii=False)}],
    ))
    assert "source_type" not in row["change_note"]
    assert "760c5e8a" not in row["change_note"]
    assert "我改成早上学习了。" in row["change_note"]
    assert assert_no_internal_fields(row) == []


def test_change_note_without_any_quotes_still_reads_human():
    """连反证都没有原话时，也只能说人话，不能空着也不能漏字段。"""
    row = build_understanding(hypo(
        evidence=[{"polarity": "contradict", "excerpt": "",
                   "created_at_iso": "2026-10-02T09:00:00"}],
        history=[{"action": "change", "iso": "2026-10-02T09:00:00",
                  "detail": json.dumps({"source_type": "correction"},
                                       ensure_ascii=False)}],
    ))
    assert "source_type" not in row["change_note"]
    assert "调整了" in row["change_note"]
    assert assert_no_internal_fields(row) == []


def test_quotes_only_come_from_real_user_text():
    """时间线上的引语必须是真原话，不许被内部字段顶替。"""
    row = build_understanding(hypo(evidence=[
        {"polarity": "support", "excerpt": "", "created_at_iso": "2026-10-01"},
        {"polarity": "support", "excerpt": "晚上十点后学习效率最高",
         "created_at_iso": "2026-10-01"},
    ]))
    assert [q["text"] for q in row["quotes"]] == ["晚上十点后学习效率最高"]


def test_understanding_reads_time_from_either_field_name():
    """回归：hypothesis 与 context 的时间字段名不同（created_at vs
    created_at_iso）。只认一种就会出现"形成时间空白"。"""
    a = build_understanding(hypo(created="2026-10-01T20:00:00"))
    b = build_understanding(hypo_alt_time(created="2026-10-01T20:00:00"))
    assert a["formed_on"] == "昨天" and b["formed_on"] == "昨天"


def test_quote_time_reads_either_field_name():
    a = build_understanding(hypo(evidence=[
        {"polarity": "support", "excerpt": "我喜欢猫",
         "created_at_iso": "2026-10-01T20:00:00"}]))
    b = build_understanding(hypo_alt_time(evidence=[
        {"polarity": "support", "excerpt": "我喜欢猫",
         "created_at": "2026-10-01T20:00:00"}]))
    assert a["quotes"][0]["when"] == "昨天"
    assert b["quotes"][0]["when"] == "昨天"


def test_outdated_note_quotes_the_users_own_change_words():
    """变化说明里应该出现用户当时说的话，而不是一句笼统的"变了"。"""
    row = build_note({
        "kind": "dynamic", "content": "晚上十点后学习效率最高",
        "memory_status": "outdated",
        "status_reason": "用户表示自己变了：我最近已经改成早上学习了。",
        "created_at": "2026-10-01T20:00:00"}, NOW)
    assert "改成早上学习了" in row["change_note"]
    assert "用户表示自己变了" not in row["change_note"]


def test_internal_status_reason_is_not_shown_as_user_words():
    """别把 "user_correction" 这种内部标记当成人话端出来。"""
    row = build_note({
        "kind": "dynamic", "content": "晚上十点后学习效率最高",
        "memory_status": "outdated", "status_reason": "user_correction",
        "created_at": "2026-10-01T20:00:00"}, NOW)
    assert "user_correction" not in row["change_note"]
    assert row["change_note"]


# ============================================================ 变化与历史

def test_outdated_entry_moves_to_changed_not_current():
    """用户自己的话变了——必须让用户看见。

    早先版本把 outdated 条目直接丢掉，结果"有过变化"永远是空的：
    系统悄悄改写了他的历史，他一点都察觉不到。
    """
    tl = build_timeline([], [{"kind": "dynamic", "content": "晚上学习效率高",
                              "memory_status": "outdated",
                              "created_at_iso": "2026-10-01T20:00:00"}])
    assert tl["items"] == [], "不该再当现状展示"
    assert len(tl["changed"]) == 1, "但必须出现在变化区"
    row = tl["changed"][0]
    assert row["state_text"] == "有过变化"
    assert row["quotes"][0]["text"] == "晚上学习效率高"   # 原话保留
    assert tl["count"] == 1


def test_changed_entry_is_not_current_but_is_visible(client_world=None):
    tl = build_timeline([], [
        {"kind": "dynamic", "content": "晚上学习效率高",
         "memory_status": "outdated", "created_at_iso": "2026-10-01T20:00:00"},
        {"kind": "dynamic", "content": "最近改成早上学习",
         "memory_status": "active", "created_at_iso": "2026-10-02T20:00:00"},
    ])
    assert [r["about"] for r in tl["items"]] == ["最近改成早上学习"]
    assert [r["about"] for r in tl["changed"]] == ["晚上学习效率高"]


def test_note_time_reads_created_at_from_context_public_dict():
    """ContextEntry.to_public_dict 给的是 created_at，不是 created_at_iso。"""
    row = build_note({"kind": "dynamic", "content": "我喜欢猫",
                      "memory_status": "active",
                      "created_at": "2026-10-01T20:00:00"}, NOW)
    assert row["formed_on"] == "昨天"
    assert row["quotes"][0]["when"] == "昨天"


def test_active_entry_becomes_a_timeline_item():
    row = build_note({"kind": "dynamic", "content": "晚上十点后学习效率最高",
                      "memory_status": "active",
                      "created_at_iso": "2026-10-01T20:00:00"}, NOW)
    assert row["about"] == "晚上十点后学习效率最高"
    assert row["quotes"][0]["text"] == "晚上十点后学习效率最高"
    assert row["changed"] is False


def test_timeline_splits_current_and_changed():
    tl = build_timeline([
        hypo(scope="activity:甲"),
        hypo(scope="activity:乙",
             history=[{"action": "change", "iso": "2026-10-02T09:00:00",
                       "detail": json.dumps({"excerpt": "我改主意了"},
                                            ensure_ascii=False)}]),
    ], [])
    assert tl["count"] == 2
    assert len(tl["changed"]) == 1
    assert tl["changed"][0]["topic"].endswith("乙")


def test_timeline_is_sorted_newest_first():
    tl = build_timeline([
        hypo(scope="activity:旧", created="2026-09-01T10:00:00"),
        hypo(scope="activity:新", created="2026-10-02T10:00:00"),
    ], [])
    assert tl["items"][0]["topic"].endswith("新")


def test_items_without_evidence_still_show_but_have_no_quotes():
    """没证据也要能看见（它确实是系统的一条理解），只是不编造来源。"""
    row = build_understanding(hypo(evidence=[]))
    assert row["about"]
    assert row["quotes"] == []


# ============================================================ 首次体验引导

def test_first_run_is_detected_for_a_brand_new_user():
    st = onboarding_state(0, [], 0)
    assert st["is_first_run"] is True and st["done"] is False
    assert len(st["topics"]) == 3


def test_onboarding_covers_as_user_talks():
    st = onboarding_state(3, [{"content": "我一般晚上十点后学习效率最高"}], 0)
    assert [t["key"] for t in st["topics"] if t["covered"]] == ["routine"]

    st = onboarding_state(5, [
        {"content": "我一般晚上十点后学习效率最高"},
        {"content": "我很喜欢玩单机游戏"},
        {"content": "希望你先给我提示，别直接给答案"},
    ], 0)
    assert st["covered"] == 3


def test_onboarding_finishes_and_stops_asking():
    st = onboarding_state(14, [{"content": "我喜欢喝红茶"}], 3)
    assert st["is_first_run"] is False
    assert st["done"] is True


def test_onboarding_is_not_a_quiz():
    """每个方向都是邀请口气，不是表单字段式的"你的学习时间是？"。

    判定口径：邀请里应该有让对方放松的词（说说/随便/就行），
    而不是把人当成填空题。
    """
    st = onboarding_state(0, [], 0)
    soft = ("说说", "随便", "就行", "？")
    for t in st["topics"]:
        assert t["name"]
        assert t["ask"]
        assert any(w in t["ask"] for w in soft), f"太像问卷了：{t['ask']}"


def test_onboarding_adds_no_persistent_state():
    """引导状态是推导出来的，不落库——纯函数，没有 IO 参数。"""
    a = onboarding_state(2, [{"content": "我喜欢猫"}], 0)
    b = onboarding_state(2, [{"content": "我喜欢猫"}], 0)
    assert a == b


# ============================================================ API

@pytest.fixture
def client(tmp_path, monkeypatch):
    import core.pipeline as pm
    from core.service_state import ServiceState

    monkeypatch.setattr(pm, "THUMB_DIR", tmp_path / "thumbs")
    store = MemoryStore(tmp_path / "web.db")
    app = create_app(store=store, model=MockChatModel(),
                     service_state=ServiceState())
    return TestClient(app), store


def test_api_understanding_is_empty_for_new_user(client):
    c, _ = client
    r = c.get("/api/understanding").json()
    assert r["count"] == 0 and r["items"] == []
    assert r["onboarding"]["is_first_run"] is True


def test_api_warmup_offers_invitation_for_new_user(client):
    c, _ = client
    r = c.get("/api/understanding/warmup").json()
    assert r["active"] is True
    assert r["opening"]
    assert len(r["topics"]) == 3


def test_api_understanding_never_leaks_internal_fields(client):
    """这是产品底线：整个响应体里不许出现内部字段名。"""
    c, store = client
    hs = HypothesisStore(store.db_path)
    cs = ContextStore(store.db_path)
    h, _ = hs.create("用户近期多次进行「写作业」相关活动，持续期。",
                     "activity:写作业")
    # 用真实链路造一条有原话的证据，不手工塞数据
    cs.add_entry(kind="dynamic", content="晚上十点后学习效率最高",
                 source="user_direct", source_grade="user_direct")
    hs.record_natural_evidence(h.id, "对，我就是晚上学习效率最高。", "support")

    body = c.get("/api/understanding").text
    for word in FORBIDDEN_IN_OUTPUT:
        assert word not in body, f"响应体里出现了内部字段：{word}"
    r = c.get("/api/understanding").json()
    assert r["count"] >= 1
    all_quotes = [q["text"] for i in r["items"] + r["changed"] for q in i["quotes"]]
    assert all_quotes, "必须能指回用户自己的话"
    assert any("晚上学习效率最高" in q for q in all_quotes), all_quotes


def test_api_understanding_survives_empty_db(client):
    c, _ = client
    assert c.get("/api/understanding").status_code == 200
    assert c.get("/api/understanding/warmup").status_code == 200