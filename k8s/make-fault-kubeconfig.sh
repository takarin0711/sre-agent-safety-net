#!/usr/bin/env bash
# 実行層の障害注入用の kubeconfig と CA を var/ に出す（phase5 design.md 3.11）。
#
#   ./k8s/make-fault-kubeconfig.sh
#   K8S_PROXY_UPSTREAM=$(cat var/kind-upstream.txt) K8S_PROXY_CA=var/kind-ca.pem \
#     K8S_PROXY_MODE=server_error python mocks/k8s_proxy.py
#
# **agents/ も slack/ も変更しない。** 向き先を差し替えるだけで注入する。
set -euo pipefail
cd "$(dirname "$0")/.."

CONTEXT=kind-sre-safety-net
PORT="${K8S_PROXY_PORT:-8081}"

kubectl --context "$CONTEXT" config view --raw \
  -o jsonpath="{.clusters[?(@.name=='${CONTEXT}')].cluster.server}" > var/kind-upstream.txt
kubectl --context "$CONTEXT" get secret sre-agent-token -n sre-system \
  -o jsonpath='{.data.ca\.crt}' | base64 -d > var/kind-ca.pem
TOKEN=$(kubectl --context "$CONTEXT" get secret sre-agent-token -n sre-system \
  -o jsonpath='{.data.token}' | base64 -d)

cat > var/kubeconfig-sre-agent-fault.yaml <<YAML
apiVersion: v1
kind: Config
clusters:
  - name: via-proxy
    cluster: {server: "http://127.0.0.1:${PORT}"}
users:
  - name: sre-agent
    user: {token: ${TOKEN}}
contexts:
  - name: sre-agent
    context: {cluster: via-proxy, user: sre-agent}
current-context: sre-agent
YAML
chmod 600 var/kubeconfig-sre-agent-fault.yaml
echo "upstream: $(cat var/kind-upstream.txt)"
echo "wrote var/kubeconfig-sre-agent-fault.yaml / var/kind-ca.pem"
