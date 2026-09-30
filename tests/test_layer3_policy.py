"""第3層 (OPA) 呼び出し側の単体検証。

ここで検証するのは **agents/policy.py の振る舞い**だけである。
ポリシーの内容（何が禁止か）は `opa test opa/` の仕事であり、ここには書かない。
書いた時点で、ポリシーの正しさを Python のテストが二重に主張することになり、
「ポリシーはアプリと独立に単体テストできる」という D-002 の主張が濁る。

対応するシナリオは docs/phases/phase3-opa/design.md:

- 3.3  Fail-Closed の 6 つの失敗モード（X-01〜X-05 の単体側）
- 3.2  OPA の応答を無検証で信じない（D-017）
- 3.5  Trace ID をエンベロープで送る（D-016）
- 判定基準7  ポリシーが agents/ に漏れていないこと
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from agents import policy
from agents.policy import PolicyUnavailableError, evaluate_action
from agents.schemas import Environment, Operation, ProposedAction
from agents.validation import validate_action

FIXTURES = Path(__file__).parent / "fixtures"
POLICY_FIXTURES = FIXTURES / "policy"
URL = "http://opa.invalid/v1/data/sre/agent/decision"

ALLOW_BODY = '{"result": {"allow": true, "violated_rules": []}, "decision_id": "dec-1"}'
DENY_RESULT = {
    "allow": False,
    "violated_rules": [
        {"rule": "PROHIBIT_PVC_DELETE", "message": "PVC の削除は不可逆なため許可しません"}
    ],
}
DENY_BODY = json.dumps({"result": DENY_RESULT, "decision_id": "dec-2"})


def action_of(name: str) -> ProposedAction:
    """fixture を第2層に通してから使う。第3層には検証済みのものしか渡らない。"""
    path = POLICY_FIXTURES / name if (POLICY_FIXTURES / name).is_file() else FIXTURES / name
    return validate_action(
        path.read_text(encoding="utf-8"), original_request="第3層の単体検証", trace_id="t-1"
    )


def respond_with(status: int, text: str):
    """httpx.post を差し替えるためのフェイク。OPA そのものは立てない。"""

    def fake_post(url, **kwargs):
        fake_post.calls.append((url, kwargs))
        return httpx.Response(status_code=status, text=text, request=httpx.Request("POST", url))

    fake_post.calls = []
    return fake_post


def raising(exc: Exception):
    def fake_post(url, **kwargs):
        raise exc

    return fake_post


def evaluate(fixture: str = "valid_rollout_restart.json") -> policy.PolicyDecision:
    return evaluate_action(
        action_of(fixture), trace_id="t-1", original_request="第3層の単体検証", url=URL
    )


# ── 正常系: allow と deny はどちらも「評価できた」結果である ──────────


def test_allow_is_returned_as_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(policy.httpx, "post", respond_with(200, ALLOW_BODY))
    decision = evaluate()
    assert decision.allow is True
    assert decision.violated_rules == []


def test_deny_is_a_return_value_not_an_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """deny は確定した判定であり、評価不能とは別物（design.md 3.3）。"""
    monkeypatch.setattr(policy.httpx, "post", respond_with(200, DENY_BODY))
    decision = evaluate("n01_dangerous_delete_pvc.json")
    assert decision.allow is False
    assert [v.rule for v in decision.violated_rules] == ["PROHIBIT_PVC_DELETE"]
    assert all(v.message for v in decision.violated_rules)  # 理由なき拒否を許さない


# ── Fail-Closed: 3.3 の失敗モードすべて ────────────────────────────


@pytest.mark.parametrize(
    ("failure", "post"),
    [
        ("timeout", raising(httpx.ReadTimeout("timed out"))),
        ("unreachable", raising(httpx.ConnectError("connection refused"))),
        ("http_error", respond_with(500, "internal error")),
        ("http_error", respond_with(404, "not found")),
        ("undefined", respond_with(200, "{}")),
        ("malformed_decision", respond_with(200, '{"result": {"allow": "false", "violated_rules": []}}')),
        (
            "malformed_decision",
            respond_with(
                200,
                '{"result": {"allow": true, "violated_rules": [{"rule": "R", "message": "m"}]}}',
            ),
        ),
        ("malformed_decision", respond_with(200, '{"result": {"allow": false, "violated_rules": []}}')),
        (
            "malformed_decision",
            respond_with(
                200,
                '{"result": {"allow": false, "violated_rules": [{"rule": "R", "message": "m"}],'
                ' "requires_approval_from": "sre"}}',
            ),
        ),
        ("malformed_decision", respond_with(200, "not json at all")),
    ],
)
def test_fail_closed_on_every_failure_mode(
    failure: str, post, monkeypatch: pytest.MonkeyPatch
) -> None:
    """評価できないときは allow にも deny にもならず、必ず例外になる。"""
    monkeypatch.setattr(policy.httpx, "post", post)
    with pytest.raises(PolicyUnavailableError) as excinfo:
        evaluate()
    assert excinfo.value.failure == failure
    assert excinfo.value.detail  # 人間が読む説明が必ず付く


def test_malformed_decision_keeps_validation_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """どのフィールドで落ちたかが取れること（判定基準4 の「理由が取れる」）。"""
    monkeypatch.setattr(
        policy.httpx,
        "post",
        respond_with(200, '{"result": {"allow": "false", "violated_rules": []}}'),
    )
    with pytest.raises(PolicyUnavailableError) as excinfo:
        evaluate()
    types = {(e["type"], tuple(e["loc"])) for e in excinfo.value.errors}
    assert ("bool_type", ("result", "allow")) in types


def test_undefined_is_not_treated_as_deny(monkeypatch: pytest.MonkeyPatch) -> None:
    """undefined を allow: false に潰さない（D-017 / 通知パターン3 と 4 の区別）。"""
    monkeypatch.setattr(policy.httpx, "post", respond_with(200, "{}"))
    with pytest.raises(PolicyUnavailableError) as excinfo:
        evaluate()
    assert excinfo.value.failure == "undefined"


# ── 送信するもの: input のエンベロープ（D-016）────────────────────


def test_input_envelope_carries_action_and_trace_id(monkeypatch: pytest.MonkeyPatch) -> None:
    post = respond_with(200, ALLOW_BODY)
    monkeypatch.setattr(policy.httpx, "post", post)
    action = action_of("valid_rollout_restart.json")

    evaluate_action(action, trace_id="trace-xyz", original_request="req", url=URL)

    (url, kwargs) = post.calls[0]
    assert url == URL
    sent = kwargs["json"]["input"]
    assert sent["trace_id"] == "trace-xyz"
    # 第2層の出力がそのまま input.action になる（Phase 2 判定基準5 の継承）
    assert sent["action"] == action.model_dump(mode="json")


def test_timeout_is_passed_to_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    post = respond_with(200, ALLOW_BODY)
    monkeypatch.setattr(policy.httpx, "post", post)
    evaluate_action(
        action_of("valid_rollout_restart.json"),
        trace_id="t-1",
        original_request="req",
        url=URL,
        timeout=1.5,
    )
    assert post.calls[0][1]["timeout"] == 1.5


# ── エスカレーションイベント（Phase 4 に渡す形）────────────────────


def test_denied_event_has_what_pattern3_needs() -> None:
    action = action_of("n01_dangerous_delete_pvc.json")
    decision = policy.PolicyDecision.model_validate_json(json.dumps(DENY_RESULT))
    event = policy.build_denied_event(
        decision, action=action, trace_id="trace-1", original_request="本番の PVC が枯渇"
    )

    assert event.layer == "layer3_opa"
    assert event.kind == "policy_denied"  # 通知パターン3
    assert event.trace_id == "trace-1"
    assert event.action is not None and event.action.environment is Environment.PRODUCTION
    assert [v.rule for v in event.violated_rules] == ["PROHIBIT_PVC_DELETE"]
    assert event.reason and event.original_request == "本番の PVC が枯渇"
    # Slack / ログに載せるため JSON 化できること
    json.dumps(event.model_dump(mode="json"), ensure_ascii=False)


def test_unavailable_event_has_no_violations_to_approve() -> None:
    """パターン4 は承認ボタンを出さない。違反が特定できたわけではないため。"""
    event = policy.build_unavailable_event(
        "timeout",
        "3.0 秒以内に応答がありませんでした",
        action=action_of("valid_rollout_restart.json"),
        trace_id="trace-2",
        original_request="req",
    )
    assert event.kind == "policy_unavailable"
    assert event.violated_rules == []
    assert event.action is not None  # 何をしようとしていたかは人間の状況把握に要る
    json.dumps(event.model_dump(mode="json"), ensure_ascii=False)


# ── 判定基準7: ポリシーが agents/ に漏れていないこと ────────────────


@pytest.mark.parametrize("module", ["policy.py", "main.py", "validation.py"])
def test_no_policy_literals_in_agent_code(module: str) -> None:
    """環境名・操作名による分岐が agents/ に無いこと。

    語彙そのものを定義する schemas.py と、それを提示する prompts.py は対象外
    （語彙は第2層の責務であり、判定ではない）。
    """
    source = (Path(__file__).parent.parent / "agents" / module).read_text(encoding="utf-8")
    leaked = [v.value for v in (*Environment, *Operation) if v.value in source]
    assert leaked == [], f"{module} にポリシーの語彙が漏れている: {leaked}"


# ── 第3層に渡るのは第2層を通過したものだけである ────────────────────


@pytest.mark.parametrize(
    "name", sorted(p.name for p in POLICY_FIXTURES.glob("*.json") if not p.name.startswith("n02_"))
)
def test_policy_fixtures_pass_layer2(name: str) -> None:
    """第3層用の fixture は「構造は正しいが内容が危険」でなければならない。

    ここで落ちるものがあれば、それは第2層のシナリオであって第3層のものではない。
    """
    action = action_of(name)
    assert isinstance(action, ProposedAction)
