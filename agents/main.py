"""PoC 検証用の最小エージェント。

エージェントが知っているのは論理モデル名だけであり、リージョンも実モデル ID も
LiteLLM Proxy 側の関心事とする。フェイルオーバーの有無によってこのファイルを
変更しないことが Phase 1 の判定基準3（agents/ のコード差分ゼロ）の根拠になる。

経路は 2 つ。**--prompt は Phase 1 のまま変更しない**（エビデンスの再現性を保つため）。

    python -m agents.main --prompt "hello"                 # 第2層を通らない（Phase 1）
    python -m agents.main --prompt "hello" --repeat 20 --interval 15   # F-05 用
    python -m agents.main --incident "本番の payment-api の Pod が CrashLoopBackOff"
                                                           # 第2層 → 第3層（Phase 3）
    python -m agents.main --incident "..." --no-policy      # 第2層で止める（Phase 2 の経路）
    python -m agents.main --incident "..." --no-slack       # Slack へ送出しない（Phase 3 の経路）

--incident の終了コードは分岐を一意に表す（phase3 design.md 3.8）。

    0  allow: true → 実行層（dry-run）まで到達
    1  第1層の呼び出し失敗（通知パターン1 の代替）、または第2層の ValidationError
       （パターン2）。**どちらも Slack へ出す。** 分けていないのは、どちらも
       「実行していないことが確定しており、重さが同じ」ためである（D-031）
    2  環境変数の設定不足
    3  第3層が allow: false（Handoff 仕様書の通知パターン3）
    4  第3層が評価不能・Fail-Closed（同 パターン4）
    5  エスカレーションの送出に失敗した（遮断は成立しているが人間に届いていない）
    6  実行を試みて確定的に失敗した（4xx。実行されていないことが分かっている）
    7  実行の有無が不明（5xx / 応答なし）。**もっとも重い**
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from dataclasses import dataclass

from dotenv import load_dotenv
from openai import OpenAI
from pydantic_core import ValidationError

from agents import escalation
from agents.execution import execute
from agents.log import log
from agents.policy import PolicyUnavailableError, evaluate_action
from agents.prompts import build_incident_messages
from agents.validation import validate_action

# エージェントから見える唯一のモデル名。実デプロイの解決は LiteLLM に委ねる。
LOGICAL_MODEL = "sre-agent-model"

LAYER1 = "layer1_litellm"


@dataclass(frozen=True)
class RequestFailure:
    """第1層の呼び出しが失敗したときに、エージェントが**実際に持っている**情報。

    これ以上のこと（どのデプロイが落ちたか・残りのリージョンが生きているか）は
    LiteLLM の内側にあり、ここからは見えない。通知の文面をこの型だけから組み立てることで、
    持っていない情報を書けない構造にする（D-031）。
    """

    error_type: str
    error: str


# 終了コード。シナリオ実行スクリプトが分岐を一意に判定できるようにする。
# 値が大きいほど重い（--repeat では最も重いものを返す）。
EXIT_OK = 0
EXIT_LAYER1_OR_2_FAILED = 1
EXIT_CONFIG_ERROR = 2
EXIT_POLICY_DENIED = 3
EXIT_POLICY_UNAVAILABLE = 4
# 遮断そのものは成立しているが、人間に届いていない（docs/decisions.md D-022）。
# 多層防御でもっとも危険な失敗は「止めたことを誰も知らない」ことなので、3 / 4 と区別する。
EXIT_ESCALATION_FAILED = 5
# 実行を試みた後の失敗。3〜5 は「実行していない」ことが確定している状態なので、それより重い。
# 7 を最重とするのは、**唯一「いまの状態が分からない」状態だから**である（D-028）。
EXIT_EXECUTION_FAILED = 6
EXIT_EXECUTION_UNKNOWN = 7


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
    client: OpenAI,
    messages: list[dict[str, str]],
    attempt: int,
    trace_id: str | None = None,
) -> tuple[str | None, RequestFailure | None]:
    """1 リクエスト分の呼び出しとログ出力。

    戻り値は (content, failure)。failure が None でないときは第1層の呼び出し自体が
    失敗している。**ログ出力は Phase 1 から 1 バイトも変えていない**（戻り値だけが増えた）。

    trace_id は --incident 経路でのみ渡す。--prompt 経路（Phase 1）のログ出力を
    1 バイトも変えないため、None のときはフィールドごと出さない。
    """
    request_id = str(uuid.uuid4())
    started = time.monotonic()
    trace = {"trace_id": trace_id} if trace_id else {}

    log(event="request.start", **trace, request_id=request_id, attempt=attempt, model=LOGICAL_MODEL)

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
            **trace,
            request_id=request_id,
            attempt=attempt,
            elapsed_sec=round(time.monotonic() - started, 3),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return None, RequestFailure(error_type=type(exc).__name__, error=str(exc))

    elapsed = round(time.monotonic() - started, 3)
    completion = raw.parse()
    content = completion.choices[0].message.content if completion.choices else None

    log(
        event="request.success",
        **trace,
        request_id=request_id,
        attempt=attempt,
        elapsed_sec=elapsed,
        response_model=completion.model,
        litellm_headers=litellm_headers(raw.headers),
        content_preview=(content or "")[:200],
    )
    return content, None


def call_once(client: OpenAI, prompt: str, attempt: int) -> int:
    """Phase 1 の経路。第2層を通さず、呼び出しの成否だけを見る。

    **この経路では通知しない。** インシデントが無いので「何が処理されなかったか」を
    書けず、Trace ID も発行していない（D-031）。
    """
    _, failure = call_llm(client, [{"role": "user", "content": prompt}], attempt)
    return EXIT_OK if failure is None else EXIT_LAYER1_OR_2_FAILED


def call_incident_once(
    client: OpenAI, incident: str, attempt: int, *, skip_policy: bool = False
) -> int:
    """第1層 → 第2層 → 第3層 → 実行層（dry-run）。戻り値は終了コード。

    1 インシデント処理 = 1 Trace ID。全層のログ・OPA の Decision Log・
    エスカレーションイベントを貫く相関 ID になる（docs/decisions.md D-016）。
    """
    trace_id = str(uuid.uuid4())
    code = _run_layers(client, incident, attempt, trace_id, skip_policy=skip_policy)
    if escalation.delivery_failed(trace_id):
        # 遮断の成立（3 / 4）と「人間に届いていない」を区別する（D-022）。
        # 実行まで到達して不明に倒れた場合（7）は、届かなかったことより重い状態なので
        # 大きいほうを返す（「大きいほど重い」の規律を保つ）。
        return max(code, EXIT_ESCALATION_FAILED)
    return code


def _run_layers(
    client: OpenAI, incident: str, attempt: int, trace_id: str, *, skip_policy: bool
) -> int:
    """第1層 → 第2層 → 第3層 → 実行層。戻り値は送出の成否を含まない終了コード。"""
    content, failure = call_llm(client, build_incident_messages(incident), attempt, trace_id)
    if failure is not None:
        # 通知パターン1 の**代替**を出す（D-031）。「どのエンドポイントがどう落ちたか」は
        # 依然として書けないが、「このインシデントが処理されなかった」ことは書ける。
        # 第1層が落ちているとき、それを人間に伝えられる層は他に無い。
        handoff = escalation.build_request_failed_event(
            failure.error_type,
            failure.error,
            trace_id=trace_id,
            original_request=incident,
        )
        # 他層と違い、既存の層のログ行（request.error）は call_llm が**既に出している**。
        # Phase 1 のエビデンスを変えないため、そこには手を入れず別の行として足す。
        escalation.escalate(
            handoff,
            event="request.escalated",
            layer=LAYER1,
            trace_id=trace_id,
            escalation=handoff.model_dump(mode="json"),
        )
        return EXIT_LAYER1_OR_2_FAILED

    try:
        # 例外は握り潰さない。第2層の失敗は検知してハンドオフすることが検証対象。
        # ValidationError の内容とエスカレーションイベントは validate_action() が出力する。
        action = validate_action(content, original_request=incident, trace_id=trace_id)
    except ValidationError:
        return EXIT_LAYER1_OR_2_FAILED

    if skip_policy:
        # Phase 2 の経路を再現するためのフラグ。第3層には問い合わせない。
        return EXIT_OK

    try:
        # 第2層を通過したものだけを第3層に渡す。構造未保証のデータを Rego に渡すと、
        # 欠落したフィールドを参照するルールが undefined になって発火せず、
        # 違反ゼロ = allow: true に化ける（design.md 2.2 / N-02）。
        decision = evaluate_action(action, trace_id=trace_id, original_request=incident)
    except PolicyUnavailableError:
        # Fail-Closed。評価できない以上、実行には進まない（D-015）。
        return EXIT_POLICY_UNAVAILABLE

    if not decision.allow:
        # 承認は待たない。イベントを出して終了する（Handoff 仕様書「App は待機せず解放」）。
        return EXIT_POLICY_DENIED

    # 実行層。Phase 5 から**実際に Kubernetes API を発行する**（対象は kind のみ。D-026）。
    outcome = execute(action, trace_id)
    if outcome.executed:
        return EXIT_OK
    # 止めたのではなく、通したうえで失敗した。人間に渡す形が層の失敗とは違う（通知パターン5）。
    escalation.escalate_autonomous_execution(action, trace_id, incident, outcome)
    return (
        EXIT_EXECUTION_FAILED
        if outcome.status == "execution_failed"
        else EXIT_EXECUTION_UNKNOWN
    )


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="SRE エージェント（PoC 検証用）")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prompt", help="LLM に送るプロンプト（第2層を通らない Phase 1 の経路）")
    mode.add_argument("--incident", help="インシデント記述（第2層 → 第3層を通る経路）")
    parser.add_argument(
        "--no-policy",
        action="store_true",
        help="第3層に問い合わせず第2層で止める（Phase 2 の経路の再現用）",
    )
    parser.add_argument(
        "--no-slack",
        action="store_true",
        help="エスカレーションを Slack へ送出しない（Phase 3 までの経路の再現用）",
    )
    parser.add_argument("--repeat", type=int, default=1, help="繰り返し回数（F-05 / L-01 用）")
    parser.add_argument("--interval", type=float, default=0.0, help="繰り返し間隔（秒）")
    args = parser.parse_args()

    base_url = os.environ.get("OPENAI_BASE_URL")
    api_key = os.environ.get("LITELLM_MASTER_KEY")
    if not base_url or not api_key:
        print("OPENAI_BASE_URL と LITELLM_MASTER_KEY を .env に設定してください", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    if args.no_slack:
        escalation.set_enabled(False)

    client = OpenAI(base_url=base_url, api_key=api_key)

    codes: list[int] = []
    for attempt in range(1, args.repeat + 1):
        if args.incident is not None:
            codes.append(
                call_incident_once(client, args.incident, attempt, skip_policy=args.no_policy)
            )
        else:
            codes.append(call_once(client, args.prompt, attempt))
        if attempt < args.repeat and args.interval > 0:
            time.sleep(args.interval)

    failures = sum(1 for code in codes if code != EXIT_OK)
    log(event="run.finished", total=args.repeat, failures=failures)
    # --repeat では最も重い結果を返す（7 > 6 > 5 > 4 > 3 > 1 > 0）。
    return max(codes, default=EXIT_OK)


if __name__ == "__main__":
    raise SystemExit(main())
