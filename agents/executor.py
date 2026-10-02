"""実行プロセス（phase5 design.md 3.4 / D-027）。

    python -m agents.executor            # 常駐。approved を拾って実行する
    python -m agents.executor --once     # 1 周だけ（検証用）

**クラスタの資格情報を持つ唯一の常駐プロセスである。** Slack の受信（``slack/handler.py``）は
ステートを進めるところまでで終わり、ここには到達しない。
「承認ボタンを押せる = handler の権限で実行できる」という結合を切るための分離である
（phase4 result.md 提言5）。

ここが守る規律:

- **占有してから実行する。** ``approved -> executing`` の条件付き更新（CAS）が実行側の排他。
- **起動時に ``executing`` を ``unknown`` に倒す。** 実行中に落ちたプロセスの後始末であり、
  **``approved`` に戻して再実行してはならない**（それが二重実行の典型的な作り方）。
- **復元したレコードは実行の直前にもう一度第2層で検証する**（D-020）。
- **自動で再試行しない。** ``execution_failed`` からの再試行は人間がボタンを押したときだけ（D-028）。
"""

from __future__ import annotations

import argparse
import os
import time
from datetime import UTC, datetime

from pydantic_core import ValidationError

from agents import execution, k8s
from agents.log import json_safe_errors, log
from agents.schemas import EscalationEvent
from slack import notifier
from slack.blocks import build_resolved_blocks
from slack.store import HandoffStatus, PendingAction, StateConflict, Store, is_stale

LAYER = "execution"
DEFAULT_POLL_SECONDS = 2.0

# 終了コード（--once のとき。design.md 3.12 と同じ値を使う）。
EXIT_OK = 0
EXIT_EXECUTION_FAILED = 6
EXIT_EXECUTION_UNKNOWN = 7

_RECOVERY_DETAIL = (
    "実行の開始は記録されているが結果が記録されていない（プロセスが落ちた）。"
    "実行されたかどうかは判別できないため、自動では再実行しない"
)


def poll_seconds() -> float:
    raw = os.environ.get("EXECUTOR_POLL_SECONDS")
    return DEFAULT_POLL_SECONDS if not raw else float(raw)


def recover(store: Store, client: object | None = None) -> list[str]:
    """起動時の後始末。``executing`` のまま残っているものを ``unknown`` に倒す。"""
    recovered = store.recover_executing(_RECOVERY_DETAIL)
    for row in recovered:
        log(event="executor.recovered", layer=LAYER, trace_id=row.trace_id, status=row.status)
        _notify_outcome(row, execution.ExecutionOutcome("unknown", _RECOVERY_DETAIL), client)
    return [row.trace_id for row in recovered]


def run_once(
    *, store: Store | None = None, apis: k8s.Apis | None = None, client: object | None = None
) -> int:
    """実行待ちを 1 周ぶん処理する。戻り値は**もっとも重い**終了コード。

    ``apis`` / ``client`` はテストから差し替えるためのもの。既定では
    ``K8S_KUBECONFIG`` と Slack の実クライアントを使う。
    """
    store = store or Store()
    worst = EXIT_OK
    for row in store.due_for_execution():
        worst = max(worst, _process(row, store, apis, client))
    return worst


def _process(row: PendingAction, store: Store, apis: k8s.Apis | None, client: object | None = None) -> int:
    trace_id = row.trace_id

    # TOCTOU。**提案から**の経過で測る（phase4 design.md 3.5）。占有する前に見る。
    if is_stale(row):
        detail = (
            f"提案から {(datetime.now(UTC) - row.created_at).total_seconds():.0f} 秒が"
            "経過しているため実行しません"
        )
        try:
            store.mark_stale(trace_id, detail, actor=row.approved_by)
        except StateConflict as exc:
            log(event="executor.conflict", layer=LAYER, trace_id=trace_id, actual=exc.actual)
            return EXIT_OK
        log(event="executor.stale", layer=LAYER, trace_id=trace_id, detail=detail)
        _update_message(row, "⏱️ [実行中止] 提案から時間が経過しました", detail, client)
        return EXIT_OK

    try:
        # ここが実行側の排他。複数の executor が拾っても更新件数が 1 になるのは 1 つだけ。
        row = store.claim_for_execution(trace_id)
    except StateConflict as exc:
        log(event="executor.conflict", layer=LAYER, trace_id=trace_id, actual=exc.actual)
        return EXIT_OK

    try:
        # ストアも無検証で信じない。strict / extra="forbid" で再検証する（D-020）。
        event = EscalationEvent.model_validate_json(row.event_json)
        action = event.action
        if action is None:
            raise ValueError("実行対象のアクションがイベントに含まれていません")
    except (ValidationError, ValueError) as exc:
        detail = f"ストアから実行対象を復元できませんでした（{type(exc).__name__}）"
        errors = json_safe_errors(exc) if isinstance(exc, ValidationError) else []
        # API を 1 度も呼んでいない。実行されていないことが確定している。
        store.mark_execution_failed(trace_id, detail)
        store.audit(trace_id, action="restore_failed", actor=row.approved_by, detail=str(exc))
        log(event="executor.restore_failed", layer=LAYER, trace_id=trace_id, detail=detail, errors=errors)
        _update_message(row, "❌ [実行中止] 実行対象を復元できませんでした", detail, client)
        return EXIT_EXECUTION_FAILED

    outcome = execution.execute(action, trace_id, apis=apis, actor=row.approved_by)
    return _record(row, event, outcome, store, client)


