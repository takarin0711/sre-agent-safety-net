"""異常系用の障害注入サーバ。

LiteLLM Proxy の primary デプロイをこのサーバに差し替えることで、
任意のエラーをエージェント側のコードを変更せずに注入する。
OpenAI 互換の /v1/chat/completions を受け、FAULT_MODE に応じた応答を返す。

Phase 1（第1層）は「呼び出しそのものを失敗させる」モード（rate_limit / server_error / timeout）、
Phase 2（第2層）は「HTTP 200 のまま content だけを壊す」モード（malformed）を使う。
malformed では**第1層から見ると完全に正常**であり、検知するのは第2層だけになる。

依存ライブラリなし（標準ライブラリのみ）。

環境変数:
  FAULT_MODE            rate_limit | server_error | timeout | success | malformed
  FAULT_DELAY_SECONDS   FAULT_MODE=timeout のときのスリープ秒数（既定: 45）
  FAULT_FIXTURE         FAULT_MODE=malformed のときに content へ詰める fixture 名
                        （拡張子なし。tests/fixtures/ と共用する。
                         予約名 null_content は content: null を返す）
  FAULT_FIXTURE_DIR     fixture の探索先（既定: /fixtures）
  PORT                  待ち受けポート（既定: 8080）
"""

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FAULT_MODE = os.environ.get("FAULT_MODE", "server_error")
FAULT_DELAY_SECONDS = float(os.environ.get("FAULT_DELAY_SECONDS", "45"))
FAULT_FIXTURE = os.environ.get("FAULT_FIXTURE", "v01_prose")
FAULT_FIXTURE_DIR = Path(os.environ.get("FAULT_FIXTURE_DIR", "/fixtures"))
PORT = int(os.environ.get("PORT", "8080"))

# content を JSON の null にするための予約 fixture 名。
# 「空文字列」はファイルで表現できるが、「文字列ですらない」状態はファイルでは表現できない。
NULL_CONTENT_FIXTURE = "null_content"

# FAULT_MODE -> (HTTP ステータス, OpenAI 形式のエラー type)
FAULTS = {
    "rate_limit": (429, "rate_limit_error"),
    "server_error": (500, "internal_server_error"),
}


def _error_body(message: str, err_type: str) -> bytes:
    return json.dumps(
        {"error": {"message": message, "type": err_type, "code": err_type}}
    ).encode()


def _completion_body(content: "str | None") -> bytes:
    """最小限の OpenAI 互換の成功応答（HTTP 200）。content だけを差し替えられるようにする。"""
    return json.dumps(
        {
            "id": "chatcmpl-fault-injector",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "fault-injector",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
    ).encode()


def _success_body() -> bytes:
    """FAULT_MODE=success のときに返す応答。

    「モックに向いているのに成功してしまう」状態を検知するための対照用。
    """
    return _completion_body("FAULT_INJECTOR_RESPONSE")


def _load_fixture(name: str) -> "str | None":
    """fixture を読み込む。見つからなければ起動時に落とす（黙って別のシナリオにしない）。

    存在しない fixture で 500 を返すと第1層がフォールバックして実 Bedrock が応答し、
    「壊れた content を返したはずなのに正常なアクションが返る」という誤ったシナリオになる。
    """
    if name == NULL_CONTENT_FIXTURE:
        return None

    for suffix in (".txt", ".json"):
        path = FAULT_FIXTURE_DIR / f"{name}{suffix}"
        if path.is_file():
            return path.read_text(encoding="utf-8")

    available = sorted(p.name for p in FAULT_FIXTURE_DIR.glob("*")) if FAULT_FIXTURE_DIR.is_dir() else []
    print(
        f"FAULT_FIXTURE={name!r} が {FAULT_FIXTURE_DIR} に見つかりません。"
        f"利用可能: {available}",
        file=sys.stderr,
        flush=True,
    )
    raise SystemExit(1)


# malformed のときだけ、起動時に fixture を解決しておく（実行時に初めて失敗しないように）。
MALFORMED_CONTENT = _load_fixture(FAULT_FIXTURE) if FAULT_MODE == "malformed" else None


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

        if FAULT_MODE == "malformed":
            # HTTP 200 と OpenAI 互換の形は保ったまま、content だけを壊す。
            # 第1層から見ると正常応答であり、フォールバックも発火しない。
            self.log_message(
                "malformed fixture=%s content_bytes=%s",
                FAULT_FIXTURE,
                "null" if MALFORMED_CONTENT is None else len(MALFORMED_CONTENT.encode()),
            )
            self._respond(200, _completion_body(MALFORMED_CONTENT))
            return

        status, err_type = FAULTS.get(FAULT_MODE, FAULTS["server_error"])
        self._respond(status, _error_body(f"injected fault: {FAULT_MODE}", err_type))


if __name__ == "__main__":
    print(
        f"fault-injector listening on :{PORT} "
        f"(FAULT_MODE={FAULT_MODE}, FAULT_DELAY_SECONDS={FAULT_DELAY_SECONDS}, "
        f"FAULT_FIXTURE={FAULT_FIXTURE if FAULT_MODE == 'malformed' else '-'})",
        flush=True,
    )
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
