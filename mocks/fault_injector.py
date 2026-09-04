"""Phase 1 異常系用の障害注入サーバ。

LiteLLM Proxy の primary デプロイをこのサーバに差し替えることで、
任意のエラーをエージェント側のコードを変更せずに注入する。
OpenAI 互換の /v1/chat/completions を受け、FAULT_MODE に応じた応答を返す。

依存ライブラリなし（標準ライブラリのみ）。

環境変数:
  FAULT_MODE            rate_limit | server_error | timeout | success
  FAULT_DELAY_SECONDS   FAULT_MODE=timeout のときのスリープ秒数（既定: 45）
  PORT                  待ち受けポート（既定: 8080）
"""

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FAULT_MODE = os.environ.get("FAULT_MODE", "server_error")
FAULT_DELAY_SECONDS = float(os.environ.get("FAULT_DELAY_SECONDS", "45"))
PORT = int(os.environ.get("PORT", "8080"))

# FAULT_MODE -> (HTTP ステータス, OpenAI 形式のエラー type)
FAULTS = {
    "rate_limit": (429, "rate_limit_error"),
    "server_error": (500, "internal_server_error"),
}


def _error_body(message: str, err_type: str) -> bytes:
    return json.dumps(
        {"error": {"message": message, "type": err_type, "code": err_type}}
    ).encode()


def _success_body() -> bytes:
    """FAULT_MODE=success のときに返す、最小限の OpenAI 互換応答。

    「モックに向いているのに成功してしまう」状態を検知するための対照用。
    """
    return json.dumps(
        {
            "id": "chatcmpl-fault-injector",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "fault-injector",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "FAULT_INJECTOR_RESPONSE"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
    ).encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _respond(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        # LiteLLM のヘルスチェックおよび手動疎通確認用
        self._respond(200, json.dumps({"status": "ok", "fault_mode": FAULT_MODE}).encode())

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)  # ボディは読み捨てる

        self.log_message("fault_mode=%s path=%s", FAULT_MODE, self.path)

        if FAULT_MODE == "timeout":
            # LiteLLM の timeout 設定を超えるまで応答しない
            time.sleep(FAULT_DELAY_SECONDS)
            self._respond(504, _error_body("simulated upstream timeout", "timeout_error"))
            return

        if FAULT_MODE == "success":
            self._respond(200, _success_body())
            return

        status, err_type = FAULTS.get(FAULT_MODE, FAULTS["server_error"])
        self._respond(status, _error_body(f"injected fault: {FAULT_MODE}", err_type))


if __name__ == "__main__":
    print(
        f"fault-injector listening on :{PORT} "
        f"(FAULT_MODE={FAULT_MODE}, FAULT_DELAY_SECONDS={FAULT_DELAY_SECONDS})",
        flush=True,
    )
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
