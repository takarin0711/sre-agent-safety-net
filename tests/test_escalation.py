"""ハンドオフ経路の単体検証（design.md 3.9 / E 系の単体側）。

実機の E 系（Slack 不達）の前に、ここで次の 3 点を決定的に固める。

1. **承認待ちとしてストアに入るのは ``policy_denied`` だけ**（design.md 2.2）
2. **送出の失敗が握り潰されない**（終了コード 5 の材料になる。D-022）
3. **Phase 1〜3 の構造化ログが 1 バイトも変わっていない**

Slack には触れない。``notifier.build_client`` をフェイクに差し替えて経路だけを通す。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic_core import ValidationError
from slack_sdk.errors import SlackApiError

from agents import escalation
from agents.policy import build_denied_event, build_unavailable_event
from agents.schemas import PolicyDecision, ViolatedRule
from agents.validation import validate_action
from slack.store import HandoffStatus, Store
from tests.fakes import FakeSlack

FIXTURES = Path(__file__).parent / "fixtures"
TRACE = "trace-esc-1"


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake-token")
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C-TEST")


@pytest.fixture
def slack(monkeypatch) -> FakeSlack:
    client = FakeSlack()
    monkeypatch.setattr("slack.notifier.build_client", lambda: client)
    return client


def action():
    return validate_action(
        (FIXTURES / "n01_dangerous_delete_pvc.json").read_text(encoding="utf-8"),
        original_request="本番が落ちた",
        trace_id=TRACE,
    )


def denied_event():
    decision = PolicyDecision(
        allow=False, violated_rules=[ViolatedRule(rule="PROHIBIT_PVC_DELETE", message="不可逆です")]
    )
    return build_denied_event(decision, action=action(), trace_id=TRACE, original_request="本番が落ちた")


def unavailable_event():
    return build_unavailable_event(
        "timeout", "応答がありません", action=action(), trace_id=TRACE, original_request="本番が落ちた"
    )


def emitted(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]


# ── ストアに入るのは policy_denied だけ ──────────────────


def test_only_policy_denied_creates_a_pending_entry(slack, capsys):
    escalation.escalate(denied_event(), event="policy.deny", layer="layer3_opa", trace_id=TRACE)
    row = Store().get(TRACE)
    assert row is not None
    assert row.status == HandoffStatus.PENDING
    # 送出に成功したら chat.update の宛先を結びつける
    assert row.channel == "C-TEST"
    assert row.message_ts == "1700000000.000100"
    assert [name for name, _ in slack.api_calls] == ["chat_postMessage"]
    assert emitted(capsys)[-1]["event"] == "slack.notified"


def test_policy_unavailable_creates_no_entry(slack):
    """評価できていないものに承認ボタンは出さない。ストアにも入れない。"""
    escalation.escalate(
        unavailable_event(), event="policy.unavailable", layer="layer3_opa", trace_id=TRACE
    )
    assert Store().get(TRACE) is None
    assert [name for name, _ in slack.api_calls] == ["chat_postMessage"]


def test_validation_failed_creates_no_entry(slack):
    raw = (FIXTURES / "v01_prose.txt").read_text(encoding="utf-8")
    with pytest.raises(ValidationError):
        validate_action(raw, original_request="本番が落ちた", trace_id=TRACE)
    assert Store().get(TRACE) is None


def test_full_text_goes_to_a_thread_reply(slack):
    """本体は要約、全文はスレッド（運用原則1）。Slack は保管先ではない。"""
    raw = (FIXTURES / "v01_prose.txt").read_text(encoding="utf-8")
    with pytest.raises(ValidationError):
        validate_action(raw, original_request="本番が落ちた", trace_id=TRACE)

    calls = slack.api_calls
    assert [name for name, _ in calls] == ["chat_postMessage", "chat_postMessage"]
    assert "thread_ts" not in calls[0][1]
    assert calls[1][1]["thread_ts"] == "1700000000.000100"


# ── 送出の失敗を握り潰さない（D-022）────────────────────


class BrokenSlack(FakeSlack):
    """Slack が ok:false を返す（HTTP 200 で失敗が返る。第3層の undefined と同じ罠）。"""

    def chat_postMessage(self, **kwargs):  # type: ignore[override]
        self.calls.append(("chat_postMessage", kwargs))
        raise SlackApiError("invalid_auth", self._response({"ok": False, "error": "invalid_auth"}))


class UnreachableSlack(FakeSlack):
    def chat_postMessage(self, **kwargs):  # type: ignore[override]
        self.calls.append(("chat_postMessage", kwargs))
        raise TimeoutError("timed out")


@pytest.mark.parametrize(
    ("client_class", "failure"), [(BrokenSlack, "api_error"), (UnreachableSlack, "transport")]
)
def test_delivery_failure_is_recorded_not_swallowed(monkeypatch, capsys, client_class, failure):
    monkeypatch.setattr("slack.notifier.build_client", lambda: client_class())
    escalation.escalate(denied_event(), event="policy.deny", layer="layer3_opa", trace_id=TRACE)

    # 遮断は成立している。失敗しているのは「人間に届けること」だけ。
    assert escalation.delivery_failed(TRACE) is True
    assert Store().get(TRACE).status == HandoffStatus.NOTIFY_FAILED
    assert Store().get(TRACE).message_ts is None  # 更新先が無い状態を表せている
    last = emitted(capsys)[-1]
    assert last["event"] == "slack.failed"
    assert last["failure"] == failure


def test_successful_delivery_does_not_mark_failure(slack):
    escalation.escalate(denied_event(), event="policy.deny", layer="layer3_opa", trace_id=TRACE)
    assert escalation.delivery_failed(TRACE) is False


def test_no_slack_flag_reproduces_the_phase3_path(slack):
    """--no-slack。ストアにも入れず、送出もしない（Phase 3 の経路の再現）。"""
    escalation.set_enabled(False)
    escalation.escalate(denied_event(), event="policy.deny", layer="layer3_opa", trace_id=TRACE)
    assert Store().get(TRACE) is None
    assert slack.api_calls == []
    assert escalation.delivery_failed(TRACE) is False


# ── 既存のログ行を変えていないこと ───────────────────────


def test_existing_log_line_is_emitted_unchanged(slack, capsys):
    """Phase 2・3 の assets と比較できなくなると、過去の判定が再現できない。"""
    raw = (FIXTURES / "v01_prose.txt").read_text(encoding="utf-8")
    with pytest.raises(ValidationError):
        validate_action(raw, original_request="本番が落ちた", trace_id=TRACE)

    lines = emitted(capsys)
    first = lines[0]
    assert first["event"] == "validation.error"
    # Phase 2 の時点のキー構成。増やしても減らしてもいけない。
    assert set(first) == {"event", "layer", "trace_id", "error_type", "error_count", "escalation"}
    # 送出の結果は**別の行**として足す
    assert [line["event"] for line in lines[1:]] == ["slack.notified"]


# ── 送出先が DM でも成立すること（チャンネルを作らずに検証する）──────


def test_user_id_destination_is_resolved_to_a_dm(monkeypatch, slack):
    """``SLACK_CHANNEL_ID`` にユーザ ID を書くと Bot との DM に送る。

    検証用のチャンネルを作らずに実機確認できるようにするための経路。
    ``conversations.open`` で ``D...`` に解決し、``chat.update`` の宛先もそれになる。
    """
    from slack import notifier

    notifier._DM_CACHE.clear()
    monkeypatch.setenv("SLACK_CHANNEL_ID", "U-ME")

    escalation.escalate(denied_event(), event="policy.deny", layer="layer3_opa", trace_id=TRACE)

    methods = [name for name, _ in slack.api_calls]
    assert methods[0] == "conversations_open"
    assert slack.last("chat_postMessage")["channel"] == "D-TEST"
    # ストアに入るのは解決後の D... （押下時の chat.update がこれを使う）
    assert Store().get(TRACE).channel == "D-TEST"


def test_dm_resolution_is_cached(monkeypatch, slack):
    """押下のたびに conversations.open を叩かない（3 秒ルールに効く）。"""
    from slack import notifier

    notifier._DM_CACHE.clear()
    monkeypatch.setenv("SLACK_CHANNEL_ID", "U-ME")
    for trace in ("t-a", "t-b"):
        escalation.escalate(
            denied_event().model_copy(update={"trace_id": trace}),
            event="policy.deny",
            layer="layer3_opa",
            trace_id=trace,
        )
    assert [name for name, _ in slack.api_calls].count("conversations_open") == 1
