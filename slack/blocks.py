"""EscalationEvent -> Block Kit の変換（design.md 3.2）。

**このモジュールは Slack SDK を import しない。** 純粋に dict を組み立てるだけにして、
ワークスペースが無くても pytest で全パターンを固められるようにする（判定基準1）。

設計上の要点:

- ``kind`` -> 通知パターンの対応を知っているのはここだけである。EscalationEvent 側に
  パターン番号を持たせない設計（phase3 design.md 3.6）を受けている。
- **承認ボタンを出す条件は ``kind == "policy_denied"`` のみ。** 環境・操作・違反の種類で
  出し分けない（D-021）。条件が 1 つなら「パターン4 にボタンが出た」事故が構造的に起きない。
- **マスキングはこのモジュールの中で必ず適用する**（``build_notification()``）。
  呼び出し側の規律に任せると、通知経路が増えたときに漏れる（design.md 3.8）。
- 値を文面に埋め込むのはよいが、値で分岐してはいけない（D-021）。
  提案アクションの表示も、操作ごとのテンプレートを持たず汎用の整形で済ませる。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast
from zoneinfo import ZoneInfo

from agents.schemas import EscalationEvent, ProposedAction
from slack.masking import mask, mask_value

# Slack Block Kit の上限（design.md 3.8。実測は未決事項2 で確認する）。
HEADER_TEXT_LIMIT = 150
SECTION_TEXT_LIMIT = 3000
FIELD_TEXT_LIMIT = 2000
BUTTON_TEXT_LIMIT = 75
BUTTON_VALUE_LIMIT = 2000
CONFIRM_TITLE_LIMIT = 100
CONFIRM_TEXT_LIMIT = 300
MAX_BLOCKS = 50
MAX_FIELDS = 10

# 本体に載せる要約の長さ。全文はスレッド返信に回す（運用原則1）。
REASON_PREVIEW = 600
RAW_OUTPUT_PREVIEW = 1200

ACTION_ID_APPROVE = "approve_action"
ACTION_ID_REJECT = "reject_action"

# 承認期限の表示タイムゾーン。PoC の運用者が日本にいる前提。
# 実運用では閲覧者のロケールに合わせるか、Slack の日付フォーマット（<!date^...>）を使う。
DISPLAY_TZ = ZoneInfo("Asia/Tokyo")


@dataclass(frozen=True)
class Notification:
    """1 回の送出で Slack に渡すものの全体。

    ``thread_blocks`` が空でなければ、本体の投稿に続けてスレッド返信を送る。
    """

    text: str  # 通知欄とフォールバック用のプレーンテキスト
    blocks: list[dict]
    thread_blocks: list[dict] = field(default_factory=list)


def truncate(text: str, limit: int) -> str:
    """上限を超えたら切り詰める。**切り詰めたことを本文に残す。**

    黙って切ると、人間は全部を見たと誤解する（design.md 3.8）。
    """
    if len(text) <= limit:
        return text
    marker = f"\n…（切り詰め: 全 {len(text)} 文字）"
    return text[: max(0, limit - len(marker))] + marker


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": truncate(text, SECTION_TEXT_LIMIT)}}


def _fields(pairs: list[tuple[str, str]]) -> dict:
    return {
        "type": "section",
        "fields": [
            {"type": "mrkdwn", "text": truncate(f"*{label}:*\n{value}", FIELD_TEXT_LIMIT)}
            for label, value in pairs[:MAX_FIELDS]
        ],
    }


def _header(text: str) -> dict:
    # header は plain_text。絵文字は :shortcode: ではなくそのまま載せる。
    return {"type": "header", "text": {"type": "plain_text", "text": truncate(text, HEADER_TEXT_LIMIT), "emoji": True}}


def _context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": truncate(text, SECTION_TEXT_LIMIT)}]}


def _code_block(body: str, limit: int) -> str:
    return f"```{truncate(body, limit)}```"


def describe_action(action: ProposedAction) -> str:
    """提案アクションを 1 行で表す。

    **操作ごとのテンプレート（``kubectl delete pvc ...`` のような文字列）を持たない。**
    理由は 2 つある:

    1. 操作の種別で分岐すると、ポリシーの語彙が通知層に漏れる（D-021）。
    2. 本 PoC は実際にシェルコマンドを発行しない。発行しないコマンド文字列を
       人間に見せると、実行されるものについて誤った印象を与える。
    """
    target = action.target
    replicas = "" if action.replicas is None else f" replicas={action.replicas}"
    return (
        f"{action.operation.value}  {target.kind}/{target.name} @ {target.namespace} "
        f"({action.environment.value}){replicas}  dry_run={str(action.dry_run).lower()}"
    )


def _action_blocks(action: ProposedAction | None) -> list[dict]:
    if action is None:
        return []
    return [
        _section(f"*提案されたアクション:*\n{_code_block(describe_action(action), SECTION_TEXT_LIMIT - 64)}"),
        _section(f"*AI の思考ログ（判断理由）:*\n>{truncate(action.reason, REASON_PREVIEW)}"),
    ]


def _approval_elements(trace_id: str, action: ProposedAction | None) -> list[dict]:
    """承認・却下ボタン。``value`` は Trace ID のみ（運用原則2 / D-020）。

    confirm は**すべての承認ボタンに一律で**付ける。操作の種別で出し分けない（D-021）。
    """
    if action is None:
        target = "対象不明"
    else:
        target = f"{action.environment.value} の {action.target.kind}/{action.target.name}"
    return [
        {
            "type": "button",
            "action_id": ACTION_ID_APPROVE,
            "text": {"type": "plain_text", "text": truncate("手動で承認して実行", BUTTON_TEXT_LIMIT), "emoji": True},
            "style": "danger",
            "value": truncate(trace_id, BUTTON_VALUE_LIMIT),
            "confirm": {
                "title": {"type": "plain_text", "text": truncate("本当に実行しますか？", CONFIRM_TITLE_LIMIT)},
                "text": {
                    "type": "mrkdwn",
                    "text": truncate(f"*{target}* に対する操作を実行します。", CONFIRM_TEXT_LIMIT),
                },
                "confirm": {"type": "plain_text", "text": "実行する"},
                "deny": {"type": "plain_text", "text": "キャンセル"},
            },
        },
        {
            "type": "button",
            "action_id": ACTION_ID_REJECT,
            "text": {"type": "plain_text", "text": truncate("提案を却下", BUTTON_TEXT_LIMIT), "emoji": True},
            "value": truncate(trace_id, BUTTON_VALUE_LIMIT),
        },
    ]


def _violation_section(event: EscalationEvent) -> list[dict]:
    """違反を**すべて**並べる。1 件に要約しない（phase3 result.md 考察6）。

    優先度付けはポリシー側に通知の都合を持ち込むことになるため採らない。
    """
    if not event.violated_rules:
        return []
    lines = "\n".join(f"• `{v.rule}`\n　{v.message}" for v in event.violated_rules)
    return [_section(f"*違反したポリシー（{len(event.violated_rules)} 件）:*\n{lines}")]


def _validation_failed(event: EscalationEvent) -> Notification:
    """通知パターン2。第2層の構造検証に失敗した。ボタンは出さない。"""
    types = "、".join(sorted({str(e.get("type", "unknown")) for e in event.errors})) or "unknown"
    blocks = [
        _header("⚠️ [WARNING] エージェント処理停止 (Schema Validation Error)"),
        _fields(
            [
                ("検知レイヤー", "`第2層 (JSON Validator)`"),
                ("Trace ID", f"`{event.trace_id}`"),
                ("エラー件数", f"`{len(event.errors)} 件`"),
                ("エラー種別", f"`{types}`"),
            ]
        ),
        _section(f"*理由:*\n{event.reason}"),
    ]
    if event.raw_output:
        blocks.append(_section(f"*LLM 生レスポンス (Raw Output):*\n{_code_block(event.raw_output, RAW_OUTPUT_PREVIEW)}"))
    blocks.append(_section(f"*元のインシデント記述:*\n>{truncate(event.original_request, REASON_PREVIEW)}"))
    blocks.append(_context("💡 出力フォーマットの定義（プロンプト）かスキーマの見直しを検討してください。"))
    return Notification(text=f"[WARNING] 第2層の構造検証に失敗しました（{event.trace_id}）", blocks=blocks)


def _deadline_field(expires_at: datetime) -> str:
    """承認期限の表示。

    **絶対時刻と残り時間の両方を出す。** 残り時間だけだと見た瞬間しか正しくなく、
    絶対時刻だけだと「あとどれくらい猶予があるか」を暗算させることになる。
    """
    remaining = int((expires_at - datetime.now(UTC)).total_seconds() // 60)
    local = expires_at.astimezone(DISPLAY_TZ).strftime("%H:%M")
    if remaining <= 0:
        return f"`{local} JST` **超過（実行できません）**"
    return f"`{local} JST`（あと約 {remaining} 分）"


def _policy_denied(event: EscalationEvent, expires_at: datetime | None = None) -> Notification:
    """通知パターン3。第3層が拒否した。**ここだけが承認ボタンを持つ。**"""
    env = event.action.environment.value if event.action else "unknown"
    # 4 つ目は承認期限を優先する。違反の件数は下の見出し（違反したポリシー（N 件））と
    # 二重になるため、**猶予という別の情報**に場所を譲る。
    deadline_field = (
        ("承認期限", _deadline_field(expires_at))
        if expires_at is not None
        else ("違反件数", f"`{len(event.violated_rules)} 件`")
    )
    blocks = [
        _header("🚨 [AIエージェント] 危険な操作を検知し自動実行をブロックしました"),
        _fields(
            [
                ("対象環境", f"`{env}`"),
                ("Trace ID", f"`{event.trace_id}`"),
                ("検知レイヤー", "`第3層 (OPA)`"),
                deadline_field,
            ]
        ),
        *_action_blocks(event.action),
        *_violation_section(event),
        {"type": "divider"},
        {"type": "actions", "block_id": "handoff_action_block", "elements": _approval_elements(event.trace_id, event.action)},
        _context(
            "実行内容は Trace ID をキーにサーバ側のストアから取得されます（ボタンの値は Trace ID のみ）。"
        ),
    ]
    return Notification(text=f"[HIGH] 第3層が実行をブロックしました（{event.trace_id}）", blocks=blocks)


def _policy_unavailable(event: EscalationEvent) -> Notification:
    """通知パターン4。評価不能。**ボタンを出さない**（Fail-Closed を UI で崩さない）。"""
    env = event.action.environment.value if event.action else "unknown"
    blocks = [
        _header("🚨 [CRITICAL] ポリシー評価不能のため処理を自動遮断しました (Fail-Closed)"),
        _fields(
            [
                ("対象環境", f"`{env}`"),
                ("Trace ID", f"`{event.trace_id}`"),
                ("判定結果", "`Fail-Closed (即時遮断)`"),
                ("検知レイヤー", "`第3層 (OPA)`"),
            ]
        ),
        _section(f"*理由:*\n{event.reason}"),
        *_action_blocks(event.action),
        _context(
            "💡 OPA の稼働とポリシーの配布状況を確認してください。"
            "**安全性が確認できていないため承認ボタンは出していません。**"
        ),
    ]
    return Notification(text=f"[CRITICAL] 第3層が評価不能です（{event.trace_id}）", blocks=blocks)


_BUILDERS = {
    "validation_failed": _validation_failed,
    "policy_denied": _policy_denied,
    "policy_unavailable": _policy_unavailable,
}


def _thread_blocks(event: EscalationEvent) -> list[dict]:
    """全文はスレッドに置く（運用原則1）。

    **Slack は保管先ではない。** 正本はストアとローカルの構造化ログであり、
    ここは人間が最初に見る場所にすぎない（design.md 3.8）。
    """
    blocks: list[dict] = []
    if event.raw_output and len(event.raw_output) > RAW_OUTPUT_PREVIEW:
        blocks.append(_section(f"*Raw Output（全文）:*\n{_code_block(event.raw_output, SECTION_TEXT_LIMIT - 64)}"))
    if event.errors:
        dumped = json.dumps(event.errors, ensure_ascii=False, indent=2)
        blocks.append(_section(f"*ValidationError の詳細:*\n{_code_block(dumped, SECTION_TEXT_LIMIT - 64)}"))
    if event.action and len(event.action.reason) > REASON_PREVIEW:
        blocks.append(_section(f"*AI の思考ログ（全文）:*\n>{truncate(event.action.reason, SECTION_TEXT_LIMIT - 8)}"))
    return blocks[:MAX_BLOCKS]


def build_notification(
    event: EscalationEvent, *, expires_at: datetime | None = None
) -> Notification:
    """EscalationEvent から送出物を組み立てる。**マスキングはここで必ず適用される。**

    呼び出し側がマスキングを忘れられない構造にしてある（design.md 3.8）。

    ``expires_at`` は承認期限（パターン3 のみ使う）。**イベントではなくストアが持つ**
    （design.md 確認事項11）ので、引数で受け取る。ここで環境変数から TTL を読むと、
    「表示した期限」と「ストアが判定に使う期限」が別々に決まって食い違いうる。
    """
    builder = _BUILDERS[event.kind]
    notification = (
        builder(event, expires_at) if event.kind == "policy_denied" else builder(event)
    )
    return Notification(
        text=mask(notification.text),
        blocks=_mask_blocks(notification.blocks)[:MAX_BLOCKS],
        thread_blocks=_mask_blocks(_thread_blocks(event)),
    )


def _mask_blocks(blocks: list[dict]) -> list[dict]:
    return cast("list[dict]", mask_value(blocks))


def build_resolved_blocks(*, trace_id: str, headline: str, detail: str) -> list[dict]:
    """押下後・失効後の表示（運用原則5）。

    **actions ブロックを含めない。** 非活性化ではなく削除を採る理由は design.md 3.10。
    """
    return [
        _section(f"{headline}\n*Trace ID:* `{trace_id}`"),
        _context(mask(detail)),
    ]
