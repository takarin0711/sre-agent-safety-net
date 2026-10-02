"""実行層の障害注入（phase5 design.md 3.11）。

executor と kind の API サーバの**あいだ**に挟まるプロキシ。
``agents/`` と ``slack/`` は一切変更せず、kubeconfig の ``server`` をここに向けるだけで注入する
（Phase 1〜4 と同じ原則）。

    K8S_PROXY_UPSTREAM=https://127.0.0.1:6443 K8S_PROXY_CA=var/kind-ca.pem \
      K8S_PROXY_MODE=server_error python mocks/k8s_proxy.py

モード:

===============  ==========================================================
``pass``         素通し（注入が経路に入っていること自体の確認に使う）
``server_error`` 上流へ**転送せずに** 500 を返す
``drop_response`` **上流へ転送してから**応答を返さずに切る。実行はされているが
                 クライアントは結果を知らない ＝ ``unknown`` の本体
``timeout``      応答を返さずに待つ（クライアント側のタイムアウトを起こす）
===============  ==========================================================

``mocks/fault_injector.py`` は**改修しない**（Phase 1〜4 の再現性を保つ）。
実行層の注入は別ファイルに置く。注入部品を ``mocks/`` に置く方針は D-006 と同じ。

標準ライブラリだけで書く（第1層の注入器と同じ制約）。
"""

from __future__ import annotations

import http.client
import json
import os
import ssl
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

MODE = os.environ.get("K8S_PROXY_MODE", "pass")
UPSTREAM = os.environ.get("K8S_PROXY_UPSTREAM", "")
CA_FILE = os.environ.get("K8S_PROXY_CA", "")
PORT = int(os.environ.get("K8S_PROXY_PORT", "8081"))
DELAY = float(os.environ.get("K8S_PROXY_DELAY_SECONDS", "60"))

VALID_MODES = ("pass", "server_error", "drop_response", "timeout")

# 転送しないモード。**上流に届いていない**ことがログから読めるようにする。
NOT_FORWARDED = ("server_error", "timeout")


def _log(**fields: object) -> None:
    """注入側のアクセスログ。**これが「注入が効いた」ことの証拠になる**（D-018）。"""
    print(json.dumps({"source": "k8s_proxy", **fields}, ensure_ascii=False), flush=True)


class Proxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:  # 既定の stderr ログは出さない
        pass

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        forwarded = MODE not in NOT_FORWARDED
        _log(mode=MODE, method=self.command, path=self.path, forwarded=forwarded)

        if MODE == "server_error":
            # 上流に届いていないが、**クライアントからは区別できない**。
            # だからこそ 5xx は「失敗」ではなく「不明」に分類する（D-028）。
            payload = json.dumps(
                {"kind": "Status", "status": "Failure", "message": "injected by k8s_proxy", "code": 500}
            ).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if MODE == "timeout":
            time.sleep(DELAY)
            self.close_connection = True
            return

        status, headers, upstream_body = self._forward(body)
        _log(mode=MODE, upstream_status=status, path=self.path)

        if MODE == "drop_response":
            # **上流では実行されている。** 応答だけを握り潰す。
            # 「実行されたのに unknown」を決定的に作るためのモード。
            _log(mode=MODE, note="上流は応答したが、クライアントには返さない", upstream_status=status)
            self.close_connection = True
            return

        self.send_response(status)
        for key, value in headers:
            if key.lower() not in ("transfer-encoding", "connection", "content-length"):
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(upstream_body)))
        self.end_headers()
        self.wfile.write(upstream_body)

    def _forward(self, body: bytes) -> tuple[int, list[tuple[str, str]], bytes]:
        parsed = urlparse(UPSTREAM)
        context = ssl.create_default_context(cafile=CA_FILE or None)
        # kind の証明書は 127.0.0.1 向けに出ていないことがある。検証対象は TLS ではない。
        context.check_hostname = False
        conn = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, context=context, timeout=30)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "content-length")}
        if body:
            headers["Content-Length"] = str(len(body))
        conn.request(self.command, self.path, body=body or None, headers=headers)
        response = conn.getresponse()
        data = response.read()
        result = (response.status, response.getheaders(), data)
        conn.close()
        return result

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle


def main() -> int:
    if MODE not in VALID_MODES:
        print(f"K8S_PROXY_MODE は {VALID_MODES} のいずれか（指定値: {MODE}）", file=sys.stderr)
        return 2
    if not UPSTREAM:
        # 起動時に落とす。実行時に失敗すると「注入したつもりで素通り」になる
        # （Phase 2 で踏んだ形。D-014）。
        print("K8S_PROXY_UPSTREAM が未設定です", file=sys.stderr)
        return 2
    _log(event="start", mode=MODE, upstream=UPSTREAM, port=PORT)
    ThreadingHTTPServer(("127.0.0.1", PORT), Proxy).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
