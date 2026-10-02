"""実行層のディスパッチ（phase5 design.md 3.2 / D-030）。

``operation × kind`` から Kubernetes API 呼び出しへの対応を**表**で持つ。
`kubectl` のサブプロセスは起動しない。理由は失敗の分類を文字列解析に依存させないこと
（D-028 の 3 分類が成立する前提）。

ここが守る規律:

- **ポリシーの語彙を書かない。** 環境名や操作名の**リテラル**による分岐をこのファイルに
  書いてはならない（D-002 / D-021）。語彙が 1 つでもこのファイルに現れたら
  pytest が落ちる（K-06）ので、**コメントでも書かない**。実行層が知ってよいのは
  「この operation と kind なら、どの API を、どの引数で呼ぶか」だけである。
- **表に無い組み合わせは呼ばない。** 第2層の ``Operation`` と ``TargetResource.kind`` は
  どちらも閉じた語彙だが、その直積のすべてが意味を持つわけではない
  （スケール操作に Pod を指定する、といった組み合わせ）。
  表に無いものは API を呼ばずに ``UnsupportedTarget`` にする（＝確定的な失敗。実行していない）。
- **資格情報は明示的に渡されたものだけを使う。** ``~/.kube/config`` へのフォールバックを
  絶対に許さない（下の ``load_api_client()`` を参照）。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from kubernetes import client as k8s_client
from kubernetes import config as k8s_config

from agents.schemas import Operation, ProposedAction

# 実行層のタイムアウト。**この値が unknown の発生率を直接決める**（design.md 4.6 L-01）。
# 短すぎると、実行されたものを「不明」と記録する。初期値は L-01 で実測して確定する。
DEFAULT_TIMEOUT_SECONDS = 10.0

# server-side dry-run。クライアント側で「送らない」のでは何も検証したことにならないため、
# API サーバまで届けて認可と検証を通し、永続化だけさせない（design.md 3.3）。
DRY_RUN_ALL = "All"

RESTART_ANNOTATION = "kubectl.kubernetes.io/restartedAt"


class UnsupportedTarget(RuntimeError):
    """表に無い operation × kind の組み合わせ。**API を呼ぶ前**に落ちる。"""


class CredentialsNotConfigured(RuntimeError):
    """実行に使う kubeconfig が指定されていない。"""


@dataclass(frozen=True)
class Apis:
    """実行層が触る API クライアント一式。テストから差し替えられるよう束ねて持つ。"""

    core: k8s_client.CoreV1Api
    apps: k8s_client.AppsV1Api


def load_api_client(kubeconfig: str | None = None, context: str | None = None) -> Apis:
    """kubeconfig を**明示的に**読み込む。

    **``load_kube_config()`` を引数なしで呼んではならない。** 引数を省くと
    ``~/.kube/config`` にフォールバックし、kind の **admin** コンテキストが拾われる。
    そうなると RBAC で止まるはずの操作が全部通り、判定基準5（多層防御の最後の 1 枚）が
    静かに無意味になる。**資格情報は設定であって既定値ではない。**

    環境変数は ``K8S_KUBECONFIG``。標準の ``KUBECONFIG`` を使わないのは、
    シェルに残った ``KUBECONFIG`` が意図せず admin を渡すのを防ぐため。
    """
    path = kubeconfig or os.environ.get("K8S_KUBECONFIG")
    if not path:
        raise CredentialsNotConfigured(
            "K8S_KUBECONFIG が未設定です（~/.kube/config へはフォールバックしません）"
        )
    k8s_config.load_kube_config(
        config_file=path, context=context or os.environ.get("K8S_CONTEXT") or None
    )
    return Apis(core=k8s_client.CoreV1Api(), apps=k8s_client.AppsV1Api())


def timeout_from_env() -> float:
    raw = os.environ.get("EXECUTION_TIMEOUT_SECONDS")
    # 設定ミスを黙って既定値に落とさない（例外は握り潰さないのが本 PoC の規律）。
    return DEFAULT_TIMEOUT_SECONDS if not raw else float(raw)


# ── 個々の呼び出し ────────────────────────────────────────
#
# どれも「何を呼んだか」を人間が読める 1 行で返す。戻り値はログと監査に載せるだけで、
# 判定には使わない（判定は HTTP ステータスの有無だけで行う。D-028）。

_Call = Callable[[Apis, ProposedAction, dict], str]


def _dry(action: ProposedAction) -> dict:
    """変更系に付ける dry-run の引数。参照系には API 側にパラメータが無い（design.md 3.3）。"""
    return {"dry_run": DRY_RUN_ALL} if action.dry_run else {}


def _read_pod(apis: Apis, a: ProposedAction, kw: dict) -> str:
    pod = apis.core.read_namespaced_pod(a.target.name, a.target.namespace, **kw)
    return f"pod {a.target.namespace}/{a.target.name} phase={pod.status.phase} uid={pod.metadata.uid}"


def _read_deployment(apis: Apis, a: ProposedAction, kw: dict) -> str:
    d = apis.apps.read_namespaced_deployment(a.target.name, a.target.namespace, **kw)
    return (
        f"deployment {a.target.namespace}/{a.target.name} "
        f"generation={d.metadata.generation} replicas={d.spec.replicas}"
    )


def _read_statefulset(apis: Apis, a: ProposedAction, kw: dict) -> str:
    s = apis.apps.read_namespaced_stateful_set(a.target.name, a.target.namespace, **kw)
    return (
        f"statefulset {a.target.namespace}/{a.target.name} "
        f"generation={s.metadata.generation} replicas={s.spec.replicas}"
    )


def _read_pvc(apis: Apis, a: ProposedAction, kw: dict) -> str:
    p = apis.core.read_namespaced_persistent_volume_claim(
        a.target.name, a.target.namespace, **kw
    )
    return f"pvc {a.target.namespace}/{a.target.name} phase={p.status.phase}"


def _read_node(apis: Apis, a: ProposedAction, kw: dict) -> str:
    """node はクラスタスコープ。``target.namespace`` は API 側で意味を持たない。

    第2層のスキーマが node にも namespace を要求している（``TargetResource``）ため、
    **申告された namespace が判定にだけ使われて実行では無視される**という非対称が生じる。
    スキーマは変えない（Phase 2・3 の fixture とエビデンスが無効になる）。限界として記録する。
    """
    node = apis.core.read_node(a.target.name, **kw)
    return (
        f"node {node.metadata.name} "
        f"(namespace={a.target.namespace} は API では無視される)"
    )


def _describe(reader: _Call) -> _Call:
    """describe 系操作の代替（design.md 3.2 / 未決事項3）。

    **describe に対応する API は存在しない。** kubectl がリソースと Event を取得して
    整形するクライアント側の機能である。ここでは read + Event 取得で代替する。
    語彙が kubectl のサブコマンド名に引きずられていることの実例として記録する。
    """

    def call(apis: Apis, a: ProposedAction, kw: dict) -> str:
        summary = reader(apis, a, kw)
        events = apis.core.list_namespaced_event(
            a.target.namespace,
            field_selector=f"involvedObject.name={a.target.name}",
            **kw,
        )
        return f"{summary} events={len(events.items)}"

    return call


def _read_logs(apis: Apis, a: ProposedAction, kw: dict) -> str:
    text = apis.core.read_namespaced_pod_log(
        a.target.name, a.target.namespace, tail_lines=20, **kw
    )
    return f"pod/log {a.target.namespace}/{a.target.name} lines={len(text.splitlines())}"


def _restart_body() -> dict:
    """`kubectl rollout restart` と同じ形。Pod テンプレートの注釈を更新して置き換えを誘発する。"""
    return {
        "spec": {
            "template": {
                "metadata": {"annotations": {RESTART_ANNOTATION: datetime.now(UTC).isoformat()}}
            }
        }
    }


def _restart_deployment(apis: Apis, a: ProposedAction, kw: dict) -> str:
    d = apis.apps.patch_namespaced_deployment(
        a.target.name, a.target.namespace, _restart_body(), **_dry(a), **kw
    )
    return f"rollout restart deployment {a.target.namespace}/{a.target.name} generation={d.metadata.generation}"


def _restart_statefulset(apis: Apis, a: ProposedAction, kw: dict) -> str:
    s = apis.apps.patch_namespaced_stateful_set(
        a.target.name, a.target.namespace, _restart_body(), **_dry(a), **kw
    )
    return f"rollout restart statefulset {a.target.namespace}/{a.target.name} generation={s.metadata.generation}"


def _scale_deployment(apis: Apis, a: ProposedAction, kw: dict) -> str:
    s = apis.apps.patch_namespaced_deployment_scale(
        a.target.name, a.target.namespace, {"spec": {"replicas": a.replicas}}, **_dry(a), **kw
    )
    return f"scale deployment {a.target.namespace}/{a.target.name} replicas={s.spec.replicas}"


def _scale_statefulset(apis: Apis, a: ProposedAction, kw: dict) -> str:
    s = apis.apps.patch_namespaced_stateful_set_scale(
        a.target.name, a.target.namespace, {"spec": {"replicas": a.replicas}}, **_dry(a), **kw
    )
    return f"scale statefulset {a.target.namespace}/{a.target.name} replicas={s.spec.replicas}"


def _delete_pod(apis: Apis, a: ProposedAction, kw: dict) -> str:
    apis.core.delete_namespaced_pod(a.target.name, a.target.namespace, **_dry(a), **kw)
    return f"delete pod {a.target.namespace}/{a.target.name} dry_run={a.dry_run}"


def _delete_pvc(apis: Apis, a: ProposedAction, kw: dict) -> str:
    apis.core.delete_namespaced_persistent_volume_claim(
        a.target.name, a.target.namespace, **_dry(a), **kw
    )
    return f"delete pvc {a.target.namespace}/{a.target.name} dry_run={a.dry_run}"


# ── ディスパッチ表 ────────────────────────────────────────
#
# **ここに無い組み合わせは実行しない。** 表そのものが allow-list であり、
# 「意味を持つ組み合わせ」の定義が 1 箇所に集まる。

DISPATCH: dict[tuple[Operation, str], _Call] = {
    (Operation.GET, "pod"): _read_pod,
    (Operation.GET, "deployment"): _read_deployment,
    (Operation.GET, "statefulset"): _read_statefulset,
    (Operation.GET, "pvc"): _read_pvc,
    (Operation.GET, "node"): _read_node,
    (Operation.DESCRIBE, "pod"): _describe(_read_pod),
    (Operation.DESCRIBE, "deployment"): _describe(_read_deployment),
    (Operation.DESCRIBE, "statefulset"): _describe(_read_statefulset),
    (Operation.DESCRIBE, "pvc"): _describe(_read_pvc),
    (Operation.LOGS, "pod"): _read_logs,
    (Operation.ROLLOUT_RESTART, "deployment"): _restart_deployment,
    (Operation.ROLLOUT_RESTART, "statefulset"): _restart_statefulset,
    (Operation.SCALE, "deployment"): _scale_deployment,
    (Operation.SCALE, "statefulset"): _scale_statefulset,
    (Operation.DELETE_POD, "pod"): _delete_pod,
    (Operation.DELETE_PVC, "pvc"): _delete_pvc,
}

# node に describe を置いていないのは、Event が namespace 付きリソースのものであり、
# クラスタスコープの node に対して list_namespaced_event が噛み合わないため。
# 「表に無い＝実行しない」で安全側に倒す。


def invoke(apis: Apis, action: ProposedAction, *, timeout: float | None = None) -> str:
    """1 アクションを実行する。**例外はそのまま投げる**（分類は execution.py の責務）。

    ここで握り潰すと、呼び出し側は「成功したのか、失敗を無視したのか」を区別できなくなる。
    """
    call = DISPATCH.get((action.operation, action.target.kind))
    if call is None:
        raise UnsupportedTarget(
            f"operation={action.operation.value} と kind={action.target.kind} の"
            "組み合わせに対応する API がありません（実行していません）"
        )
    limit = timeout if timeout is not None else timeout_from_env()
    return call(apis, action, {"_request_timeout": limit})
