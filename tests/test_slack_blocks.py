"""Block Kit 生成の単体検証（design.md 4.1 / シナリオ S-01〜S-08）。

**ワークスペースを使わずに実行できる**ことが、このフェーズの主張のひとつである
（判定基準1）。したがってここでは Slack に一切触れない。

入力は Phase 2・3 の fixture から組み立てる。通知のためだけの新しい入力を作ると、
「実際に層が出すイベント」と「通知のテスト用のイベント」が乖離する。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic_core import ValidationError

from agents.escalation import build_request_failed_event
from agents.policy import build_denied_event, build_unavailable_event
from agents.schemas import EscalationEvent, PolicyDecision, ProposedAction, ViolatedRule
from agents.validation import build_escalation_event, validate_action
from slack.blocks import (
    _BUILDERS,
    ACTION_ID_APPROVE,
    ACTION_ID_REJECT,
    HEADER_TEXT_LIMIT,
    MAX_BLOCKS,
    MAX_FIELDS,
    SECTION_TEXT_LIMIT,
    build_notification,
)

FIXTURES = Path(__file__).parent / "fixtures"
TRACE = "trace-0001"
KNOWN_BLOCK_TYPES = {"header", "section", "divider", "actions", "context"}


def action_from(name: str) -> ProposedAction:
    path = FIXTURES / name
    return validate_action(path.read_text(encoding="utf-8"), original_request="検証用", trace_id=TRACE)


def denied_event(*, rules: int = 1, action_file: str = "n01_dangerous_delete_pvc.json") -> EscalationEvent:
    action = action_from(action_file)
    violated = [
        ViolatedRule(rule=f"PROHIBIT_RULE_{i}", message=f"{i} 件目の理由。人間が読んで判断できる文言。")
        for i in range(1, rules + 1)
    ]
    decision = PolicyDecision(allow=False, violated_rules=violated)
    return build_denied_event(decision, action=action, trace_id=TRACE, original_request="本番が落ちた")


def unavailable_event() -> EscalationEvent:
    return build_unavailable_event(
        "timeout",
        "3.0 秒以内に応答がありませんでした（ConnectTimeout）",
        action=action_from("valid_rollout_restart.json"),
        trace_id=TRACE,
        original_request="本番が落ちた",
    )


def request_failed_event() -> EscalationEvent:
    """第1層の呼び出しが失敗した状態（通知パターン1 の代替）。"""
    return build_request_failed_event(
        "APIConnectionError",
        "Connection error.",
        trace_id=TRACE,
        original_request="本番が落ちた",
    )


def validation_failed_event(fixture: str = "v01_prose.txt") -> EscalationEvent:
    raw = (FIXTURES / fixture).read_text(encoding="utf-8")
    try:
        validate_action(raw, original_request="本番が落ちた", trace_id=TRACE)
    except ValidationError as exc:
        return build_escalation_event(
            exc, raw_output=raw, original_request="本番が落ちた", trace_id=TRACE
        )
    raise AssertionError(f"{fixture} は第2層を通過してはいけない")


def block_types(blocks: list[dict]) -> list[str]:
    return [b["type"] for b in blocks]


def collect(blocks: list[dict], key: str) -> list:
    """入れ子の dict / list から特定のキーの値をすべて拾う。"""
    found = []
    stack: list = list(blocks)
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if key in item:
                found.append(item[key])
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found


# ── パターン1 の代替（D-031）──────────────────────────────


def test_request_failed_has_no_buttons():
    """提案が存在しない。承認できるものが無いのでボタンも無い。"""
    notification = build_notification(request_failed_event())
    assert "actions" not in block_types(notification.blocks)
    assert collect(notification.blocks, "action_id") == []


def test_request_failed_says_what_it_knows_and_nothing_more():
    """**基盤の状態を書かない。** 書けるのは「この 1 件が処理されなかった」ことだけ。"""
    notification = build_notification(request_failed_event())
    dumped = json.dumps(notification.blocks, ensure_ascii=False)
    for required in (TRACE, "第1層", "APIConnectionError", "本番が落ちた"):
        assert required in dumped, required
    # 提案も違反も存在しない。通知層が埋めて穴埋めしていないことの確認。
    for absent in ("提案されたアクション", "違反したポリシー", "AI の思考ログ"):
        assert absent not in dumped, absent
    # 代替であることが読み手に分かる形で書かれている（パターン1 を騙らない）。
    assert "基盤障害そのもののアラートではありません" in dumped


def test_every_kind_has_exactly_one_builder():
    """``kind`` を増やしたのにビルダーを足し忘れると KeyError で落ちる。

    その KeyError は送出の直前に出る（＝**人間に届かない**）ので、ここで止める。
    """
    kinds = set(EscalationEvent.model_fields["kind"].annotation.__args__)  # type: ignore[union-attr]
    assert set(_BUILDERS) == kinds


# ── S-01 パターン2 ────────────────────────────────────────


def test_s01_validation_failed_has_no_buttons():
    notification = build_notification(validation_failed_event())
    assert "actions" not in block_types(notification.blocks)
    dumped = json.dumps(notification.blocks, ensure_ascii=False)
    assert TRACE in dumped
    assert "第2層" in dumped


# ── S-02 / S-03 パターン3 ─────────────────────────────────


def test_s02_policy_denied_has_required_elements():
    notification = build_notification(denied_event(rules=1))
    dumped = json.dumps(notification.blocks, ensure_ascii=False)
    assert "actions" in block_types(notification.blocks)
    for required in ("production", TRACE, "kubectl_delete_pvc", "PROHIBIT_RULE_1", "第3層"):
        assert required in dumped, required
    action_ids = collect(notification.blocks, "action_id")
    assert action_ids == [ACTION_ID_APPROVE, ACTION_ID_REJECT] or set(action_ids) == {
        ACTION_ID_APPROVE,
        ACTION_ID_REJECT,
    }


def test_s03_all_violations_are_shown_not_summarized():
    """違反は**すべて**表示する。1 件に要約しない（phase3 result.md 考察6）。"""
    notification = build_notification(denied_event(rules=4))
    dumped = json.dumps(notification.blocks, ensure_ascii=False)
    for i in range(1, 5):
        assert f"PROHIBIT_RULE_{i}" in dumped
        assert f"{i} 件目の理由" in dumped


# ── S-04 パターン4 ────────────────────────────────────────


def test_s04_policy_unavailable_has_no_buttons():
    """評価できていない操作に承認ボタンを出さない（Fail-Closed を UI で崩さない）。"""
    notification = build_notification(unavailable_event())
    assert "actions" not in block_types(notification.blocks)
    assert collect(notification.blocks, "action_id") == []
    assert "timeout" in json.dumps(notification.blocks, ensure_ascii=False)


@pytest.mark.parametrize("builder", [request_failed_event, validation_failed_event, unavailable_event])
def test_s04_only_policy_denied_is_approvable(builder):
    assert collect(build_notification(builder()).blocks, "action_id") == []


# ── S-05 上限と切り詰め ───────────────────────────────────


def test_s05_long_input_is_truncated_and_says_so():
    event = denied_event(rules=6)
    event = event.model_copy(update={"raw_output": "あ" * 8000})
    # reason はスキーマ上限（1000 文字）いっぱいのものを使う
    long_action = event.action.model_copy(update={"reason": "い" * 1000})
    event = event.model_copy(update={"action": long_action})

    notification = build_notification(event)
    assert len(notification.blocks) <= MAX_BLOCKS
    for block in notification.blocks:
        if block["type"] == "header":
            assert len(block["text"]["text"]) <= HEADER_TEXT_LIMIT
        if block["type"] == "section" and "text" in block:
            assert len(block["text"]["text"]) <= SECTION_TEXT_LIMIT
        if block["type"] == "section" and "fields" in block:
            assert len(block["fields"]) <= MAX_FIELDS
    # 全文はスレッドに回り、本体には切り詰めた旨が残る
    body = json.dumps(notification.blocks, ensure_ascii=False)
    assert "切り詰め" in body
    assert notification.thread_blocks


# ── S-06 マスキング ───────────────────────────────────────

SECRETS = {
    "aws_key": "AKIAIOSFODNN7EXAMPLE",
    "slack_token": "xoxb-1234567890-abcdefghij",
    "bearer": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
    "url": "postgres://admin:sup3rs3cret@db.internal:5432/app",
}


def test_s06_secrets_never_reach_the_payload():
    raw = " ".join(SECRETS.values())
    event = denied_event(rules=1)
    leaky_action = event.action.model_copy(update={"reason": f"接続情報: {raw}"})
    event = event.model_copy(update={"action": leaky_action, "raw_output": raw})

    notification = build_notification(event)
    dumped = json.dumps(
        {"text": notification.text, "blocks": notification.blocks, "thread": notification.thread_blocks},
        ensure_ascii=False,
    )
    assert SECRETS["aws_key"] not in dumped
    assert SECRETS["slack_token"] not in dumped
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in dumped
    assert "sup3rs3cret" not in dumped


# ── S-07 value は Trace ID のみ ───────────────────────────


@pytest.mark.parametrize(
    "builder",
    [request_failed_event, validation_failed_event, lambda: denied_event(rules=3), unavailable_event],
)
def test_s07_button_value_contains_only_trace_id(builder):
    """運用原則2。実行内容がペイロードに一切載らないことを機械的に確認する。"""
    notification = build_notification(builder())
    values = collect(notification.blocks, "value")
    assert all(value == TRACE for value in values)
    for value in values:
        for leaked in ("kubectl", "prod", "delete", "db-data", "namespace"):
            assert leaked not in value


# ── S-08 構造 ────────────────────────────────────────────


@pytest.mark.parametrize(
    "builder",
    [request_failed_event, validation_failed_event, lambda: denied_event(rules=2), unavailable_event],
)
def test_s08_structure_is_valid_block_kit(builder):
    notification = build_notification(builder())
    assert notification.text
    assert 0 < len(notification.blocks) <= MAX_BLOCKS
    for block in notification.blocks:
        assert block["type"] in KNOWN_BLOCK_TYPES, block["type"]
        if block["type"] == "actions":
            for element in block["elements"]:
                assert element["type"] == "button"
                assert element["action_id"]
                assert element["text"]["type"] == "plain_text"
        if block["type"] == "header":
            assert block["text"]["type"] == "plain_text"


def test_s08_approve_button_always_has_confirm():
    """confirm は**すべての承認ボタンに一律で**付ける（D-021。分岐を作らない）。"""
    for rules in (1, 3):
        notification = build_notification(denied_event(rules=rules))
        buttons = [
            element
            for block in notification.blocks
            if block["type"] == "actions"
            for element in block["elements"]
        ]
        approve = next(b for b in buttons if b["action_id"] == ACTION_ID_APPROVE)
        assert approve["confirm"]["title"]["type"] == "plain_text"
        assert approve["style"] == "danger"


# ── D-01 決定性（同一イベントから同一の Block Kit）──────────


def test_d01_block_kit_is_deterministic():
    event = denied_event(rules=3).model_copy(
        update={"occurred_at": datetime(2026, 10, 1, 12, 0, tzinfo=UTC)}
    )
    first = json.dumps(build_notification(event).blocks, ensure_ascii=False, sort_keys=True)
    for _ in range(10):
        again = json.dumps(build_notification(event).blocks, ensure_ascii=False, sort_keys=True)
        assert again == first


# ── 承認期限の表示（Handoff 仕様書 パターン3 の context）────


def test_deadline_is_shown_only_on_the_approvable_pattern():
    """期限を知らせないと、受け取った人は猶予を知らずに放置できてしまう。

    値は**ストアが判定に使う expires_at そのもの**を渡す（別々に計算しない）。
    """
    from datetime import timedelta

    expires_at = datetime.now(UTC) + timedelta(minutes=15)
    denied = build_notification(denied_event(), expires_at=expires_at)
    assert "承認期限" in json.dumps(denied.blocks, ensure_ascii=False)

    # ボタンが無いパターンに期限は要らない（承認できないため）
    for builder in (request_failed_event, validation_failed_event, unavailable_event):
        assert "承認期限" not in json.dumps(build_notification(builder()).blocks, ensure_ascii=False)


def test_expired_deadline_says_it_cannot_be_executed():
    from datetime import timedelta

    expired = build_notification(denied_event(), expires_at=datetime.now(UTC) - timedelta(minutes=1))
    assert "超過" in json.dumps(expired.blocks, ensure_ascii=False)
