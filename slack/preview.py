"""通知の実機確認用 CLI（design.md 8. 実施手順 1）。

第1層〜第3層を起動せずに、通知パターン 1（代替）/ 2 / 3 / 4 をそのまま出せる。

    python -m slack.preview --dump            # JSON を標準出力へ（Slack に触れない）
    python -m slack.preview --dump --pattern 3 > docs/phases/phase4-slack/assets/s02.json
    python -m slack.preview                   # 実際に送る（SLACK_CHANNEL_ID 宛て）
    python -m slack.preview --pattern 3       # パターン3 だけ送る

``--dump`` は Block Kit Builder に貼って表示を確かめるためのもので、
ワークスペースが無くても実行できる。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

from agents.escalation import build_request_failed_event
from agents.log import log
from agents.policy import build_denied_event, build_unavailable_event
from agents.schemas import EscalationEvent, PolicyDecision, ViolatedRule
from agents.validation import build_escalation_event, validate_action
from slack import notifier
from slack.blocks import build_notification
from slack.store import Store, actionable_until

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
INCIDENT = "本番の payment-api の Pod が CrashLoopBackOff"


def _action(name: str, trace_id: str):
    return validate_action(
        (FIXTURES / name).read_text(encoding="utf-8"), original_request=INCIDENT, trace_id=trace_id
    )


def sample_event(pattern: int, trace_id: str) -> EscalationEvent:
    """Phase 2・3 の fixture から、実際に層が出すのと同じ形のイベントを作る。"""
    if pattern == 1:
        # パターン1 の**代替**（D-031）。第1層が全滅したときに OpenAI SDK から出る
        # 例外の型と文面をそのまま使う（Z-05 の実測に合わせる）。
        return build_request_failed_event(
            "APIConnectionError",
            "Connection error.",
            trace_id=trace_id,
            original_request=INCIDENT,
        )
    if pattern == 2:
        raw = (FIXTURES / "v01_prose.txt").read_text(encoding="utf-8")
        try:
            validate_action(raw, original_request=INCIDENT, trace_id=trace_id)
        except Exception as exc:  # 第2層の ValidationError をそのまま使う
            return build_escalation_event(
                exc,  # type: ignore[arg-type]
                raw_output=raw,
                original_request=INCIDENT,
                trace_id=trace_id,
            )
        raise AssertionError("v01_prose.txt は第2層を通過してはいけない")
    if pattern == 3:
        # B-01 と同じ入力（本番の PVC 削除）。違反は意図的に複数にする（考察6）。
        decision = PolicyDecision(
            allow=False,
            violated_rules=[
                ViolatedRule(
                    rule="PROHIBIT_PVC_DELETE",
                    message="PVC の削除は不可逆なため自動実行を許可しません（対象: prod/payment-db-data）",
                ),
                ViolatedRule(
                    rule="PROHIBIT_PRODUCTION_DESTRUCTIVE",
                    message="production 環境での破壊的操作は人間の承認を要します",
                ),
            ],
        )
        return build_denied_event(
            decision,
            action=_action("n01_dangerous_delete_pvc.json", trace_id),
            trace_id=trace_id,
            original_request=INCIDENT,
        )
    return build_unavailable_event(
        "timeout",
        "3.0 秒以内に応答がありませんでした（ConnectTimeout）",
        action=_action("valid_rollout_restart.json", trace_id),
        trace_id=trace_id,
        original_request=INCIDENT,
    )


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="通知パターンのプレビュー")
    parser.add_argument(
        "--pattern", type=int, choices=(1, 2, 3, 4), help="省略時は 1 / 2 / 3 / 4 をすべて出す"
    )
    parser.add_argument("--dump", action="store_true", help="送らずに JSON を標準出力へ")
    parser.add_argument(
        "--out",
        help="--dump の出力先ディレクトリ。構造化ログが stdout に混ざるため、"
        "assets に残すときはこちらを使う（pattern2.json のように書き出す）",
    )
    parser.add_argument("--trace-id", default="preview", help="Trace ID の接頭辞")
    args = parser.parse_args()

    patterns = [args.pattern] if args.pattern else [1, 2, 3, 4]
    for pattern in patterns:
        trace_id = f"{args.trace_id}-{pattern}"
        event = sample_event(pattern, trace_id)
        notification = build_notification(event)

        if args.dump:
            payload = json.dumps({"blocks": notification.blocks}, ensure_ascii=False, indent=2)
            if args.out:
                path = Path(args.out)
                path.mkdir(parents=True, exist_ok=True)
                (path / f"pattern{pattern}.json").write_text(payload, encoding="utf-8")
                log(event="preview.dumped", pattern=pattern, path=str(path / f"pattern{pattern}.json"))
            else:
                print(payload)
            continue

        # パターン3 は承認ボタンが出る。押されたときに復元できるよう、送る前にストアへ入れる
        # （design.md 3.5 の順序。逆にすると「押しても何も起きない」状態が作れる）。
        store = None
        if event.kind == "policy_denied":
            store = Store()
            row = store.create_pending(
                trace_id=trace_id, kind=event.kind, event_json=event.model_dump_json()
            )
            # 表示する承認期限は、ストアが判定に使う値そのもの
            notification = build_notification(event, expires_at=actionable_until(row))
        try:
            delivery = notifier.post(notification)
        except notifier.NotificationError as exc:
            log(event="preview.failed", pattern=pattern, failure=exc.failure, detail=exc.detail)
            return 1
        if store is not None:
            store.attach_message(trace_id, channel=delivery.channel, message_ts=delivery.ts)
        if notification.thread_blocks:
            notifier.post_thread(delivery, notification.thread_blocks, text="詳細（全文）")
        log(
            event="preview.sent",
            pattern=pattern,
            trace_id=trace_id,
            channel=delivery.channel,
            message_ts=delivery.ts,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
