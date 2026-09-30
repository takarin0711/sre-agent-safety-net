"""第2層 JSON Validator の入口。

第2層はエージェントと同一プロセスで動くライブラリであり、HTTP 境界を持たない
（docs/decisions.md D-010）。第1層・第3層の実装コードは import しない。

責務は「構造が仕様どおりであることの保証」だけである。以下は**やらない**:

- LLM 出力の修復（コードフェンス剥がし・前後の自然言語の除去）。design.md 3.4
- ValidationError からの再プロンプト（自動修復リトライ）。D-012
- 内容の安全性判定。第3層の責務。D-002

失敗は握り潰さず、EscalationEvent を構造化ログに出してから例外を再送出する。
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic_core import InitErrorDetails, PydanticCustomError, ValidationError

from agents.log import json_safe_errors, log
from agents.schemas import EscalationEvent, ProposedAction

LAYER = "layer2_json_validator"


def validate_action(
    content: str | None, *, original_request: str, trace_id: str
) -> ProposedAction:
    """第1層の出力文字列を検証し、型付きの ProposedAction にする。

    失敗時は EscalationEvent を構造化ログに出してから ValidationError を送出する。
    呼び出し側が捕捉すべき例外型はこの 1 つだけである（design.md 証明すること 3）。

    ``trace_id`` は全層を貫く相関 ID（Phase 3 で導入。docs/decisions.md D-016）。
    第2層はこれを判定に使わず、イベントとログに載せるだけである。
    """
    if content is None or not content.strip():
        # 「文字列ですらない / 中身が無い」は Pydantic に渡せないため、層の内部で検知する。
        # ただし例外型は揃える。ハンドオフ経路を 1 本に保つのが目的（design.md 3.5）。
        exc = _empty_content_error(content)
        _handoff(exc, raw_output=content, original_request=original_request, trace_id=trace_id)
        raise exc

    try:
        # json.loads() → model_validate() の 2 段にしない。
        # model_validate_json() は JSON 構文エラーも ValidationError に正規化するため、
        # 第2層の失敗が単一の例外型になる。
        action = ProposedAction.model_validate_json(content)
    except ValidationError as exc:
        _handoff(exc, raw_output=content, original_request=original_request, trace_id=trace_id)
        raise

    log(
        event="validation.ok",
        layer=LAYER,
        trace_id=trace_id,
        action=action.model_dump(mode="json"),  # そのまま第3層 (OPA) の input になる形
    )
    return action


def build_escalation_event(
    exc: ValidationError,
    *,
    raw_output: str | None,
    original_request: str,
    trace_id: str,
) -> EscalationEvent:
    """ValidationError を人間に引き渡せる形に構造化する。"""
    return EscalationEvent(
        layer=LAYER,
        kind="validation_failed",  # Handoff 仕様書の通知パターン2
        trace_id=trace_id,
        reason=f"第2層の構造検証に失敗しました（{exc.error_count()} 件）",
        errors=json_safe_errors(exc),
        raw_output=raw_output,
        original_request=original_request,
        occurred_at=datetime.now(UTC),
    )


def _handoff(
    exc: ValidationError,
    *,
    raw_output: str | None,
    original_request: str,
    trace_id: str,
) -> None:
    """エスカレーションイベントを生成して構造化ログに出す。例外は握り潰さない。"""
    event = build_escalation_event(
        exc, raw_output=raw_output, original_request=original_request, trace_id=trace_id
    )
    log(
        event="validation.error",
        layer=LAYER,
        trace_id=trace_id,
        error_type=type(exc).__name__,
        error_count=exc.error_count(),
        escalation=event.model_dump(mode="json"),
    )


def _empty_content_error(content: str | None) -> ValidationError:
    """content が None / 空 / 空白のみだったことを ValidationError として表現する。"""
    kind = "null" if content is None else "empty_or_whitespace"
    return ValidationError.from_exception_data(
        title=ProposedAction.__name__,
        line_errors=[
            InitErrorDetails(
                type=PydanticCustomError(
                    "empty_content",
                    "第1層の応答 content が空です（{kind}）。検証可能な文字列がありません",
                    {"kind": kind},
                ),
                loc=("content",),
                input=content,
            )
        ],
    )
