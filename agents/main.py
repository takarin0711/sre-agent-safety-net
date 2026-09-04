"""Phase 1 検証用の最小エージェント。

エージェントが知っているのは論理モデル名だけであり、リージョンも実モデル ID も
LiteLLM Proxy 側の関心事とする。フェイルオーバーの有無によってこのファイルを
変更しないことが Phase 1 の判定基準3（agents/ のコード差分ゼロ）の根拠になる。

使い方:
    python -m agents.main --prompt "hello"
    python -m agents.main --prompt "hello" --repeat 20 --interval 15   # F-05 用
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid

from dotenv import load_dotenv
from openai import OpenAI

# エージェントから見える唯一のモデル名。実デプロイの解決は LiteLLM に委ねる。
LOGICAL_MODEL = "sre-agent-model"


def log(**fields: object) -> None:
    """構造化ログを 1 行 1 JSON で標準出力に書く（検証のエビデンスになる）。"""
    print(json.dumps(fields, ensure_ascii=False, default=str), flush=True)


def litellm_headers(headers: object) -> dict[str, str]:
    """レスポンスヘッダのうち LiteLLM が付与したものだけを抜き出す。

    どのデプロイ（リージョン）が応答したかを特定する手がかりになる。
    実際にどのヘッダが返るかは design.md 第6章の確認事項。
    """
    try:
        items = headers.items()  # type: ignore[attr-defined]
    except AttributeError:
        return {}
    return {k: v for k, v in items if k.lower().startswith("x-litellm")}


def call_once(client: OpenAI, prompt: str, attempt: int) -> int:
    """1 リクエスト分の呼び出しとログ出力。戻り値は終了コード。"""
    request_id = str(uuid.uuid4())
    started = time.monotonic()

    log(event="request.start", request_id=request_id, attempt=attempt, model=LOGICAL_MODEL)

    try:
        # with_raw_response を使い、ヘッダも観測できるようにする。
        raw = client.chat.completions.with_raw_response.create(
            model=LOGICAL_MODEL,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        # ここで握り潰さないことが判定基準4（N-01）の検証対象そのもの。
        log(
            event="request.error",
            request_id=request_id,
            attempt=attempt,
            elapsed_sec=round(time.monotonic() - started, 3),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return 1

    elapsed = round(time.monotonic() - started, 3)
    completion = raw.parse()
    content = completion.choices[0].message.content if completion.choices else None

    log(
        event="request.success",
        request_id=request_id,
        attempt=attempt,
        elapsed_sec=elapsed,
        response_model=completion.model,
        litellm_headers=litellm_headers(raw.headers),
        content_preview=(content or "")[:200],
    )
    return 0


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Phase 1 検証用エージェント")
    parser.add_argument("--prompt", required=True, help="LLM に送るプロンプト")
    parser.add_argument("--repeat", type=int, default=1, help="繰り返し回数（F-05 のフェイルバック観測用）")
    parser.add_argument("--interval", type=float, default=0.0, help="繰り返し間隔（秒）")
    args = parser.parse_args()

    base_url = os.environ.get("OPENAI_BASE_URL")
    api_key = os.environ.get("LITELLM_MASTER_KEY")
    if not base_url or not api_key:
        print("OPENAI_BASE_URL と LITELLM_MASTER_KEY を .env に設定してください", file=sys.stderr)
        return 2

    client = OpenAI(base_url=base_url, api_key=api_key)

    failures = 0
    for attempt in range(1, args.repeat + 1):
        failures += call_once(client, args.prompt, attempt)
        if attempt < args.repeat and args.interval > 0:
            time.sleep(args.interval)

    log(event="run.finished", total=args.repeat, failures=failures)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
