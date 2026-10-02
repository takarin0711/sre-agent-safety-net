"""受信側の単体検証（design.md 4.3 / シナリオ G-01〜G-06）。

ここで見たいのは「Bolt が 401 を返すこと」ではなく、
**署名検証を通らない要求がストアを 1 レコードも変えないこと**である（判定基準4）。
したがって各ケースでストアの状態まで確認する。

G-05 は対比のシナリオで、**署名検証を外すと改変ペイロードが通ってしまう**ことを実測する。
防御の効果は、外したときに何が起きるかでしか測れない（Phase 2 の strict と同じ構図）。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from agents.policy import build_denied_event
from agents.schemas import PolicyDecision, ViolatedRule
from agents.validation import validate_action
from slack import handler
from slack.blocks import ACTION_ID_REJECT
from slack.store import HandoffStatus, Store
from tests.fakes import (
    FakeSlack,
    block_actions_payload,
    signed_request,
    unsigned_request,
)

FIXTURES = Path(__file__).parent / "fixtures"
SECRET = "test-signing-secret"
TRACE = "trace-sig-1"
OTHER_TRACE = "trace-sig-2"
APPROVER = "U-APPROVER"


@pytest.fixture(autouse=True)
def slack_env(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("SLACK_APPROVER_USER_IDS", f"{APPROVER},U-OTHER")


@pytest.fixture
def store() -> Store:
    store = Store()  # conftest が SLACK_STORE_PATH を一時ファイルに向けている
    for trace_id in (TRACE, OTHER_TRACE):
        action = validate_action(
            (FIXTURES / "n01_dangerous_delete_pvc.json").read_text(encoding="utf-8"),
            original_request="本番が落ちた",
            trace_id=trace_id,
        )
        decision = PolicyDecision(
            allow=False,
            violated_rules=[ViolatedRule(rule="PROHIBIT_PVC_DELETE", message="不可逆です")],
        )
        event = build_denied_event(
            decision, action=action, trace_id=trace_id, original_request="本番が落ちた"
        )
        store.create_pending(
            trace_id=trace_id, kind="policy_denied", event_json=event.model_dump_json()
        )
    return store


@pytest.fixture
def client() -> FakeSlack:
    return FakeSlack()


def drain(store, client):
    """受信のあとに実行プロセスを 1 周回す。

    **Phase 5 で承認と実行は別プロセスになった**（D-027）。G 系が確かめているのは
    「検証を通らない要求が実行に至らないこと」「再送があっても実行は 1 回だけ」であり、
    それはプロセスの分け方に依らないシステムの性質である。受信だけを見て終えると、
    分離したことで検証範囲が狭まってしまう。
    """
    from agents import executor
    from tests.fakes import FakeApis

    return executor.run_once(store=store, apis=FakeApis(), client=client)


def app_with(client: FakeSlack, *, signature_verification: bool = True):
    # token_verification_enabled=True だと Bolt は起動時に 1 回だけ auth.test を行い、
    # 結果を使い回す（False にするとリクエストごとに実 Slack へ飛ぶ）。
    return handler.build_app(client=client, signature_verification=signature_verification)


# ── G-01 正しい署名 ───────────────────────────────────────


def test_g01_valid_signature_is_processed(store, client):
    response = app_with(client).dispatch(signed_request(block_actions_payload(TRACE), secret=SECRET))
    assert response.status == 200
    # 受信プロセスが進めるのはここまで。**実行はしない**（D-027）。
    assert store.get(TRACE).status == HandoffStatus.APPROVED
    drain(store, client)
    assert store.get(TRACE).status == HandoffStatus.EXECUTED
    assert "chat_update" in client.methods()


def test_g01_reject_button_is_processed(store, client):
    payload = block_actions_payload(TRACE, action_id=ACTION_ID_REJECT)
    response = app_with(client).dispatch(signed_request(payload, secret=SECRET))
    assert response.status == 200
    assert store.get(TRACE).status == HandoffStatus.REJECTED


# ── G-02 / G-03 / G-04 検証を通らない要求 ────────────────


def test_g02_missing_signature_changes_nothing(store, client):
    response = app_with(client).dispatch(unsigned_request(block_actions_payload(TRACE)))
    assert response.status == 401
    assert store.get(TRACE).status == HandoffStatus.PENDING
    assert client.api_calls == []


def test_g03_tampered_body_changes_nothing(store, client):
    """署名を計算したあとに本文を差し替える（改変ペイロードの再現）。"""
    original = block_actions_payload(TRACE)
    tampered = block_actions_payload(OTHER_TRACE)
    response = app_with(client).dispatch(
        signed_request(original, secret=SECRET, tamper_with=tampered)
    )
    assert response.status == 401
    assert store.get(TRACE).status == HandoffStatus.PENDING
    assert store.get(OTHER_TRACE).status == HandoffStatus.PENDING
    assert client.api_calls == []


def test_g04_replayed_old_request_changes_nothing(store, client):
    """Slack は 5 分より古いタイムスタンプをリプレイとみなす。"""
    stale_timestamp = int(time.time()) - 60 * 10
    response = app_with(client).dispatch(
        signed_request(block_actions_payload(TRACE), secret=SECRET, timestamp=stale_timestamp)
    )
    assert response.status == 401
    assert store.get(TRACE).status == HandoffStatus.PENDING


def test_g03_wrong_secret_changes_nothing(store, client):
    response = app_with(client).dispatch(
        signed_request(block_actions_payload(TRACE), secret="attacker-guessed-secret")
    )
    assert response.status == 401
    assert store.get(TRACE).status == HandoffStatus.PENDING


# ── G-05 対比: 署名検証を外すと通ってしまう ───────────────


def test_g05_without_verification_the_tampered_payload_is_executed(store, client):
    """**この防御を外すと、外部から任意の Trace ID の承認を実行できる。**

    運用原則2 が「署名検証を必須とする」と書いている理由の実測。
    ここで通ってしまうのは異常ではなく、防御を外したことの帰結である。
    """
    original = block_actions_payload(TRACE)
    tampered = block_actions_payload(OTHER_TRACE)
    response = app_with(client, signature_verification=False).dispatch(
        signed_request(original, secret=SECRET, tamper_with=tampered)
    )
    assert response.status == 200
    drain(store, client)
    assert store.get(OTHER_TRACE).status == HandoffStatus.EXECUTED  # 改変した側が実行された
    assert store.get(TRACE).status == HandoffStatus.PENDING


# ── G-06 Slack の再送（3 秒ルール）────────────────────────


def test_g06_slack_retry_executes_only_once(store, client):
    """人間は 1 回しか押していないが、受信側には 2 回届く（D-024）。

    ヘッダで弾くのではなく、**ストアの条件付き更新で弾く**ことを確認する。
    ヘッダに依存した防御にすると、ヘッダの無い二重押下が素通りする。
    """
    app = app_with(client)
    payload = block_actions_payload(TRACE)
    first = app.dispatch(signed_request(payload, secret=SECRET))
    second = app.dispatch(signed_request(payload, secret=SECRET, retry_num=1))

    assert first.status == 200
    assert second.status == 200  # 再送は異常ではない。Slack 側の正常な動作である
    # 2 回届いても承認は 1 回しか成立していない（ストアの CAS で弾いた）
    audit = [entry["action"] for entry in store.audit_entries(TRACE)]
    assert audit.count("approve") == 1
    assert "lock_conflict" in audit

    drain(store, client)
    row = store.get(TRACE)
    assert row.status == HandoffStatus.EXECUTED
    audit = [entry["action"] for entry in store.audit_entries(TRACE)]
    assert audit.count("execute") == 1
    # 2 回目は本人にだけ「処理済み」と返す
    assert "chat_postEphemeral" in client.methods()


# ── RBAC（付帯要件。承認系のみ上位ロールを要求する）────────


def test_rbac_non_approver_cannot_approve(store, client):
    payload = block_actions_payload(TRACE, user_id="U-STRANGER")
    response = app_with(client).dispatch(signed_request(payload, secret=SECRET))
    # 拒否は正常な処理結果。200 を返さないと Slack から失敗に見え、再送される
    assert response.status == 200
    assert store.get(TRACE).status == HandoffStatus.PENDING
    assert "chat_postEphemeral" in client.methods()
    assert any(e["action"] == "denied_by_rbac" for e in store.audit_entries(TRACE))


def test_rbac_rejection_is_not_privileged(store, client):
    """却下は危険な操作ではないので、承認者でなくても押せる。"""
    payload = block_actions_payload(TRACE, user_id="U-STRANGER", action_id=ACTION_ID_REJECT)
    app_with(client).dispatch(signed_request(payload, secret=SECRET))
    assert store.get(TRACE).status == HandoffStatus.REJECTED


def test_rbac_denies_everyone_when_list_is_unset(store, client, monkeypatch):
    """未設定が「全員許可」になるほうが事故として重い。認可も Fail-Closed に倒す。"""
    monkeypatch.delenv("SLACK_APPROVER_USER_IDS", raising=False)
    app_with(client).dispatch(signed_request(block_actions_payload(TRACE), secret=SECRET))
    assert store.get(TRACE).status == HandoffStatus.PENDING
