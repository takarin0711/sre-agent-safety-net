"""第3層 OPA の入口。

第3層はエージェントとは別プロセス・別コンテナであり、HTTP 越しに問い合わせる
（docs/decisions.md D-010 の非対称性）。OPA の実装コードは import しない。

責務は「このアクションを実行してよいかの判定を受け取ること」だけである。以下は**やらない**:

- 判定そのもの。何が禁止かは opa/policy.rego の中だけに存在する（D-002）。
  このファイルに environment / operation による分岐を書いてはならない（判定基準7）。
- 評価不能時のリトライ。Fail-Closed で人間に渡す（D-015）。
- 拒否されたアクションの再提案。deny は確定的な結果であり、次に動くのは人間である（D-012 と同じ思想）。

**deny と「評価不能」を区別する。** 拒否は戻り値（``PolicyDecision(allow=False, ...)``）、
評価不能は例外（``PolicyUnavailableError``）で表す。両方を同じ形にすると、
Handoff 仕様書の通知パターン3（承認ボタンつき）とパターン4（ボタンなし）を
呼び出し側が取り違える。遮断されたという結果は同じでも、人間に見せるものと次の一手が違う。
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime

import httpx
from pydantic_core import ValidationError

from agents.escalation import escalate
from agents.log import json_safe_errors, log
from agents.schemas import EscalationEvent, OpaResponse, PolicyDecision, ProposedAction

LAYER = "layer3_opa"

# 既定値。実際の値は .env で与える（エージェントが知ってよいのは接続設定だけであり、
# 「何が禁止か」は知らない。design.md 2.3）。
DEFAULT_OPA_URL = "http://localhost:8181/v1/data/sre/agent/decision"
# Phase 2 の教訓により、タイムアウトは実測して決める（L-01 で確定する）。
# 初期値は Handoff 仕様書の記載に合わせた 3.0 秒。
DEFAULT_TIMEOUT_SECONDS = 3.0


class PolicyUnavailableError(RuntimeError):
    """第3層が評価できなかった。allow ではないし、deny でもない。

    ``failure`` は 3.3 の失敗モード:
    ``unreachable`` / ``timeout`` / ``http_error`` / ``undefined`` / ``malformed_decision``
    """

    def __init__(self, failure: str, detail: str, *, errors: list[dict] | None = None) -> None:
        super().__init__(f"{failure}: {detail}")
        self.failure = failure
        self.detail = detail
        self.errors = errors or []


def evaluate_action(
    action: ProposedAction,
    *,
    trace_id: str,
    original_request: str,
    url: str | None = None,
    timeout: float | None = None,
) -> PolicyDecision:
    """第3層に実行可否を問い合わせる。第3層の唯一の入口。

    戻り値は ``allow`` の真偽に関わらず ``PolicyDecision``。評価できなかった場合だけ
    ``PolicyUnavailableError`` を送出する（Fail-Closed）。
    """
    url = url or os.environ.get("OPA_URL") or DEFAULT_OPA_URL
    timeout = timeout if timeout is not None else _timeout_from_env()

    # OPA へ渡す input。判定材料は action だけで、trace_id は監査の突合キーとしてのみ
    # 存在する（D-016）。Rego 側はこれを参照しない。
    payload = {"action": action.model_dump(mode="json"), "trace_id": trace_id}

    started = time.monotonic()
    try:
        response = httpx.post(url, json={"input": payload}, timeout=timeout)
    except httpx.TimeoutException as exc:
        raise _unavailable(
            "timeout",
            f"{timeout} 秒以内に応答がありませんでした（{type(exc).__name__}）",
            action=action,
            trace_id=trace_id,
            original_request=original_request,
            url=url,
        ) from exc
    except httpx.RequestError as exc:
        raise _unavailable(
            "unreachable",
            f"OPA に接続できませんでした（{type(exc).__name__}: {exc}）",
            action=action,
            trace_id=trace_id,
            original_request=original_request,
            url=url,
        ) from exc

    elapsed = round(time.monotonic() - started, 4)

    if response.status_code != 200:
        raise _unavailable(
            "http_error",
            f"HTTP {response.status_code}: {response.text[:200]}",
            action=action,
            trace_id=trace_id,
            original_request=original_request,
            url=url,
            elapsed_sec=elapsed,
        )

    try:
        # response.json() → model_validate(dict) の 2 段にしない。Phase 2 の知見
        # （strict は JSON 入力と Python 入力で挙動が違う）をそのまま適用する。
        parsed = OpaResponse.model_validate_json(response.text)
    except ValidationError as exc:
        raise _unavailable(
            "malformed_decision",
            f"OPA の応答が decision の仕様と違います（{exc.error_count()} 件）",
            action=action,
            trace_id=trace_id,
            original_request=original_request,
            url=url,
            elapsed_sec=elapsed,
            errors=json_safe_errors(exc),
        ) from exc

    if parsed.result is None:
        # ポリシーが読み込まれていない / パスが違う。allow: false に潰さない（D-017）。
        raise _unavailable(
            "undefined",
            "OPA は 200 を返しましたが result がありません（ポリシー未ロードまたはパス誤り）",
            action=action,
            trace_id=trace_id,
            original_request=original_request,
            url=url,
            elapsed_sec=elapsed,
        )

    decision = parsed.result
    if decision.allow:
        log(
            event="policy.allow",
            layer=LAYER,
            trace_id=trace_id,
            elapsed_sec=elapsed,
            decision_id=parsed.decision_id,
            decision=decision.model_dump(mode="json"),
        )
        return decision

    event = build_denied_event(
        decision, action=action, trace_id=trace_id, original_request=original_request
    )
    escalate(
        event,
        event="policy.deny",
        layer=LAYER,
        trace_id=trace_id,
        elapsed_sec=elapsed,
        decision_id=parsed.decision_id,
        violated_rules=[v.rule for v in decision.violated_rules],
        escalation=event.model_dump(mode="json"),
    )
    return decision


def build_denied_event(
    decision: PolicyDecision,
    *,
    action: ProposedAction,
    trace_id: str,
    original_request: str,
) -> EscalationEvent:
    """`allow: false` を人間に引き渡せる形に構造化する（通知パターン3 の材料）。"""
    rules = "、".join(v.rule for v in decision.violated_rules)
    return EscalationEvent(
        layer=LAYER,
        kind="policy_denied",
        trace_id=trace_id,
        reason=f"第3層が実行を拒否しました（違反: {rules}）",
        action=action,
        violated_rules=list(decision.violated_rules),
        original_request=original_request,
        occurred_at=datetime.now(UTC),
    )


def build_unavailable_event(
    failure: str,
    detail: str,
    *,
    action: ProposedAction,
    trace_id: str,
    original_request: str,
    errors: list[dict] | None = None,
) -> EscalationEvent:
    """評価不能を人間に引き渡せる形に構造化する（通知パターン4 の材料）。"""
    return EscalationEvent(
        layer=LAYER,
        kind="policy_unavailable",
        trace_id=trace_id,
        reason=f"第3層が評価できないため実行を中止しました（{failure}）: {detail}",
        # 何を実行しようとしていたかは人間の状況把握に要る（運用原則1）。
        # ただし承認ボタンは出さない。安全性が確認できていないため（Fail-Closed）。
        action=action,
        errors=errors or [],
        original_request=original_request,
        occurred_at=datetime.now(UTC),
    )


def _unavailable(
    failure: str,
    detail: str,
    *,
    action: ProposedAction,
    trace_id: str,
    original_request: str,
    url: str,
    elapsed_sec: float | None = None,
    errors: list[dict] | None = None,
) -> PolicyUnavailableError:
    """エスカレーションイベントを構造化ログに出し、送出すべき例外を返す。

    例外は握り潰さない。呼び出し側は捕捉して終了コード 4 で終わる。
    """
    event = build_unavailable_event(
        failure,
        detail,
        action=action,
        trace_id=trace_id,
        original_request=original_request,
        errors=errors,
    )
    escalate(
        event,
        event="policy.unavailable",
        layer=LAYER,
        trace_id=trace_id,
        failure=failure,
        detail=detail,
        url=url,
        elapsed_sec=elapsed_sec,
        escalation=event.model_dump(mode="json"),
    )
    return PolicyUnavailableError(failure, detail, errors=errors)


def _timeout_from_env() -> float:
    raw = os.environ.get("OPA_TIMEOUT_SECONDS")
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    # 設定ミスを黙って既定値に落とさない。例外は握り潰さないのが本 PoC の規律。
    return float(raw)
