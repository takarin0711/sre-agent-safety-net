"""送出したメッセージを Slack から読み戻す（design.md 6. 観測方法）。

**「例外が出なかった」は届いた証拠にならない**（D-018 の Phase 4 版）。
``chat.postMessage`` が ok を返したことと、そのメッセージが実際に載っていることは別である。
判定基準7（押下後に ``actions`` ブロックが消えていること）の確認もここで行う。

    python -m slack.readback                 # 直近 1 件の blocks 構造を出す
    python -m slack.readback --limit 5       # 直近 5 件
    python -m slack.readback --json out.json # 生の JSON を assets 用に保存
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

from agents.log import log
from slack import notifier


def summarize(message: dict) -> dict:
    """人間が判定に使う最小限だけを抜く。"""
    blocks = message.get("blocks", [])
    buttons = [
        {"action_id": element.get("action_id"), "value": element.get("value"), "confirm": "confirm" in element}
        for block in blocks
        if block.get("type") == "actions"
        for element in block.get("elements", [])
    ]
    return {
        "ts": message.get("ts"),
        "text": message.get("text"),
        "block_types": [block.get("type") for block in blocks],
        "has_actions": any(block.get("type") == "actions" for block in blocks),
        "buttons": buttons,
        "thread_replies": message.get("reply_count", 0),
    }


def main() -> int:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    parser = argparse.ArgumentParser(description="送出したメッセージの読み戻し")
    parser.add_argument("--limit", type=int, default=1, help="直近から何件読むか")
    parser.add_argument("--json", help="生の JSON を保存するパス（assets 用）")
    args = parser.parse_args()

    client = notifier.build_client()
    channel = notifier.channel_id(client)
    response = client.conversations_history(channel=channel, limit=args.limit)
    messages = response["messages"]

    log(event="readback.fetched", channel=channel, count=len(messages))
    for message in messages:
        log(event="readback.message", channel=channel, **summarize(message))

    if args.json:
        Path(args.json).write_text(
            json.dumps({"channel": channel, "messages": messages}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log(event="readback.saved", path=args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
