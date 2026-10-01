"""ハンドオフ経路の 1 本化（design.md 3.1 / D-019）。

第2層・第3層は、失敗を検知したら ``escalate()`` を呼ぶだけでよい。
**各層は通知先を知らない。** Slack を別の通知層に差し替えても、層の実装は変わらない
（architecture.md 設計原則2）。

ここが守る規律:

- **Phase 1〜3 の構造化ログを 1 バイトも変えない。** 既存の行をそのまま出したうえで、
  送出の結果を別の行（``slack.notified`` / ``slack.failed`` / ``slack.skipped``）として足す。
- **ストアへの書き込みは Slack より先**（design.md 3.5）。逆順にすると
  「通知はあるがストアに無い」＝押しても何も起きない状態が作れる。
- **承認待ちとしてストアに入るのは ``policy_denied`` だけ。**
  「ストアにエントリがある」＝「承認ボタンが存在しうる」を 1 対 1 に保つ。
- **送出の失敗を握り潰さない。** 終了コード 5 に反映できるよう台帳に記録する（D-022）。
"""

from __future__ import annotations

from datetime import datetime

from agents.log import log
from agents.schemas import EscalationEvent
from slack import notifier
from slack.blocks import build_notification
from slack.store import Store, actionable_until

# --no-slack で落とす。Phase 3 までの経路をそのまま再現するためのフラグ。
_enabled = True

# 送出に失敗した Trace ID。main.py が終了コード 5 を決めるために参照する。
# 例外型（ValidationError / PolicyUnavailableError）は Phase 2・3 の設計で確定しており、
# そこに配送結果を載せると層の責務が濁るため、台帳として外に置く。
_failed_deliveries: set[str] = set()


def set_enabled(value: bool) -> None:
    global _enabled
    _enabled = value


def delivery_failed(trace_id: str) -> bool:
    """この Trace ID のハンドオフが人間に届かなかったか。"""
    return trace_id in _failed_deliveries


def reset() -> None:
    """テスト用。台帳とフラグを初期化する。"""
    _failed_deliveries.clear()
    set_enabled(True)


def escalate(handoff: EscalationEvent, /, **log_fields: object) -> None:
    """層の失敗を人間に引き渡す。``log_fields`` は各層が従来どおり出していた行。

    第1引数を位置専用にしているのは、``log_fields`` に ``event=`` を渡せるようにするため
    （各層の既存のログ行は ``event="validation.error"`` のような形をしている）。
    """
    log(**log_fields)

    if not _enabled:
        log(event="slack.skipped", layer=handoff.layer, trace_id=handoff.trace_id, reason="--no-slack")
        return
    if not notifier.is_configured():
        # 未設定は障害ではなく構成。終了コードを変えない（design.md 3.9）。
        log(
            event="slack.skipped",
            layer=handoff.layer,
            trace_id=handoff.trace_id,
            reason="SLACK_BOT_TOKEN / SLACK_CHANNEL_ID が未設定",
        )
        return

    pending = _create_pending_if_approvable(handoff)
    store, expires_at = pending if pending else (None, None)
    # 表示する期限は、ストアが判定に使う値そのもの（別々に計算しない）。
    notification = build_notification(handoff, expires_at=expires_at)

    try:
        delivery = notifier.post(notification)
    except notifier.NotificationError as exc:
        # 遮断は成立している。失敗しているのは「人間に届けること」だけである。
        _failed_deliveries.add(handoff.trace_id)
        log(
            event="slack.failed",
            layer=handoff.layer,
            trace_id=handoff.trace_id,
            kind=handoff.kind,
            failure=exc.failure,
            detail=exc.detail,
        )
        if store is not None:
            store.mark_notify_failed(handoff.trace_id, f"{exc.failure}: {exc.detail}")
        return

    log(
        event="slack.notified",
        layer=handoff.layer,
        trace_id=handoff.trace_id,
        kind=handoff.kind,
        channel=delivery.channel,
        message_ts=delivery.ts,
        has_thread=bool(notification.thread_blocks),
    )
    if store is not None:
        store.attach_message(handoff.trace_id, channel=delivery.channel, message_ts=delivery.ts)

    if notification.thread_blocks:
        try:
            notifier.post_thread(delivery, notification.thread_blocks, text="詳細（全文）")
        except notifier.NotificationError as exc:
            # 本体は届いている。スレッドの失敗で「届かなかった」とは言わない。
            log(
                event="slack.thread_failed",
                layer=handoff.layer,
                trace_id=handoff.trace_id,
                failure=exc.failure,
                detail=exc.detail,
            )


def _create_pending_if_approvable(handoff: EscalationEvent) -> tuple[Store, datetime] | None:
    """承認されうるものだけをストアに入れる。

    ``validation_failed`` と ``policy_unavailable`` は、人間が承認して実行してよい状態ではない。
    ここでエントリを作らないことが、パターン4 に承認ボタンを出す実装ミスを
    ストアの状態にも現れさせる（design.md 2.2）。
    """
    if handoff.kind != "policy_denied":
        return None
    store = Store()
    row = store.create_pending(
        trace_id=handoff.trace_id,
        kind=handoff.kind,
        event_json=handoff.model_dump_json(),
    )
    # 表示するのは「押して実行できる期限」。承認期限だけ見せると、
    # TOCTOU で先に弾かれる場合に受け取った人を欺く。
    return store, actionable_until(row)
