"""TTL 満了検知ジョブ（Handoff 仕様書 付帯要件 / design.md 3.11）。

    python -m slack.expire             # 期限切れを閉じ、二次エスカレーションを送る
    python -m slack.expire --dry-run   # 対象の一覧だけ出す

仕様書の注記どおり、**ストアからの能動通知ではなく走査ジョブが拾う**形にする。
PoC では cron を使わず手動実行の CLI とする（検証の再現性のため）。

**再通知を行うのは TTL 満了のときだけ。** 送出に失敗したエントリ（``notify_failed``）の
再送はしない。自動再送を入れると「最終的に届いた」が確率的に起き、
E 系で何を観測しているのか言えなくなる（D-012 と同じ構造）。
"""

from __future__ import annotations

import argparse

from dotenv import load_dotenv

from agents.log import log
from agents.schemas import EscalationEvent
from slack import notifier
from slack.blocks import Notification, build_notification, build_resolved_blocks
from slack.store import PendingAction, StateConflict, Store

LAYER = "escalation_slack"


def expire_due(*, dry_run: bool = False, store: Store | None = None) -> list[str]:
    """承認期限を過ぎた pending を閉じる。戻り値は処理した Trace ID。"""
    store = store or Store()
    due = store.due_for_expiry()
    log(event="expire.scan", layer=LAYER, due=len(due), dry_run=dry_run)
    if dry_run:
        for row in due:
            log(event="expire.candidate", layer=LAYER, trace_id=row.trace_id, expires_at=row.expires_at)
        return [row.trace_id for row in due]

    processed: list[str] = []
    for row in due:
        try:
            store.expire(row.trace_id)
        except StateConflict as exc:
            # 走査と押下が競合した。押下が勝ったのであれば、それは正しい結果である。
            log(event="expire.conflict", layer=LAYER, trace_id=row.trace_id, actual=exc.actual)
            continue
        store.audit(row.trace_id, action="expire", detail=f"expires_at={row.expires_at.isoformat()}")
        log(event="expire.closed", layer=LAYER, trace_id=row.trace_id)
        _close_message(row)
        _escalate_again(row)
        processed.append(row.trace_id)
    return processed


def _close_message(row: PendingAction) -> None:
    """元メッセージを［期限切れ・未実行］に書き換える（運用原則5）。"""
    if not (row.channel and row.message_ts):
        # 送出に失敗していたエントリは更新先が無い。ここで新規投稿はしない（上記の方針）。
        log(event="expire.no_message", layer=LAYER, trace_id=row.trace_id, status=row.status)
        return
    headline = "⏱️ *[期限切れ・未実行]* 承認されないまま期限を超過しました"
    try:
        notifier.update(
            notifier.Delivery(channel=row.channel, ts=row.message_ts),
            text=headline,
            blocks=build_resolved_blocks(
                trace_id=row.trace_id,
                headline=headline,
                detail=f"承認期限: {row.expires_at.isoformat()}",
            ),
        )
    except notifier.NotificationError as exc:
        log(event="slack.failed", layer=LAYER, trace_id=row.trace_id, failure=exc.failure, detail=exc.detail)


def _escalate_again(row: PendingAction) -> None:
    """二次エスカレーション。期限切れを**新しいメッセージとして**出し直す。

    元メッセージの更新だけだと、チャンネルを遡らない限り誰も気づかない。
    実運用ではエスカレーション先を昇格させる（提言）。
    """
    try:
        event = EscalationEvent.model_validate_json(row.event_json)
    except Exception as exc:  # ストアが壊れていても、期限切れの事実は伝える
        log(event="expire.restore_failed", layer=LAYER, trace_id=row.trace_id, error=str(exc))
        return
    notification = build_notification(event)
    text = f"⏱️ [再通知] 承認期限を超過しました（{row.trace_id}）"
    try:
        # 承認ボタンは出さない。期限切れのエントリは CAS で承認できない状態にある。
        notifier.post(
            Notification(
                text=text,
                blocks=[b for b in notification.blocks if b.get("type") != "actions"],
            )
        )
    except notifier.NotificationError as exc:
        log(event="slack.failed", layer=LAYER, trace_id=row.trace_id, failure=exc.failure, detail=exc.detail)


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="承認期限の満了検知ジョブ")
    parser.add_argument("--dry-run", action="store_true", help="対象の一覧だけ出して何もしない")
    args = parser.parse_args()
    processed = expire_due(dry_run=args.dry_run)
    log(event="expire.finished", layer=LAYER, processed=len(processed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