def _record(
    row: PendingAction,
    event: EscalationEvent,
    outcome: execution.ExecutionOutcome,
    store: Store,
    client: object | None = None,
) -> int:
    trace_id = row.trace_id
    # 人間がポリシーの判断を上書きした事実。ここで記録しなければどこにも残らない（D-023）。
    override = "policy_override" if event.kind == "policy_denied" else "normal_approval"

    if outcome.status == "executed":
        store.mark_executed(trace_id, outcome.detail)
        store.audit(
            trace_id, action="execute", actor=row.approved_by, override_kind=override,
            detail=f"retry_count={row.retry_count} result={outcome.detail}",
        )
        log(event="executor.executed", layer=LAYER, trace_id=trace_id, actor=row.approved_by,
            override_kind=override, retry_count=row.retry_count)
        _update_message(
            row,
            f"✅ *[承認済み・実行完了]*\n*承認者:* <@{row.approved_by}>",
            f"実行結果: {outcome.detail}",
            client,
        )
        return EXIT_OK

    if outcome.status == "execution_failed":
        store.mark_execution_failed(trace_id, outcome.detail)
        store.audit(trace_id, action="execution_failed", actor=row.approved_by,
                    detail=f"http_status={outcome.http_status} {outcome.detail}")
        log(event="executor.failed", layer=LAYER, trace_id=trace_id,
            http_status=outcome.http_status, retry_count=row.retry_count)
        _notify_outcome(store.get(trace_id) or row, outcome, client)
        return EXIT_EXECUTION_FAILED

    store.mark_unknown(trace_id, outcome.detail)
    store.audit(trace_id, action="execution_unknown", actor=row.approved_by,
                detail=f"http_status={outcome.http_status} {outcome.detail}")
    log(event="executor.unknown", layer=LAYER, trace_id=trace_id, http_status=outcome.http_status,
        note="クラスタの実際の状態を人間が確認すること")
    _notify_outcome(store.get(trace_id) or row, outcome, client)
    return EXIT_EXECUTION_UNKNOWN


def _notify_outcome(
    row: PendingAction, outcome: execution.ExecutionOutcome, client: object | None = None
) -> None:
    """通知パターン5 を出す（design.md 3.9）。

    **遅延 import。** ``agents.escalation`` は送出経路を 1 本化する側であり、
    executor から見ると循環参照になるため、呼ぶ時点で読み込む。
    """
    from agents import escalation

    escalation.escalate_execution_outcome(row, outcome, client=client)


def _update_message(
    row: PendingAction, headline: str, detail: str, client: object | None = None
) -> None:
    """元メッセージを書き換えてボタンを消す（運用原則5）。

    失敗しても**ストアの状態を正とする**。UI が古いまま残っても、次の押下は CAS で弾かれる。
    """
    if not (row.channel and row.message_ts):
        return
    try:
        notifier.update(
            notifier.Delivery(channel=row.channel, ts=row.message_ts),
            text=headline,
            blocks=build_resolved_blocks(trace_id=row.trace_id, headline=headline, detail=detail),
            client=client,
        )
    except notifier.NotificationError as exc:
        log(event="slack.failed", layer=LAYER, trace_id=row.trace_id, failure=exc.failure,
            detail=exc.detail, method="chat.update", note="ステートは更新済み。UI だけが古い")


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(description="承認済みアクションの実行プロセス（Phase 5）")
    parser.add_argument("--once", action="store_true", help="1 周だけ処理して終了する")
    args = parser.parse_args()

    store = Store()
    log(event="executor.start", layer=LAYER, once=args.once,
        kubeconfig_configured=bool(os.environ.get("K8S_KUBECONFIG")),
        poll_seconds=poll_seconds(), status=HandoffStatus.APPROVED.value)
    recover(store)

    if args.once:
        return run_once(store=store)

    interval = poll_seconds()
    while True:
        run_once(store=store)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
