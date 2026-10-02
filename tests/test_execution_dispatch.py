"""実行層の単体検証（phase5 design.md 4.1 / シナリオ K-01〜K-07）。

**クラスタに触らない。** 判定基準1 は「実行層のディスパッチと結果分類がクラスタ無しで
決定的に検証できること」なので、API クライアントはフェイクに差し替える。
実機の確認は R 系 / Z 系 / X 系で別途行う。

ここで固めるのは 2 つ。

1. ``operation × kind`` から**どの API が、どの引数で**呼ばれるか（K-01〜K-03）
2. 返ってきたもの（または返ってこなかったこと）を**どう分類するか**（K-04 / K-05。D-028）
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import urllib3
from kubernetes.client.exceptions import ApiException

from agents import k8s
from agents.execution import execute
from agents.schemas import Environment, Operation, ProposedAction
from tests.fakes import FakeApis

TRACE = "trace-exec-1"

# operation × kind -> 期待される API メソッド名（design.md 3.2 の表の機械可読版）。
EXPECTED: dict[tuple[Operation, str], str] = {
    (Operation.GET, "pod"): "read_namespaced_pod",
    (Operation.GET, "deployment"): "read_namespaced_deployment",
    (Operation.GET, "statefulset"): "read_namespaced_stateful_set",
    (Operation.GET, "pvc"): "read_namespaced_persistent_volume_claim",
    (Operation.GET, "node"): "read_node",
    (Operation.DESCRIBE, "pod"): "list_namespaced_event",
    (Operation.DESCRIBE, "deployment"): "list_namespaced_event",
    (Operation.DESCRIBE, "statefulset"): "list_namespaced_event",
    (Operation.DESCRIBE, "pvc"): "list_namespaced_event",
    (Operation.LOGS, "pod"): "read_namespaced_pod_log",
    (Operation.ROLLOUT_RESTART, "deployment"): "patch_namespaced_deployment",
    (Operation.ROLLOUT_RESTART, "statefulset"): "patch_namespaced_stateful_set",
    (Operation.SCALE, "deployment"): "patch_namespaced_deployment_scale",
    (Operation.SCALE, "statefulset"): "patch_namespaced_stateful_set_scale",
    (Operation.DELETE_POD, "pod"): "delete_namespaced_pod",
    (Operation.DELETE_PVC, "pvc"): "delete_namespaced_persistent_volume_claim",
}

# 読み出しには dry-run のパラメータが API 側に存在しない（design.md 3.3）。
READ_ONLY = {Operation.GET, Operation.DESCRIBE, Operation.LOGS}


def action(operation: Operation, kind: str, *, dry_run: bool = True, replicas: int | None = None) -> ProposedAction:
    """第2層の入口を通して作る。**`model_validate_json()` を使う**（strict が効くのはこちら）。"""
    payload = {
        "action_id": "act-k",
        "environment": Environment.DEVELOPMENT.value,
        "operation": operation.value,
        "target": {"kind": kind, "name": "payment-api", "namespace": "dev"},
        "reason": "単体検証",
        "dry_run": dry_run,
    }
    if replicas is not None:
        payload["replicas"] = replicas
    return ProposedAction.model_validate_json(json.dumps(payload))


# ── K-01 ディスパッチ ─────────────────────────────────────


@pytest.mark.parametrize(("key", "method"), sorted(EXPECTED.items(), key=lambda kv: str(kv[0])))
def test_k01_every_supported_combination_reaches_its_api(key, method) -> None:
    operation, kind = key
    apis = FakeApis()
    outcome = execute(action(operation, kind, replicas=2 if operation is Operation.SCALE else None), TRACE, apis=apis)
    assert outcome.status == "executed"
    assert method in apis.methods


def test_k01_dispatch_table_and_expectations_agree() -> None:
    """表の内容そのものを固定する。**黙って増減していないこと**を見る。"""
    assert set(k8s.DISPATCH) == set(EXPECTED)


# ── K-02 表に無い組み合わせ ───────────────────────────────


@pytest.mark.parametrize(
    ("operation", "kind"),
    [
        (Operation.SCALE, "pod"),
        (Operation.DELETE_POD, "deployment"),
        (Operation.DELETE_PVC, "pod"),
        (Operation.LOGS, "deployment"),
        (Operation.ROLLOUT_RESTART, "pvc"),
        (Operation.DESCRIBE, "node"),
    ],
)
def test_k02_unsupported_combination_never_calls_the_api(operation, kind) -> None:
    """**API を呼ぶ前に落ちる。** 送っていないので「実行されていない」が確定している。"""
    apis = FakeApis()
    outcome = execute(action(operation, kind, replicas=1), TRACE, apis=apis)
    assert outcome.status == "execution_failed"
    assert outcome.http_status is None
    assert apis.calls == []


# ── K-03 dry-run ─────────────────────────────────────────


@pytest.mark.parametrize(("key", "method"), sorted(EXPECTED.items(), key=lambda kv: str(kv[0])))
def test_k03_dry_run_is_passed_only_to_mutating_calls(key, method) -> None:
    """``dry_run: true`` は **server-side dry-run** に対応づく（design.md 3.3）。

    クライアント側で「送らない」のでは、認可も検証も通らず何も確かめたことにならない。
    """
    operation, kind = key
    apis = FakeApis()
    execute(action(operation, kind, dry_run=True, replicas=2 if operation is Operation.SCALE else None), TRACE, apis=apis)
    kwargs = apis.calls[-1][2]
    if operation in READ_ONLY:
        assert "dry_run" not in kwargs
    else:
        assert kwargs["dry_run"] == k8s.DRY_RUN_ALL


def test_k03_dry_run_false_sends_no_dry_run_parameter() -> None:
    apis = FakeApis()
    execute(action(Operation.DELETE_POD, "pod", dry_run=False), TRACE, apis=apis)
    assert "dry_run" not in apis.last()[2]


def test_k03_timeout_is_always_passed() -> None:
    """タイムアウトを渡し忘れると、無応答の実行が永久に返らない（unknown にすら落ちない）。"""
    apis = FakeApis()
    execute(action(Operation.GET, "pod"), TRACE, apis=apis, timeout=1.5)
    assert apis.last()[2]["_request_timeout"] == 1.5


# ── K-04 / K-05 結果の分類（D-028）──────────────────────


def api_error(status: int) -> ApiException:
    exc = ApiException(status=status, reason="fake")
    exc.body = json.dumps({"kind": "Status", "code": status})
    return exc


@pytest.mark.parametrize(
    ("raises", "expected"),
    [
        (api_error(400), "execution_failed"),
        (api_error(401), "execution_failed"),
        (api_error(403), "execution_failed"),
        (api_error(404), "execution_failed"),
        (api_error(409), "execution_failed"),
        (api_error(422), "execution_failed"),
        (api_error(429), "execution_failed"),
        # ここから下は**実行されたかどうかが分からない**側。
        (api_error(500), "unknown"),
        (api_error(502), "unknown"),
        (api_error(503), "unknown"),
        (ApiException(status=0, reason="SSLError"), "unknown"),
    ],
)
def test_k04_http_status_decides_the_classification(raises, expected) -> None:
    """**分類はサーバからの応答があったかだけで決まる。** 操作の種類では決まらない。"""
    apis = FakeApis(raises=raises)
    outcome = execute(action(Operation.DELETE_POD, "pod", dry_run=False), TRACE, apis=apis)
    assert outcome.status == expected


@pytest.mark.parametrize(
    "raises",
    [
        urllib3.exceptions.ReadTimeoutError(None, "https://k8s", "read timed out"),
        urllib3.exceptions.MaxRetryError(None, "https://k8s"),
        urllib3.exceptions.ProtocolError("connection aborted"),
        ConnectionRefusedError("connection refused"),
        OSError("network is unreachable"),
    ],
)
def test_k05_no_response_is_always_unknown(raises) -> None:
    """**接続拒否も unknown に倒す**（D-028）。

    「拒否されたのだから届いていない」は多くの場合正しいが、クライアントは
    「接続前の失敗」と「送信後に切られた」を常に区別できない。
    過剰に unknown に倒すコストは人間が確認するだけで、逆の誤りは二重実行である。
    """
    apis = FakeApis(raises=raises)
    outcome = execute(action(Operation.DELETE_PVC, "pvc", dry_run=False), TRACE, apis=apis)
    assert outcome.status == "unknown"


def test_k05_unexpected_exception_is_also_unknown() -> None:
    apis = FakeApis(raises=RuntimeError("想定していない何か"))
    outcome = execute(action(Operation.GET, "pod"), TRACE, apis=apis)
    assert outcome.status == "unknown"


def test_k05_missing_credentials_is_a_determinate_failure() -> None:
    """資格情報が無いのは**構成の不足**であって、実行の不確実性ではない。

    ``~/.kube/config`` へフォールバックしないことの確認も兼ねる。フォールバックすると
    kind の admin が拾われ、RBAC で止まるはずの操作が全部通ってしまう。
    """
    outcome = execute(action(Operation.GET, "pod"), TRACE)
    assert outcome.status == "execution_failed"
    assert "K8S_KUBECONFIG" in outcome.detail


# ── K-06 / K-07 層の境界が実装にも現れていること ────────────


@pytest.mark.parametrize("module", ["k8s.py", "execution.py", "executor.py"])
def test_k06_no_policy_vocabulary_in_the_execution_layer(module: str) -> None:
    """実行層はポリシーを持たない（D-002 / D-021）。

    「本番なら確認を強める」「破壊的操作なら分岐する」を書いた時点で、
    第3層の判定が実行層にも複製される。Phase 3 の判定基準7 と同じ検査を実行層にかける。
    """
    source = (Path(__file__).parent.parent / "agents" / module).read_text(encoding="utf-8")
    leaked = [v.value for v in (*Environment, *Operation) if v.value in source]
    assert leaked == [], f"{module} にポリシーの語彙が漏れている: {leaked}"


def test_k07_the_slack_receiver_cannot_reach_the_execution_layer() -> None:
    """**受信プロセスは実行できない**（D-027。判定基準6 の構造側）。

    「承認ボタンを押せる = handler の権限で実行できる」という結合が無いことを、
    運用（kubeconfig を渡さない）だけでなく**コードの到達可能性**で固定する。
    """
    import slack.handler  # noqa: F401  （import する事実そのものが検査対象）

    forbidden = {"agents.execution", "agents.k8s", "agents.executor", "kubernetes"}
    seen: set[str] = set()

    def walk(name: str) -> None:
        import sys

        if name in seen:
            return
        seen.add(name)
        module = sys.modules.get(name)
        if module is None:
            return
        for attr in vars(module).values():
            target = getattr(attr, "__module__", None) or getattr(attr, "__name__", None)
            if isinstance(target, str) and target.split(".")[0] in {"agents", "slack", "kubernetes"}:
                walk(target)

    walk("slack.handler")
    leaked = sorted(m for m in seen if any(m == f or m.startswith(f + ".") for f in forbidden))
    assert leaked == [], f"slack/handler.py から実行層に到達できる: {leaked}"
