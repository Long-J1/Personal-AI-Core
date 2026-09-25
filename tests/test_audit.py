"""C1 沉默记账测试：观察 → 判断不介入 → 理由，落盘可审计，且绝不进正式记忆库。"""
from __future__ import annotations

import json

from tests.helpers import make_image


def test_silence_audit_written_and_stays_out_of_memory(
    pipeline, store, tmp_path, monkeypatch
):
    import core.audit as audit_mod

    target = tmp_path / "silence_audit.jsonl"
    monkeypatch.setattr(audit_mod, "SILENCE_LOG", target)

    result = pipeline.observe(make_image("AUDIT"))
    assert result.ok and result.intervention is not None

    # 1) 结构化落盘：观察 → 判断不介入 → 理由
    lines = [
        json.loads(l)
        for l in target.read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    assert len(lines) == 1
    rec = lines[0]
    assert rec["kind"] == "observation_silence"
    assert rec["intervene"] is False
    assert rec["reason"]
    assert rec["event_id"] == result.event.id
    assert rec["iso"] and rec["epoch"]

    # 2) 不进入正式记忆库：事件照常 1 条，沉默理由在记忆里检索不到
    assert store.count_events() == 1
    assert store.search_events([rec["reason"][:12]]) == []
    assert store.list_facts() == []


def test_silence_audit_failure_does_not_break_observe(
    pipeline, store, tmp_path, monkeypatch
):
    """记账写失败（目标不可写）不能影响主管线——失败可恢复。"""
    import core.audit as audit_mod

    monkeypatch.setattr(audit_mod, "SILENCE_LOG", tmp_path)  # 目录，不可当文件写
    result = pipeline.observe(make_image("AUDIT2"))
    assert result.ok and result.stored


def test_paused_observe_makes_no_silence_record(pipeline, store, tmp_path, monkeypatch):
    """暂停 = 没有观察、没有判断，也就没有沉默记账。"""
    import core.audit as audit_mod

    target = tmp_path / "silence_paused.jsonl"
    monkeypatch.setattr(audit_mod, "SILENCE_LOG", target)
    store.paused = True

    result = pipeline.observe(make_image("AUDIT3"))
    assert not result.ok and "暂停" in (result.skip_reason or "")
    assert not target.exists()
