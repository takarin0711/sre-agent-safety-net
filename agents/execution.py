"""実行層（phase5 design.md 3.5 / D-026 / D-028）。

Phase 1〜4 は構造化ログを 1 行出すだけだった。Phase 5 からは**実際に Kubernetes API を発行する**。
対象は使い捨ての kind クラスタに限る（D-026）。実クラスタには向けない。

``agents/main.py``（自律経路）と ``agents/executor.py``（承認・再試行経路）の**両方から
同じ関数を呼ぶ**。エージェント本体が実行するときと、人間が承認して実行するときで
「実行」の意味が変わってはいけない。
``slack/handler.py`` からは呼ばない（D-027。受信プロセスは実行権限を持たない）。

ここが守る規律:

- **分類は「サーバからの応答があったか」だけで行う**（D-028）。
  2xx は実行された、4xx は実行されていない、5xx と応答なしは**分からない**。
- **分からないものを「失敗」に丸めない。** 丸めると非冪等な操作で二重実行を招く。
- **例外を握り潰さない。** 想定外の例外も安全側（unknown）に倒したうえで、型と内容を必ず残す。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

import urllib3
from kubernetes.client.exceptions import ApiException

from agents import k8s
from agents.log import log
from agents.schemas import ProposedAction

ExecutionStatus = Literal["executed", "execution_failed", "unknown"]

# 分類の根拠を人間が読める形で残す。判定そのものは status が持つ。
_SENT_BUT_NO_ANSWER = "応答が無いため、実行されたかどうかは判別できません"


@dataclass(frozen=True)
class ExecutionOutcome:
    """1 回の実行の結果。**実行の有無が分かるかどうか**まで含めて表す。"""

    status: ExecutionStatus
    detail: str
    http_status: int | None = None
    elapsed_sec: float = 0.0

    @property
    def executed(self) -> bool:
        return self.status == "executed"


def execute(
    action: ProposedAction,
    trace_id: str,
    *,
    apis: k8s.Apis | None = None,
    timeout: float | None = None,
    actor: str | None = None,
) -> ExecutionOutcome:
    """アクションを 1 件実行し、結果を 3 分類して返す。**例外は投げない**。

    呼び出し側（main / executor）は戻り値の ``status`` だけを見て次を決める。
    ``actor`` は承認経路でのみ渡す（自律経路との区別がログに残る）。
    """
    started = time.monotonic()
    base = {"trace_id": trace_id, "action": action.model_dump(mode="json")}
    if actor:
        base["actor"] = actor

    try:
        resolved = apis if apis is not None else k8s.load_api_client()
        summary = k8s.invoke(resolved, action, timeout=timeout)
    except k8s.UnsupportedTarget as exc:
        # API を呼ぶ前に落ちている。送っていないことが確定している。
        return _fail(base, started, detail=str(exc), http_status=None, reason="unsupported_target")
    except k8s.CredentialsNotConfigured as exc:
        # 構成の不足。これも送っていない。
        return _fail(base, started, detail=str(exc), http_status=None, reason="no_credentials")
    except ApiException as exc:
        status = int(exc.status or 0)
        detail = f"{status} {exc.reason}: {_body(exc)}"
        if 400 <= status < 500:
            # サーバが「処理しなかった」ことを応答で示している。実行されていない。
            return _fail(base, started, detail=detail, http_status=status, reason="http_4xx")
        # 5xx、および SSLError が変換された status=0。どちらも結果を保証しない。
        return _unknown(
            base, started, detail=f"{detail} / {_SENT_BUT_NO_ANSWER}", http_status=status or None,
            reason="http_5xx" if status >= 500 else "transport_error",
        )
    except (urllib3.exceptions.HTTPError, OSError) as exc:
        # タイムアウト・接続断。**接続拒否も unknown に倒す**（D-028）。
        # クライアントは「接続前の失敗」と「送信後に切られた」を常に区別できない。
        return _unknown(
            base, started,
            detail=f"{type(exc).__name__}: {exc} / {_SENT_BUT_NO_ANSWER}",
            http_status=None, reason="no_response",
        )
    except Exception as exc:  # noqa: BLE001 - 想定外も安全側に倒すが、握り潰さない
        return _unknown(
            base, started,
            detail=f"想定外の例外 {type(exc).__name__}: {exc} / {_SENT_BUT_NO_ANSWER}",
            http_status=None, reason="unexpected_exception",
        )

    elapsed = round(time.monotonic() - started, 3)
    log(event="execution.applied", **base, dry_run=action.dry_run, result=summary, elapsed_sec=elapsed)
    return ExecutionOutcome("executed", summary, http_status=None, elapsed_sec=elapsed)


def _fail(base: dict, started: float, *, detail: str, http_status: int | None, reason: str) -> ExecutionOutcome:
    """**実行されていないことが確定している**失敗。人間が原因を直して再試行できる。"""
    elapsed = round(time.monotonic() - started, 3)
    log(
        event="execution.failed",
        **base,
        http_status=http_status,
        classified_by=reason,
        executed="no",
        detail=detail,
        elapsed_sec=elapsed,
    )
    return ExecutionOutcome("execution_failed", detail, http_status=http_status, elapsed_sec=elapsed)


def _unknown(base: dict, started: float, *, detail: str, http_status: int | None, reason: str) -> ExecutionOutcome:
    """**実行されたかどうかが分からない**。自動では再試行しない（D-028）。"""
    elapsed = round(time.monotonic() - started, 3)
    log(
        event="execution.unknown",
        **base,
        http_status=http_status,
        classified_by=reason,
        executed="unknown",
        detail=detail,
        note="クラスタの実際の状態を人間が確認すること。自動再試行はしない",
        elapsed_sec=elapsed,
    )
    return ExecutionOutcome("unknown", detail, http_status=http_status, elapsed_sec=elapsed)


def _body(exc: ApiException) -> str:
    """API サーバの応答本文。403 の理由はここにしか入っていない。"""
    raw = (exc.body or "").strip()
    return raw[:500]
