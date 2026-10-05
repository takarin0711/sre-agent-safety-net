"""LiteLLM 自身の Slack alerting が何を送るかを観測するための受け口。

通知パターン1 の整理（[D-031](../docs/decisions.md)）で残った最後の未確認項目を潰すためのもの。
「LiteLLM の alerting を設定すれば基盤の障害は通知できる」と Phase 4 の C-02 で書いたが、
**本 PoC では一度も設定していなかった**ため、以下が分かっていなかった。

- そもそも第1層の全滅で alert が飛ぶのか
- 飛ぶとして、エージェントの ``trace_id`` が載るのか（C-02 の未確認事項）

実 Slack の Incoming Webhook を作らずにこれを確かめるために、
``SLACK_WEBHOOK_URL`` をこのサーバに向けて**本文をそのまま記録する**。
送出先を実ワークスペースにしないので、検証のたびにチャンネルを汚さずに済む。

受けたものは標準出力に 1 行 1 リクエストの JSON として出す（``agents/log.py`` と同じ形）。
常に 200 を返す。**遅延も失敗も注入しない**（ここで観測したいのは送出側の中身だけで、
alerting の異常系は本 PoC の検証対象ではない）。

依存ライブラリなし（標準ライブラリのみ）。

環境変数:
  PORT  待ち受けポート（既定: 8090）
"""

import json
import os
import sys
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8090"))


def log(**fields: object) -> None:
    print(json.dumps(fields, ensure_ascii=False, default=str), flush=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            # Slack の Incoming Webhook は JSON を受ける。
            # 壊れていてもそのまま残す（整形して捨てると観測にならない）。
            body: object = json.loads(raw)
        except json.JSONDecodeError:
            body = raw

        log(
            event="webhook.received",
            received_at=datetime.now(UTC).isoformat(),
            path=self.path,
            body=body,
        )

        payload = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        # アクセスログは標準エラーへ。標準出力は本文の記録だけに使う。
        print(f"{self.address_string()} {format % args}", file=sys.stderr, flush=True)


def main() -> None:
    log(event="webhook_sink.started", port=PORT)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
