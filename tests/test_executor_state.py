"""ステートと再試行の単体検証（phase5 design.md 4.2 / シナリオ W-01〜W-08）。

**クラスタにも Slack にも触らない。** 確かめるのは「実行プロセスがどの状態から
どの状態へ進むか」であり、実行そのものではない（それは K 系）。

本フェーズで新しいのは **`executing`** である。これが無いと、実行中に落ちたプロセスの
エントリが `approved` のまま残り、再起動で**再実行される**（＝二重実行）。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from agents import executor
from agents.policy import build_denied_event
from agents.schemas import PolicyDecision, ViolatedRule
from agents.validation import validate_action
from slack import handler
from slack.blocks import ACTION_ID_RETRY, build_notification
from slack.store import HandoffStatus, StateConflict, Store, can_retry
from tests.fakes import FakeApis, FakeSlack

FIXTURES = Path(__file__).parent / "fixtures"
TRACE = "trace-exec-state"


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
    return build_denied_event(
        decision, action=action, trace_id=trace_id, original_request="本番が落ちた"
    ).model_dump_json()


def seed(store: Store, *, trace_id: str = TRACE) -> None:
    store.create_pending(trace_id=trace_id, kind="policy_denied", event_json=denied_event_json(trace_id))


def approved(store: Store, *, trace_id: str = TRACE) -> None:
    seed(store, trace_id=trace_id)
    store.approve(trace_id, actor="U-1")


# ── W-01 実行側の排他 ────────────────────────────────────


def test_w01_only_one_executor_can_claim_an_approved_entry(store) -> None:
    """``approved -> executing`` の CAS が実行側の排他である。

    承認側（``pending -> approved``）とは別の関門であることが重要で、
    承認が 1 回でも、実行プロセスが複数いれば実行は複数回起こりうる。
    """
    approved(store)
    results: list[str] = []
    barrier = threading.Barrier(2)

    def claim() -> None:
        barrier.wait()
        try:
            store.claim_for_execution(TRACE)
            results.append("claimed")
        except StateConflict:
            results.append("conflict")

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == ["claimed", "conflict"]
    assert store.get(TRACE).status == HandoffStatus.EXECUTING


def test_w01_run_once_executes_each_entry_exactly_once(store) -> None:
    approved(store)
    apis = FakeApis()
    executor.run_once(store=store, apis=apis, client=FakeSlack())
    executor.run_once(store=store, apis=apis, client=FakeSlack())
    assert apis.methods.count("delete_namespaced_persistent_volume_claim") == 1


# ── W-02 クラッシュリカバリ ───────────────────────────────


def test_w02_an_entry_left_executing_becomes_unknown(store) -> None:
    """**``approved`` に戻してはならない。** それが二重実行の典型的な作り方である。

    「実行を開始した記録はあるが結果が無い」＝定義どおり実行有無が不明（D-028）。
    """
    approved(store)
    store.claim_for_execution(TRACE)  # ここでプロセスが落ちたことにする

    executor.recover(store, FakeSlack())

    row = store.get(TRACE)
    assert row.status == HandoffStatus.UNKNOWN
    assert row.status != HandoffStatus.APPROVED
    assert row.executed_at is None
    assert not can_retry(row)


def test_w02_recovered_entry_is_not_picked_up_again(store) -> None:
    approved(store)
    store.claim_for_execution(TRACE)
    executor.recover(store, FakeSlack())

    apis = FakeApis()
    executor.run_once(store=store, apis=apis, client=FakeSlack())
    assert apis.calls == []  # unknown は実行待ちではない


# ── W-03 / W-04 再試行 ───────────────────────────────────


def fail_once(store: Store, client: FakeSlack) -> None:
    """403 を返させて ``execution_failed`` を作る。"""
    from kubernetes.client.exceptions import ApiException

    exc = ApiException(status=403, reason="Forbidden")
    exc.body = '{"message":"forbidden"}'
    executor.run_once(store=store, apis=FakeApis(raises=exc), client=client)


def test_w03_retry_increments_the_counter_and_returns_to_the_queue(store) -> None:
    approved(store)
    client = FakeSlack()
    fail_once(store, client)
    assert store.get(TRACE).status == HandoffStatus.EXECUTION_FAILED
    assert store.get(TRACE).retry_count == 0

    handler.retry(TRACE, actor="U-1", channel="C-TEST", message_ts="1.0", client=client, store=store)
    row = store.get(TRACE)
    assert row.status == HandoffStatus.APPROVED
    assert row.retry_count == 1
    # 試行の開始時刻はリセットされる（次の試行はまだ始まっていない）
    assert row.execution_started_at is None


def test_w04_retry_uses_the_stored_action_not_the_payload(store) -> None:
    """再試行の同一性（運用原則4）。変わるのは status と retry_count だけである。"""
    approved(store)
    client = FakeSlack()
    before = store.get(TRACE).event_json
    fail_once(store, client)
    handler.retry(TRACE, actor="U-1", channel="C-TEST", message_ts="1.0", client=client, store=store)

    assert store.get(TRACE).event_json == before
    apis = FakeApis()
    executor.run_once(store=store, apis=apis, client=client)
    assert apis.last()[0] == "delete_namespaced_persistent_volume_claim"
    assert apis.last()[1] == ("payment-db-data", "prod")
    assert store.get(TRACE).status == HandoffStatus.EXECUTED


def test_w04_a_corrupted_record_is_never_retried_into_the_cluster(store) -> None:
    """ストアは可変の永続状態である。再試行でも必ず再検証する（D-020）。"""
    approved(store)
    broken = json.loads(denied_event_json())
    broken["action"]["operation"] = "rm_minus_rf"
    store._update(TRACE, "SET event_json = ?", (json.dumps(broken),))

    apis = FakeApis()
    executor.run_once(store=store, apis=apis, client=FakeSlack())
    assert store.get(TRACE).status == HandoffStatus.EXECUTION_FAILED
    assert apis.calls == []


# ── W-05 再試行の上限 ────────────────────────────────────


def test_w05_retry_stops_at_the_limit(store, monkeypatch) -> None:
    monkeypatch.setenv("EXECUTION_MAX_RETRIES", "2")
    approved(store)
    client = FakeSlack()

    for expected in (1, 2):
        fail_once(store, client)
        handler.retry(TRACE, actor="U-1", channel="C-TEST", message_ts="1.0", client=client, store=store)
        assert store.get(TRACE).retry_count == expected

    fail_once(store, client)
    row = store.get(TRACE)
    assert not can_retry(row)
    outcome = handler.retry(
        TRACE, actor="U-1", channel="C-TEST", message_ts="1.0", client=client, store=store
    )
    assert outcome.result == "conflict"
    assert store.get(TRACE).status == HandoffStatus.EXECUTION_FAILED


# ── W-06 unknown に再試行ボタンを出さない（D-028）─────────


def test_w06_unknown_offers_no_retry_button(store) -> None:
    """**パターン4 に承認ボタンを出さないのとまったく同じ規律である。**

    評価できていないものに承認ボタンを出さないように、
    実行されたか分からないものに再試行ボタンを出さない。
    """
    approved(store)
    import urllib3

    executor.run_once(
        store=store,
        apis=FakeApis(raises=urllib3.exceptions.ReadTimeoutError(None, "https://k8s", "timed out")),
        client=FakeSlack(),
    )
    row = store.get(TRACE)
    assert row.status == HandoffStatus.UNKNOWN
    assert not can_retry(row)

    from agents.escalation import _execution_event
    from agents.execution import ExecutionOutcome

    event = _execution_event(row, "execution_unknown", ExecutionOutcome("unknown", "応答なし"))
    notification = build_notification(event, retryable=can_retry(row), retry_count=row.retry_count)
    action_ids = [
        element["action_id"]
        for block in notification.blocks
        if block["type"] == "actions"
        for element in block["elements"]
    ]
    assert action_ids == []
    assert ACTION_ID_RETRY not in json.dumps(notification.blocks, ensure_ascii=False)


# ── W-07 中止は却下と別の事実 ────────────────────────────


def test_w07_abandon_is_not_the_same_as_reject(store) -> None:
    """``rejected`` は実行を試みていない。``abandoned`` は試みたうえでやめた。"""
    approved(store)
    client = FakeSlack()
    fail_once(store, client)
    handler.abandon(TRACE, actor="U-1", channel="C-TEST", message_ts="1.0", client=client, store=store)

    row = store.get(TRACE)
    assert row.status == HandoffStatus.ABANDONED
    assert row.status != HandoffStatus.REJECTED
    assert row.execution_started_at is not None  # 試みた事実が残っている
    assert any(e["action"] == "abandon" for e in store.audit_entries(TRACE))


# ── W-08 時刻の意味（phase4 考察11 の解消）────────────────


def test_w08_executed_at_is_set_only_on_success(store) -> None:
    approved(store)
    client = FakeSlack()
    fail_once(store, client)
    row = store.get(TRACE)
    assert row.status == HandoffStatus.EXECUTION_FAILED
    # Phase 4 はここに時刻が入っていた（実行層に到達していないのに）。
    assert row.executed_at is None
    assert row.execution_started_at is not None

    handler.retry(TRACE, actor="U-1", channel="C-TEST", message_ts="1.0", client=client, store=store)
    executor.run_once(store=store, apis=FakeApis(), client=client)
    row = store.get(TRACE)
    assert row.status == HandoffStatus.EXECUTED
    assert row.executed_at is not None
    assert row.execution_started_at is not None


def test_w08_audit_records_every_execution_attempt(store) -> None:
    """``audit_log`` は運用ステートより寿命が長い（phase4 design.md 3.5.1）。

    再試行して成功した場合、失敗した試行の記録も残っていなければならない。
    """
    approved(store)
    client = FakeSlack()
    fail_once(store, client)
    handler.retry(TRACE, actor="U-1", channel="C-TEST", message_ts="1.0", client=client, store=store)
    executor.run_once(store=store, apis=FakeApis(), client=client)

    actions = [entry["action"] for entry in store.audit_entries(TRACE)]
    assert actions.count("execution_failed") == 1
    assert actions.count("retry") == 1
    assert actions.count("execute") == 1
    override = {e["action"]: e["override_kind"] for e in store.audit_entries(TRACE)}
    assert override["execute"] == "policy_override"
