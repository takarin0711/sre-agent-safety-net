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
# Phase 5。実行層の失敗に対する操作（通知パターン5）。
ACTION_ID_RETRY = "retry_action"
ACTION_ID_ABANDON = "abandon_action"

# 承認期限の表示タイムゾーン。PoC の運用者が日本にいる前提。
# 実運用では閲覧者のロケールに合わせるか、Slack の日付フォーマット（<!date^...>）を使う。
DISPLAY_TZ = ZoneInfo("Asia/Tokyo")


@dataclass(frozen=True)
class NotificationContext:
    """**ストアが持っている事実**のうち、表示とボタンの有無に要るものだけ。

    blocks.py は環境変数も TTL も再試行上限も読まない。読むと「表示した値」と
    「ストアが判定に使う値」が別々に決まって食い違う（phase4 考察2 で実機で踏んだ）。
    """

    expires_at: datetime | None = None
    # 再試行ボタンを出してよいか。判定は store.can_retry() が持つ（design.md 3.6）。
    retryable: bool = False
    retry_count: int = 0


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
    理由は 3 つある:

    1. 操作の種別で分岐すると、ポリシーの語彙が通知層に漏れる（D-021）。
    2. 発行しないコマンド文字列を人間に見せると、実行されるものについて誤った印象を与える。
    3. **Phase 5 以降、実行層はコマンドを組み立てていない**（D-030）。実際に飛ぶのは
       Kubernetes API の呼び出しであり、``kubectl`` の行はどこにも存在しない。
       表示すれば実行の近似ですらなく、通知層が創作した文字列になる。

    ただしこの整形は**可読性を捨てている**。取り戻すなら、通知層にテンプレートを置くのではなく
    **実行層側に描画関数を 1 つ置いて、ここはそれを呼ぶだけにする**
    （phase5 result.md 提言9）。実行を組み立てる場所が描画も持てば、表示と実行がずれない。
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


def _request_failed(event: EscalationEvent) -> Notification:
    """通知パターン1 の**代替**。第1層の呼び出しが失敗し、処理が始まらなかった。

    **仕様書のパターン1（インフラ全滅アラート）ではない**（D-031）。
    文面に書いてよいのは「このインシデントが処理されなかった」ことだけで、
    基盤がどう落ちているか・どのリージョンが生きているかは書かない。
    エージェントはそれを知らず、書けば**持っていない情報を創作する**ことになる。

    ボタンは出さない。提案そのものが存在しないので、承認できるものが無い。
    """
    detail = "、".join(str(e.get("error_type", "unknown")) for e in event.errors) or "unknown"
    blocks = [
        _header("🚨 [CRITICAL] インシデントが処理されませんでした (第1層に到達できません)"),
        _fields(
            [
                ("検知レイヤー", "`第1層 (LiteLLM Proxy)`"),
                ("Trace ID", f"`{event.trace_id}`"),
                ("失敗種別", f"`{detail}`"),
                ("処理の状態", "`未着手 (提案は生成されていません)`"),
            ]
        ),
        _section(f"*理由:*\n{event.reason}"),
        _section(f"*元のインシデント記述:*\n>{truncate(event.original_request, REASON_PREVIEW)}"),
        _context(
            "💡 *これは基盤障害そのもののアラートではありません。* "
            "エージェントが知っているのは「この 1 件が処理できなかった」ことだけで、"
            "どのデプロイがどう落ちたかは第1層の内側にあります（**集約もされません**。"
            "障害中にインシデントが複数走れば、その件数だけこの通知が出ます）。"
            "基盤の状態は LiteLLM 側のアラートとログで確認してください。"
        ),
    ]
    return Notification(
        text=f"[CRITICAL] 第1層に到達できずインシデントが処理されませんでした（{event.trace_id}）",
        blocks=blocks,
    )


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


def _execution_elements(trace_id: str, action: ProposedAction | None, retryable: bool) -> list[dict]:
    """パターン5 のボタン。**出す条件はステートだけ**（design.md 3.6 / D-021）。

    操作の種別・環境・危険度では分岐しない。confirm は承認ボタンと同じく一律で付ける。
    """
    target = (
        f"{action.environment.value} の {action.target.kind}/{action.target.name}"
        if action
        else "対象不明"
    )
    elements: list[dict] = []
    if retryable:
        elements.append(
            {
                "type": "button",
                "action_id": ACTION_ID_RETRY,
                "text": {"type": "plain_text", "text": truncate("再試行する", BUTTON_TEXT_LIMIT), "emoji": True},
                "style": "danger",
                "value": truncate(trace_id, BUTTON_VALUE_LIMIT),
                "confirm": {
                    "title": {"type": "plain_text", "text": truncate("もう一度実行しますか？", CONFIRM_TITLE_LIMIT)},
                    "text": {
                        "type": "mrkdwn",
                        "text": truncate(f"*{target}* に対する操作を再実行します。", CONFIRM_TEXT_LIMIT),
                    },
                    "confirm": {"type": "plain_text", "text": "再実行する"},
                    "deny": {"type": "plain_text", "text": "キャンセル"},
                },
            }
        )
    elements.append(
        {
            "type": "button",
            "action_id": ACTION_ID_ABANDON,
            "text": {"type": "plain_text", "text": truncate("これ以上試さない", BUTTON_TEXT_LIMIT), "emoji": True},
            "value": truncate(trace_id, BUTTON_VALUE_LIMIT),
        }
    )
    return elements


