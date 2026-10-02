#!/usr/bin/env bash
# 検証用クラスタを作り直す（phase5 design.md 事前準備）。冪等。
#
#   ./k8s/setup.sh            # 作成（既にあれば作業だけやり直す）
#   ./k8s/setup.sh --recreate # 消してから作る
#
# **消せることが D-026 の前提である。** 壊すために作る環境なので、
# 迷ったら --recreate で作り直す。実クラスタには絶対に向けない（context 名を固定している）。
set -euo pipefail
cd "$(dirname "$0")/.."

CLUSTER=sre-safety-net
CONTEXT="kind-${CLUSTER}"

if [ "${1:-}" = "--recreate" ]; then
  kind delete cluster --name "$CLUSTER"
fi

if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  mkdir -p var/audit
  kind create cluster --config k8s/kind-cluster.yaml --wait 120s
fi

kubectl --context "$CONTEXT" apply -f k8s/workloads.yaml
kubectl --context "$CONTEXT" apply -f k8s/rbac.yaml
# トークンの発行は Secret 作成より少し遅れる。
for _ in $(seq 1 30); do
  [ -n "$(kubectl --context "$CONTEXT" get secret sre-agent-token -n sre-system -o jsonpath='{.data.token}' 2>/dev/null)" ] && break
  sleep 1
done
./k8s/make-kubeconfig.sh
kubectl --context "$CONTEXT" -n prod rollout status deploy/payment-api --timeout=120s
echo
echo "完了。executor に与える資格情報: var/kubeconfig-sre-agent.yaml"
