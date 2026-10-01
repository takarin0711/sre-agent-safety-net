"""Slack Interactivity の受信（design.md 2.1 の「受信」側）。

**独立した常駐プロセス**である。承認は数分〜数時間後に来るため、
エージェントのプロセスは既に終了している（Handoff 仕様書「App は待機せず解放」）。

    python -m slack.handler        # 常駐。ngrok 経由で Slack から叩かれる

ここが守る規律:

- **署名検証を Bolt に委ねる**（運用原則2）。これを欠くと RBAC がペイロード偽造で無意味化する。
- **ボタンの ``value`` は Trace ID としてのみ使う。** 実行内容はストアから復元する（D-020）。
- **復元したレコードを strict に再検証してから実行層に渡す**（D-020）。
  ストアはこの PoC で初めて登場する可変の永続状態であり、無検証で信じない。
- **排他はストアの条件付き更新に任せる**（D-024）。二重押下も Slack の再送も同じ経路で弾く。
- **``policy_override`` を監査に残す。** OPA の Decision Log には残らないため、
  ここで記録しなければどこにも残らない（phase3 result.md 提言7）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from pydantic_core import ValidationError
from slack_bolt import App
from slack_bolt.middleware import RequestVerification
from slack_bolt.response import BoltResponse
from slack_sdk import WebClient

from agents.execution import execute_dry_run
from agents.log import json_safe_errors, log
from agents.schemas import EscalationEvent
from slack import notifier
from slack.blocks import ACTION_ID_APPROVE, ACTION_ID_REJECT, build_resolved_blocks
from slack.store import HandoffStatus, StateConflict, Store, is_stale

LAYER = "escalation_slack"

# 承認系とみなす action_id。RBAC はここだけを対象にする。
PRIVILEGED_ACTIONS = (ACTION_ID_APPROVE,)


@dataclass(frozen=True)
class Outcome:
    """1 回の押下の結果。テストから直接検証できるよう戻り値にする。"""

    result: str  # executed / rejected / conflict / stale / restore_failed / unauthorized
    detail: str = ""


def approvers() -> set[str]:
    raw = os.environ.get("SLACK_APPROVER_USER_IDS", "")
    return {value.strip() for value in raw.split(",") if value.strip()}


def is_approver(user_id: str) -> bool:
    """承認者リストに含まれるかの一律判定。

    **操作の種別や環境で出し分けない**（D-021）。出し分けが必要なら、その危険度は
    第3層が ``violated_rules`` として既に返しているのだから、Rego 側に承認要件を持たせるのが筋。

    リストが未設定のときは**誰も承認できない**。本 PoC の Fail-Closed の態度を
    認可にも適用する（未設定が「全員許可」になるほうが事故として重い）。
    """
    return user_id in approvers()


def approve(
    trace_id: str,
    *,
    actor: str,
    channel: str,
    message_ts: str,
    client: WebClient | None = None,
    store: Store | None = None,
) -> Outcome:
    """承認して実行する。Bolt から切り離して呼べるようにしてある（テスト用）。"""
    store = store or Store()

    try:
        # ここが排他の実体。二重押下も Slack の再送も、更新件数 1 でなければ弾かれる。
        row = store.approve(trace_id, actor=actor)
    except StateConflict as exc:
        store.audit(trace_id, action="lock_conflict", actor=actor, detail=f"actual={exc.actual}")
        _log("handoff.conflict", trace_id=trace_id, actor=actor, actual=exc.actual)
        _ephemeral(channel, actor, "⚠️ この操作はすでに処理済み、却下済み、または期限切れです。", client)
        return Outcome("conflict", f"actual={exc.actual}")

    # TOCTOU。**提案から**の経過時間で測る（design.md 3.5）。
    if is_stale(row):
        elapsed = (row.approved_at or row.created_at) - row.created_at
        detail = f"提案から {elapsed.total_seconds():.0f} 秒が経過しているため実行しません"
        store.mark_stale(trace_id, detail, actor=actor)
        _log("handoff.stale", trace_id=trace_id, actor=actor, detail=detail)
        _update(channel, message_ts, "⏱️ [実行中止] 提案から時間が経過しました", trace_id, detail, client)
        return Outcome("stale", detail)

    try:
        # ストアも無検証で信じない。strict / extra="forbid" で再検証する（D-020）。
        event = EscalationEvent.model_validate_json(row.event_json)
        action = event.action
        if action is None:
            raise ValueError("承認対象のアクションがイベントに含まれていません")
    except (ValidationError, ValueError) as exc:
        detail = f"ストアから承認対象を復元できませんでした（{type(exc).__name__}）"
        errors = json_safe_errors(exc) if isinstance(exc, ValidationError) else []
        store.mark_execution_failed(trace_id, detail)
        store.audit(trace_id, action="restore_failed", actor=actor, detail=str(exc))
        _log("handoff.restore_failed", trace_id=trace_id, actor=actor, detail=detail, errors=errors)
        _update(channel, message_ts, "❌ [実行中止] 承認対象を復元できませんでした", trace_id, detail, client)
        return Outcome("restore_failed", detail)

    # 実行層。allow: true の経路とまったく同じ関数を通す（design.md 3.1）。
    execute_dry_run(action, trace_id)
    store.mark_executed(trace_id, "dry_run")
    # 人間がポリシーの判断を上書きした事実。ここで記録しなければどこにも残らない（D-023）。
    store.audit(
        trace_id,
        action="execute",
        actor=actor,
        override_kind="policy_override",
        detail=f"violated_rules={[v.rule for v in event.violated_rules]}",
    )
    _log("handoff.executed", trace_id=trace_id, actor=actor, override_kind="policy_override")
    _update(
        channel,
        message_ts,
        f"✅ *[承認済み・実行完了]*\n*承認者:* <@{actor}>",
        trace_id,
        "ポリシー違反を人間が承認して実行しました（policy_override として監査に記録）",
        client,
    )
    return Outcome("executed")


def reject(
    trace_id: str,
    *,
    actor: str,
    channel: str,
    message_ts: str,
    client: WebClient | None = None,
    store: Store | None = None,
) -> Outcome:
    """却下する。**UI の更新だけでは pending が残り続ける**ので、必ずステートを閉じる。"""
    store = store or Store()
    try:
        store.reject(trace_id, actor=actor)
    except StateConflict as exc:
        store.audit(trace_id, action="lock_conflict", actor=actor, detail=f"actual={exc.actual}")
        _log("handoff.conflict", trace_id=trace_id, actor=actor, actual=exc.actual)
        _ephemeral(channel, actor, "⚠️ この操作はすでに処理済みです。", client)
        return Outcome("conflict", f"actual={exc.actual}")

    store.audit(trace_id, action="reject", actor=actor, override_kind="normal_approval")
    _log("handoff.rejected", trace_id=trace_id, actor=actor)
    _update(
        channel,
        message_ts,
        f"🚫 *[却下済み]*\n*却下者:* <@{actor}>",
        trace_id,
        "提案は実行されませんでした",
        client,
    )
    return Outcome("rejected")


def build_app(
    *,
    client: WebClient | None = None,
    token_verification_enabled: bool = True,
    signature_verification: bool = True,
) -> App:
    """Bolt アプリを組み立てる。

    ``process_before_response=True`` にしているのは、**リスナーの完了まで応答を返さない**
    ためである。本 PoC の処理は数ミリ秒で終わるので 3 秒ルールに余裕があり、
    かつ「押した結果がストアに書かれてから応答が返る」ので検証が決定的になる（D-024）。
    実行が実 API 呼び出しになったらこの前提は崩れる。

    **Slack API の呼び出しは Bolt が注入する client ではなく notifier に通す。**
    Bolt はリクエストごとに新しい WebClient を組み立てる（app.py の client_per_request）ため、
    ``SLACK_API_BASE_URL`` による障害注入（E 系）が効かず、テストからも差し替えられない。
    送出の経路を 1 本に保つ意味でも、Slack に触るのは notifier だけにする。

    ``token_verification_enabled=True`` のとき、Bolt は起動時に **1 回だけ** auth.test を行い
    結果を使い回す。False にすると**リクエストごとに** auth.test が飛ぶため、
    3 秒ルールの観点でも既定の True が望ましい。

    ``signature_verification=False`` は **G-05（防御を外すと何が起きるかの対比）専用**。
    既定の経路からこのフラグに触れられる手段は用意しない。
    """
    api_client = client  # Bolt の per-request client ではなく、これを notifier に渡す
    app = App(
        token=os.environ.get("SLACK_BOT_TOKEN"),
        signing_secret=os.environ.get("SLACK_SIGNING_SECRET"),
        client=client,
        token_verification_enabled=token_verification_enabled,
        process_before_response=True,
    )
    if not signature_verification:
        # Bolt の内部リストから検証ミドルウェアを剥がす。検証用であることを明示するため
        # 公開 API ではなくここで直接触っている。
        app._middleware_list = [
            m for m in app._middleware_list if not isinstance(m, RequestVerification)
        ]

    @app.middleware
    def rbac(body, next, request):  # type: ignore[no-untyped-def]
        """RBAC（付帯要件）。承認系アクションだけ承認者リストを要求する。

        **block_actions（``body["actions"]`` を持つイベント）だけを対象にしている。**
        Modal（``view_submission``）やスラッシュコマンドを足すと、この検証を素通りする。
        本 PoC は Modal を扱わない（D-021）が、足すときは**先にここの分岐を広げること**。
        """
        actions = body.get("actions")
        if not actions:
            return next()
        action_id = actions[0].get("action_id", "")
        if action_id not in PRIVILEGED_ACTIONS:
            return next()

        user_id = body["user"]["id"]
        if is_approver(user_id):
            return next()

        trace_id = actions[0].get("value", "")
        # 押せてしまったこと自体も記録に残す（押下は Slack 側では止められない）。
        Store().audit(trace_id, action="denied_by_rbac", actor=user_id, detail=action_id)
        _log("handoff.unauthorized", trace_id=trace_id, actor=user_id, action_id=action_id)
        _ephemeral(
            body["channel"]["id"],
            user_id,
            "⚠️ この操作を承認する権限がありません。SRE チームのメンバーへ依頼してください。",
            api_client,
        )
        # next() を呼ばない = ハンドラは実行されない。
        # **ただし 200 を返す。** レスポンスを返さずに終えると Bolt は 404 を返し、
        # Slack からは失敗に見えて再送の対象になる（D-024 の 3 秒ルール）。
        # 拒否は正常な処理結果であって、通信の失敗ではない。
        # 併せて、Bolt がリクエスト本文を警告ログに出すのも止まる
        # （本文には Slack の verification token が含まれる）。
        return BoltResponse(status=200, body="")

    @app.action(ACTION_ID_APPROVE)
    def on_approve(ack, body, request):  # type: ignore[no-untyped-def]
        ack()  # 3 秒ルール対策。ストアに触る前に応答を返す（design.md 3.14）
        _log_retry(request, body)
        approve(
            body["actions"][0]["value"],
            actor=body["user"]["id"],
            channel=body["channel"]["id"],
            message_ts=body["message"]["ts"],
            client=api_client,
        )

    @app.action(ACTION_ID_REJECT)
    def on_reject(ack, body, request):  # type: ignore[no-untyped-def]
        ack()
        _log_retry(request, body)
        reject(
            body["actions"][0]["value"],
            actor=body["user"]["id"],
            channel=body["channel"]["id"],
            message_ts=body["message"]["ts"],
            client=api_client,
        )

    return app


def _log_retry(request, body) -> None:  # type: ignore[no-untyped-def]
    """Slack の再送を**観測する**。判定には使わない（D-024）。

    ヘッダの有無で弾くと、ヘッダの無い二重押下が素通りする。冪等性はストアが担保する。
    """
    retry_num = None
    try:
        # Bolt のヘッダは dict[str, list[str]]。そのまま出すと ["1"] になって読みにくい。
        raw = request.headers.get("x-slack-retry-num")
        retry_num = raw[0] if isinstance(raw, list) and raw else raw
    except Exception:  # pragma: no cover - ヘッダが無い形の呼び出し
        retry_num = None
    _log(
        "handoff.received",
        trace_id=body["actions"][0].get("value"),
        actor=body["user"]["id"],
        action_id=body["actions"][0].get("action_id"),
        slack_retry_num=retry_num,
    )


def _log(event: str, **fields: object) -> None:
    log(event=event, layer=LAYER, **fields)


def _ephemeral(channel: str, user: str, text: str, client: WebClient | None) -> None:
    """本人にだけ見えるメッセージ。失敗しても処理は止めない（が、握り潰さず記録する）。"""
    try:
        notifier.post_ephemeral(channel=channel, user=user, text=text, client=client)
    except notifier.NotificationError as exc:
        _log("slack.failed", failure=exc.failure, detail=exc.detail, method="chat.postEphemeral")


def _update(
    channel: str, message_ts: str, headline: str, trace_id: str, detail: str, client: WebClient | None
) -> None:
    """メッセージを書き換えてボタンを消す（運用原則5）。

    失敗しても**ストアの状態を正とする**。UI は古いまま残るが、次の押下は CAS で弾かれる
    ので二重実行にはならない（design.md 3.10）。
    """
    try:
        notifier.update(
            notifier.Delivery(channel=channel, ts=message_ts),
            text=headline,
            blocks=build_resolved_blocks(trace_id=trace_id, headline=headline, detail=detail),
            client=client,
        )
    except notifier.NotificationError as exc:
        _log(
            "slack.failed",
            trace_id=trace_id,
            failure=exc.failure,
            detail=exc.detail,
            method="chat.update",
            note="ステートは更新済み。UI だけが古い",
        )


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv()
    Store()  # スキーマを先に作っておく（最初の押下で初期化が走らないように）
    port = int(os.environ.get("SLACK_HANDLER_PORT", "3000"))
    _log("handler.start", port=port, approvers=len(approvers()), status=HandoffStatus.PENDING.value)
    build_app().start(port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
