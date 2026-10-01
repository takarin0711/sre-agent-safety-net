"""Slack への送出（design.md 2.1 の「送出」側）。

エージェントと同一プロセスのライブラリとして呼ばれる（D-019）。理由は、
**送出の失敗をそのインシデント処理の結果に反映させる必要がある**ため。
投げっぱなしにすると「遮断はしたが人間に届いていない」を終了コードに出せない（D-022）。

**失敗を握り潰さない。** すべて ``NotificationError`` に正規化して送出側へ返す。
自動再送はしない（D-012 と同じ理由。「最終的に届いた」が確率的に起きると検知の決定性が測れない）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from slack.blocks import Notification

DEFAULT_TIMEOUT_SECONDS = 10.0

# ユーザ ID -> DM のチャンネル ID。プロセス内でのみ持つ（毎回 conversations.open を叩かない）。
_DM_CACHE: dict[str, str] = {}


class NotificationError(RuntimeError):
    """Slack へ届かなかった。遮断そのものは成立している（D-022）。

    ``failure`` は ``api_error``（Slack が ok:false を返した）/ ``transport``（到達不能・
    タイムアウト・応答が Slack の形でない）の 2 種。
    """

    def __init__(self, failure: str, detail: str) -> None:
        super().__init__(f"{failure}: {detail}")
        self.failure = failure
        self.detail = detail


@dataclass(frozen=True)
class Delivery:
    """送出できたメッセージの位置。``chat.update`` の宛先になる。"""

    channel: str
    ts: str


def is_configured() -> bool:
    """送出先が設定されているか。

    未設定は**障害ではなく構成**である。Phase 1〜3 の経路をそのまま再現できる必要があるため、
    終了コードを変えない（design.md 3.9）。
    """
    return bool(os.environ.get("SLACK_BOT_TOKEN") and os.environ.get("SLACK_CHANNEL_ID"))


def build_client() -> WebClient:
    """WebClient を作る。

    ``SLACK_API_BASE_URL`` を向け替えると障害注入ができる（design.md 3.12）。
    ``mocks/fault_injector.py`` は改修不要で、既存の timeout / server_error モードを流用できる。
    """
    kwargs: dict[str, object] = {
        "token": os.environ.get("SLACK_BOT_TOKEN"),
        "timeout": _timeout_from_env(),
    }
    base_url = os.environ.get("SLACK_API_BASE_URL")
    if base_url:
        kwargs["base_url"] = base_url
    return WebClient(**kwargs)  # type: ignore[arg-type]


def channel_id(client: WebClient | None = None) -> str:
    """送出先を決める。

    ``SLACK_CHANNEL_ID`` にユーザ ID（``U...`` / ``W...``）を書くと、**Bot との DM** に送る。
    検証用のチャンネルを作らずに試せるようにするため。``conversations.open`` で
    DM のチャンネル ID（``D...``）に解決する（``im:write`` スコープが要る）。

    解決に失敗したら例外にする。``U...`` のまま投げると、宛先が存在しないのか
    スコープが足りないのかが後から分からなくなる。
    """
    channel = os.environ.get("SLACK_CHANNEL_ID")
    if not channel:
        raise NotificationError("transport", "SLACK_CHANNEL_ID が設定されていません")
    if not channel.startswith(("U", "W")):
        return channel

    cached = _DM_CACHE.get(channel)
    if cached:
        return cached
    api = client or build_client()
    response = _call(api.conversations_open, users=channel)
    resolved = (response.get("channel") or {}).get("id")
    if not resolved:
        raise NotificationError("api_error", f"{channel} との DM を開けませんでした")
    _DM_CACHE[channel] = str(resolved)
    return str(resolved)


def post(notification: Notification, *, client: WebClient | None = None) -> Delivery:
    """本体を投稿する。"""
    api = client or build_client()
    channel = channel_id(api)
    response = _call(
        api.chat_postMessage,
        channel=channel,
        text=notification.text,
        blocks=notification.blocks,
    )
    ts = response.get("ts")
    if not ts:
        # 200 でも ts が無ければ「届いた」とは言えない。ok:false と同じ扱いにする。
        raise NotificationError("api_error", "応答に ts がありません（送出の成否が確認できない）")
    return Delivery(channel=str(response.get("channel") or channel), ts=str(ts))


def post_thread(delivery: Delivery, blocks: list[dict], *, text: str, client: WebClient | None = None) -> None:
    """全文をスレッド返信に置く（運用原則1）。"""
    api = client or build_client()
    _call(
        api.chat_postMessage,
        channel=delivery.channel,
        thread_ts=delivery.ts,
        text=text,
        blocks=blocks,
    )


def update(delivery: Delivery, *, text: str, blocks: list[dict], client: WebClient | None = None) -> None:
    """メッセージを書き換える（運用原則5）。ボタンは消す（非活性化ではない）。"""
    api = client or build_client()
    _call(api.chat_update, channel=delivery.channel, ts=delivery.ts, text=text, blocks=blocks)


def post_ephemeral(*, channel: str, user: str, text: str, client: WebClient | None = None) -> None:
    """本人にだけ見えるメッセージ（RBAC 拒否・競合の通知）。"""
    api = client or build_client()
    _call(api.chat_postEphemeral, channel=channel, user=user, text=text)


def _call(method, **kwargs):  # type: ignore[no-untyped-def]
    """Slack API 呼び出しを 1 本に絞り、失敗を NotificationError に正規化する。

    **ok:false は HTTP 200 で返る**（第3層の undefined と同じ形の罠）。
    slack_sdk は ok:false を SlackApiError にしてくれるため、ここで握り潰さないことが重要。
    """
    try:
        return method(**kwargs)
    except SlackApiError as exc:
        error = exc.response.get("error") if exc.response is not None else None
        raise NotificationError("api_error", f"{error or exc}") from exc
    except Exception as exc:  # 到達不能・タイムアウト・応答が Slack の形でない
        raise NotificationError("transport", f"{type(exc).__name__}: {exc}") from exc


def _timeout_from_env() -> float:
    raw = os.environ.get("SLACK_TIMEOUT_SECONDS")
    # 設定ミスを黙って既定値に落とさない。初期値 10.0 は L-01 で実測して確定する。
    return DEFAULT_TIMEOUT_SECONDS if not raw else float(raw)
