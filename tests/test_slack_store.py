"""承認ステートストアの単体検証（design.md 4.2 / シナリオ T-01〜T-07）。

運用原則4 の 6 つの性質（排他性・永続性・有効期限・冪等性・追跡性・再試行の同一性）が
実際に満たされているかを、Slack を介さずに確かめる。
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agents.policy import build_denied_event
from agents.schemas import PolicyDecision, ViolatedRule
from agents.validation import validate_action
from slack import handler
from slack.store import HandoffStatus, StateConflict, Store, is_stale
from tests.fakes import FakeSlack

FIXTURES = Path(__file__).parent / "fixtures"
TRACE = "trace-store-1"


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "store.sqlite3")


def denied_event_json(trace_id: str = TRACE) -> str:
    action = validate_action(
        (FIXTURES / "n01_dangerous_delete_pvc.json").read_text(encoding="utf-8"),
        original_request="本番が落ちた",
        trace_id=trace_id,
    )
    decision = PolicyDecision(
        allow=False,
        violated_rules=[ViolatedRule(rule="PROHIBIT_PVC_DELETE", message="不可逆な操作です")],
    )
    event = build_denied_event(
        decision, action=action, trace_id=trace_id, original_request="本番が落ちた"
    )
    return event.model_dump_json()


def seed(store: Store, *, trace_id: str = TRACE, ttl: int = 900) -> None:
    store.create_pending(
        trace_id=trace_id, kind="policy_denied", event_json=denied_event_json(trace_id), ttl_seconds=ttl
    )


# ── T-01 排他性 ───────────────────────────────────────────


def test_t01_concurrent_approval_succeeds_once(store):
    """二重押下・Slack の再送。**成功は 1 つだけ**（D-024）。"""
    seed(store)
    results: list[str] = []
    barrier = threading.Barrier(4)

    def attempt(actor: str) -> None:
        barrier.wait()
        try:
            store.approve(TRACE, actor=actor)
            results.append("ok")
        except StateConflict:
            results.append("conflict")

    threads = [threading.Thread(target=attempt, args=(f"U{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count("ok") == 1, results
    assert results.count("conflict") == 3, results
    assert store.get(TRACE).status == HandoffStatus.APPROVED


# ── T-02 却下 ────────────────────────────────────────────


def test_t02_reject_closes_the_state(store):
    """UI を更新するだけでは pending が残り続ける。必ずステートを閉じる。"""
    seed(store)
    store.reject(TRACE, actor="U-1")
    assert store.get(TRACE).status == HandoffStatus.REJECTED
    with pytest.raises(StateConflict):
        store.approve(TRACE, actor="U-1")


# ── T-03 有効期限 ────────────────────────────────────────


def test_t03_expired_entry_cannot_be_approved(store):
    """満了ジョブが掃く前でも承認できない（期限は承認可否の条件である）。"""
    seed(store, ttl=0)
    with pytest.raises(StateConflict) as excinfo:
        store.approve(TRACE, actor="U-1")
    assert excinfo.value.actual == "pending(expired)"

    assert [row.trace_id for row in store.due_for_expiry()] == [TRACE]
    store.expire(TRACE)
    assert store.get(TRACE).status == HandoffStatus.EXPIRED


def test_t03_unexpired_entry_is_not_swept(store):
    seed(store, ttl=900)
    assert store.due_for_expiry() == []


# ── T-04 冪等性 ──────────────────────────────────────────


def test_t04_executed_entry_is_never_executed_again(store):
    seed(store)
    store.approve(TRACE, actor="U-1")
    store.mark_executed(TRACE, "dry_run")
    executed_at = store.get(TRACE).executed_at
    assert executed_at is not None

    with pytest.raises(StateConflict):
        store.mark_executed(TRACE, "dry_run")
    assert store.get(TRACE).executed_at == executed_at


def test_t04_executed_at_is_only_set_on_success(store):
    """失敗したときに executed_at を立てると、再試行が冪等性で弾かれてしまう。"""
    seed(store)
    store.approve(TRACE, actor="U-1")
    assert store.get(TRACE).executed_at is None


# ── T-05 TOCTOU ─────────────────────────────────────────


def test_t05_staleness_is_measured_from_proposal(store):
    """**承認 → 実行ではなく、提案 → 実行で測る**（design.md 3.5）。"""
    seed(store)
    row = store.get(TRACE)
    assert not is_stale(row, threshold=300)
    assert is_stale(row, at=datetime.now(UTC) + timedelta(seconds=301), threshold=300)


def test_t05_stale_approval_is_not_executed(store, monkeypatch):
    monkeypatch.setenv("SLACK_TOCTOU_SECONDS", "0")
    seed(store)
    client = FakeSlack()
    outcome = handler.approve(
        TRACE, actor="U-1", channel="C-TEST", message_ts="1.0", client=client, store=store
    )
    assert outcome.result == "stale"
    assert store.get(TRACE).status == HandoffStatus.STALE
    assert store.get(TRACE).executed_at is None
    assert "実行中止" in client.last("chat_update")["text"]


# ── T-06 再試行の同一性 ──────────────────────────────────


def test_t06_action_is_restored_from_the_store(store):
    """実行内容はストアから引く。ペイロードの値からは再構成しない（運用原則2）。"""
    seed(store)
    client = FakeSlack()
    outcome = handler.approve(
        TRACE, actor="U-1", channel="C-TEST", message_ts="1.0", client=client, store=store
    )
    assert outcome.result == "executed"
    row = store.get(TRACE)
    assert row.status == HandoffStatus.EXECUTED
    # 実行対象はストアのレコードそのもの
    assert json.loads(row.event_json)["action"]["operation"] == "kubectl_delete_pvc"
    # policy_override が監査に残る（OPA の Decision Log には残らない）
    audit = {entry["action"]: entry for entry in store.audit_entries(TRACE)}
    assert audit["execute"]["override_kind"] == "policy_override"
    assert audit["execute"]["actor"] == "U-1"


# ── T-07 ストアも信じない ────────────────────────────────


def test_t07_corrupted_record_never_reaches_the_execution_layer(store):
    """ストアは可変の永続状態である。復元時に strict に再検証する（D-020）。"""
    seed(store)
    broken = json.loads(denied_event_json())
    broken["action"]["operation"] = "rm_minus_rf"  # enum に無い値
    store._update(TRACE, "SET event_json = ?", (json.dumps(broken),))

    client = FakeSlack()
    outcome = handler.approve(
        TRACE, actor="U-1", channel="C-TEST", message_ts="1.0", client=client, store=store
    )
    assert outcome.result == "restore_failed"
    assert store.get(TRACE).status == HandoffStatus.EXECUTION_FAILED
    assert store.get(TRACE).executed_at is not None  # 実行を試みた事実は残す
    assert any(e["action"] == "restore_failed" for e in store.audit_entries(TRACE))


def test_t07_missing_record_is_a_conflict(store):
    client = FakeSlack()
    outcome = handler.approve(
        "no-such-trace", actor="U-1", channel="C-TEST", message_ts="1.0", client=client, store=store
    )
    assert outcome.result == "conflict"
    assert "処理済み" in client.last("chat_postEphemeral")["text"]


def test_displayed_deadline_is_the_shorter_of_the_two(store):
    """承認期限（TTL）と TOCTOU 閾値のうち、**実際に効くのは短いほう**。

    15 分の承認期限を表示しながら 5 分で stale に倒れるのは、受け取った人を欺く。
    """
    from slack.store import actionable_until

    seed(store, ttl=900)
    row = store.get(TRACE)
    assert actionable_until(row, threshold=300) == row.created_at + timedelta(seconds=300)
    # TOCTOU 閾値のほうが長ければ、承認期限が効く
    assert actionable_until(row, threshold=1800) == row.expires_at
