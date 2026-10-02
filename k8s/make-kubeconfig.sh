#!/usr/bin/env bash
# ServiceAccount sre-agent の kubeconfig を var/ に生成する（phase5 design.md 事前準備）。
#
#   ./k8s/make-kubeconfig.sh
#
# **これが executor に与える唯一の資格情報である。** slack/handler.py には与えない（D-027）。
# var/ は gitignore 済み。**生成物を assets/ に貼らないこと**（トークンと CA が入る）。
set -euo pipefail

CLUSTER=sre-safety-net
CONTEXT="kind-${CLUSTER}"
OUT="${1:-var/kubeconfig-sre-agent.yaml}"

SERVER=$(kubectl --context "$CONTEXT" config view -o jsonpath="{.clusters[?(@.name=='${CONTEXT}')].cluster.server}" --raw)
CA=$(kubectl --context "$CONTEXT" get secret sre-agent-token -n sre-system -o jsonpath='{.data.ca\.crt}')
TOKEN=$(kubectl --context "$CONTEXT" get secret sre-agent-token -n sre-system -o jsonpath='{.data.token}' | base64 -d)

[ -n "$TOKEN" ] || { echo "token がまだ発行されていません（Secret の作成直後は数秒かかる）" >&2; exit 1; }

mkdir -p "$(dirname "$OUT")"
cat > "$OUT" <<YAML
apiVersion: v1
kind: Config
clusters:
  - name: ${CLUSTER}
    cluster:
      server: ${SERVER}
      certificate-authority-data: ${CA}
users:
  - name: sre-agent
    user:
      token: ${TOKEN}
contexts:
  - name: sre-agent
    context: {cluster: ${CLUSTER}, user: sre-agent}
current-context: sre-agent
YAML
chmod 600 "$OUT"
echo "wrote $OUT"
