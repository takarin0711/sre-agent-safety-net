"""PoC 検証用の最小エージェント。

エージェントが知っているのは論理モデル名だけであり、リージョンも実モデル ID も
LiteLLM Proxy 側の関心事とする。フェイルオーバーの有無によってこのファイルを
変更しないことが Phase 1 の判定基準3（agents/ のコード差分ゼロ）の根拠になる。

経路は 2 つ。**--prompt は Phase 1 のまま変更しない**（エビデンスの再現性を保つため）。

    python -m agents.main --prompt "hello"                 # 第2層を通らない（Phase 1）
    python -m agents.main --prompt "hello" --repeat 20 --interval 15   # F-05 用
    python -m agents.main --incident "本番の payment-api の Pod が CrashLoopBackOff"
                                                           # 第2層を通る（Phase 2）

--incident の終了コード: 検証成功 0 / ValidationError・呼び出し失敗 1（判定基準3）。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid

from dotenv import load_dotenv
from openai import OpenAI
from pydantic_core import ValidationError

from agents.log import log
from agents.prompts import build_incident_messages
from agents.validation import validate_action

# エージェントから見える唯一のモデル名。実デプロイの解決は LiteLLM に委ねる。
LOGICAL_MODEL = "sre-agent-model"


def litellm_headers(headers: object) -> dict[str, str]:
    """レスポンスヘッダのうち LiteLLM が付与したものだけを抜き出す。

    どのデプロイ（リージョン）が応答したかを特定する手がかりになる。
    R-01 では「第1層が何も検知していない」ことのエビデンスになる。
    """
    try:
        items = headers.items()  # type: ignore[attr-defined]
    except AttributeError:
        return {}
    return {k: v for k, v in items if k.lower().startswith("x-litellm")}


def call_llm(
    client: OpenAI, messages: list[dict[str, str]], attempt: int
) -> tuple[str | None, bool]:
    """1 リクエスト分の呼び出しとログ出力。

    戻り値は (content, ok)。ok=False のときは第1層の呼び出し自体が失敗している。
    """
    request_id = str(uuid.uuid4())
    started = time.monotonic()

    log(event="request.start", request_id=request_id, attempt=attempt, model=LOGICAL_MODEL)

    try:
        # with_raw_response を使い、ヘッダも観測できるようにする。
        raw = client.chat.completions.with_raw_response.create(
            model=LOGICAL_MODEL,
            messages=messages,  # type: ignore[arg-type]
        )
    except Exception as exc:
        # ここで握り潰さないことが判定基準4（Phase 1 の N-01）の検証対象そのもの。
        log(
            event="request.error",
            request_id=request_id,
            attempt=attempt,
            elapsed_sec=round(time.monotonic() - started, 3),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return None, False

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
    return content, True


def call_once(client: OpenAI, prompt: str, attempt: int) -> int:
    """Phase 1 の経路。第2層を通さず、呼び出しの成否だけを見る。"""
    _, ok = call_llm(client, [{"role": "user", "content": prompt}], attempt)
    return 0 if ok else 1


def call_incident_once(client: OpenAI, incident: str, attempt: int) -> int:
    """Phase 2 の経路。第1層の出力を第2層 (JSON Validator) に通す。"""
    content, ok = call_llm(client, build_incident_messages(incident), attempt)
    if not ok:
        return 1

    try:
        # 例外は握り潰さない。第2層の失敗は検知してハンドオフすることが検証対象。
        # ValidationError の内容とエスカレーションイベントは validate_action() が出力する。
        validate_action(content, original_request=incident)
    except ValidationError:
        return 1
    return 0


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="SRE エージェント（PoC 検証用）")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prompt", help="LLM に送るプロンプト（第2層を通らない Phase 1 の経路）")
    mode.add_argument("--incident", help="インシデント記述（第2層を通る Phase 2 の経路）")
    parser.add_argument("--repeat", type=int, default=1, help="繰り返し回数（F-05 / L-01 用）")
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
        if args.incident is not None:
            failures += call_incident_once(client, args.incident, attempt)
        else:
            failures += call_once(client, args.prompt, attempt)
        if attempt < args.repeat and args.interval > 0:
            time.sleep(args.interval)

    log(event="run.finished", total=args.repeat, failures=failures)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