def _execution_failed(event: EscalationEvent, ctx: NotificationContext) -> Notification:
    """通知パターン5（確定的な失敗）。**実行されていないことが分かっている**場合。"""
    env = event.action.environment.value if event.action else "unknown"
    blocks = [
        _header("⚠️ [HIGH] 実行に失敗しました（操作は実行されていません）"),
        _fields(
            [
                ("対象環境", f"`{env}`"),
                ("Trace ID", f"`{event.trace_id}`"),
                ("応答", f"`{event.http_status or '(応答以外の確定失敗)'}`"),
                ("再試行回数", f"`{ctx.retry_count}`"),
            ]
        ),
        _section(f"*失敗の内容:*\n{event.reason}"),
        *_action_blocks(event.action),
        _context(
            "💡 サーバが要求を**処理しなかった**ことが応答から分かっています"
            "（クラスタの状態は変わっていません）。原因を取り除いてから再試行してください。"
        ),
        {"type": "actions", "elements": _execution_elements(event.trace_id, event.action, ctx.retryable)},
    ]
    return Notification(text=f"[HIGH] 実行に失敗しました（{event.trace_id}）", blocks=blocks)


def _execution_unknown(event: EscalationEvent) -> Notification:
    """通知パターン5（実行有無が不明）。**ボタンを出さない**（D-028）。

    これはパターン4 でボタンを出さないのとまったく同じ規律である。
    評価できていないものに承認ボタンを出さないように、
    **実行されたか分からないものに再試行ボタンを出さない。**
    """
    env = event.action.environment.value if event.action else "unknown"
    blocks = [
        _header("🚨 [CRITICAL] 実行されたかどうかが判別できません"),
        _fields(
            [
                ("対象環境", f"`{env}`"),
                ("Trace ID", f"`{event.trace_id}`"),
                ("応答", f"`{event.http_status or '応答なし'}`"),
                ("判定", "`unknown (実行有無が不明)`"),
            ]
        ),
        _section(f"*状況:*\n{event.reason}"),
        *_action_blocks(event.action),
        _context(
            "💡 *クラスタの実際の状態を人間が確認してください。* "
            "要求はサーバに届いた可能性があり、結果は保証されていません。"
            "**再試行ボタンは出していません**（実行済みのものを再実行すると二重実行になるため）。"
        ),
    ]
    return Notification(text=f"[CRITICAL] 実行の有無が不明です（{event.trace_id}）", blocks=blocks)


# kind -> 通知パターンの対応を知っているのはこの表だけである。
# **ボタンが出るかどうかも、この 1 本道でしか決まらない。**
_BUILDERS = {
    "request_failed": lambda e, ctx: _request_failed(e),
    "validation_failed": lambda e, ctx: _validation_failed(e),
    "policy_denied": lambda e, ctx: _policy_denied(e, ctx.expires_at),
    "policy_unavailable": lambda e, ctx: _policy_unavailable(e),
    "execution_failed": _execution_failed,
    "execution_unknown": lambda e, ctx: _execution_unknown(e),
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
        # 見出しは層の名前ではなくフィールドの中身で決める。``errors`` には第2層の
        # ValidationError だけでなく、第1層の例外も入る（D-031）。
        blocks.append(_section(f"*失敗の詳細（全文）:*\n{_code_block(dumped, SECTION_TEXT_LIMIT - 64)}"))
    if event.action and len(event.action.reason) > REASON_PREVIEW:
        blocks.append(_section(f"*AI の思考ログ（全文）:*\n>{truncate(event.action.reason, SECTION_TEXT_LIMIT - 8)}"))
    return blocks[:MAX_BLOCKS]


def build_notification(
    event: EscalationEvent,
    *,
    expires_at: datetime | None = None,
    retryable: bool = False,
    retry_count: int = 0,
) -> Notification:
    """EscalationEvent から送出物を組み立てる。**マスキングはここで必ず適用される。**

    呼び出し側がマスキングを忘れられない構造にしてある（design.md 3.8）。

    ``expires_at`` は承認期限（パターン3 のみ使う）。**イベントではなくストアが持つ**
    （design.md 確認事項11）ので、引数で受け取る。ここで環境変数から TTL を読むと、
    「表示した期限」と「ストアが判定に使う期限」が別々に決まって食い違いうる。
    """
    ctx = NotificationContext(expires_at=expires_at, retryable=retryable, retry_count=retry_count)
    notification = _BUILDERS[event.kind](event, ctx)
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
