# コマンド集

このリポジトリで使うコマンドの全集。**日常的に使う数本は [CLAUDE.md](../CLAUDE.md) 側にある**。
ここにあるのは、特定のフェーズを再実行・再検証するときに必要になるもの。

フェーズごとの意図と判定基準は各 `phases/phaseN-*/design.md` が正。

---

## 環境

```bash
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

### AWS クレデンシャルの更新（SSO の一時クレデンシャルが失効したとき）

```bash
aws sso login --profile <PROFILE>
aws configure export-credentials --profile <PROFILE> --format env-no-export >> .env
docker compose up -d              # コンテナを作り直さないと新しい値が反映されない
```

---

## 依存サービス

```bash
docker compose up -d              # 正常系（両リージョンとも実 Bedrock）
docker compose down
docker compose logs fault-injector  # primary が実際に何回叩かれたかの裏取り
```

**`restart` と `up -d --force-recreate` を取り違えない。**

```bash
docker compose restart litellm                                 # 状態のリセット専用（Router の cooldown 等）
docker compose up -d --force-recreate litellm fault-injector   # 設定・env の切り替え（restart では反映されない）
```

`restart` はコンテナを作り直さないため、`command` の `${LITELLM_CONFIG_FILE}` も環境変数も古いままになる。
→ [lessons.md](lessons.md#層の状態と設定)

---

## エージェント実行

```bash
python -m agents.main --prompt "hello"                             # 第2層を通らない (Phase 1 の経路)
python -m agents.main --prompt "hello" --repeat 20 --interval 15   # F-05 フェイルバック観測
python -m agents.main --incident "本番の payment-api の Pod が CrashLoopBackOff"   # 第2層 → 第3層
python -m agents.main --incident "..." --no-policy                 # 第2層で止める (Phase 2 の経路の再現)
```

`--incident` の終了コード:

| コード | 意味 |
| --- | --- |
| 0 | 実行成功 |
| 1 | 第1層・第2層の失敗 |
| 2 | 設定不足 |
| 3 | deny |
| 4 | 評価不能 |
| 5 | 通知の送出失敗 |
| 6 | 実行が確定的に失敗 |
| 7 | **実行の有無が不明**（最重） |

---

## 障害注入

**異常系では `agents/` を一切変更しない。** 注入は層の外側から行う（[D-005](decisions.md) / [D-014](decisions.md)）。

### 第1層（LiteLLM / Bedrock）

```bash
LITELLM_CONFIG_FILE=config.fault.yaml FAULT_MODE=rate_limit docker compose up -d
```

`FAULT_MODE` は `rate_limit` / `500` / `timeout`。

### 第2層（壊れた LLM 出力）

```bash
LITELLM_CONFIG_FILE=config.fault.yaml FAULT_MODE=malformed FAULT_FIXTURE=v01_prose \
  docker compose up -d            # 200 を返し content だけ壊す (R-01)
```

200 を返すのがポイント。エラー応答にすると第1層のフォールバックが発火し、
第2層に壊れた入力が届かない（[lessons.md](lessons.md#検証が静かに正常系になる)）。

### 第3層（OPA）

```bash
docker compose stop opa                                                      # X-01 接続不能
OPA_POLICY_DIR=./mocks/opa_faults/no_policy       docker compose up -d opa   # X-04 undefined
OPA_POLICY_DIR=./mocks/opa_faults/allow_as_string docker compose up -d opa   # X-05 形の逸脱
docker compose logs opa           # Decision Log。allow がポリシー評価の結果である裏取り
```

### 実行層（Kubernetes API）

```bash
K8S_PROXY_MODE=drop_response K8S_PROXY_UPSTREAM=$(cat var/kind-upstream.txt) \
  K8S_PROXY_CA=var/kind-ca.pem python mocks/k8s_proxy.py   # 「実行されたのに unknown」を作る
```

---

## エスカレーション層（Phase 4）

```bash
python -m slack.preview --pattern 3       # 通知パターンを 1 本送る (第1〜3層を起動せずに試せる)
python -m slack.preview --dump --out docs/phases/phase4-slack/assets   # 送らずに JSON を保存
python -m slack.readback                  # 送出したメッセージを読み戻す (届いたことの証拠。D-018)
python -m slack.handler                   # Interactivity の受信 (常駐。ngrok の転送先)
python -m slack.expire --dry-run          # 承認期限の満了検知ジョブ
```

Interactivity の受信には外部から到達可能な URL が必要（ngrok 等）。ローカル URL では検証できない。
ただし**検証の大半はトンネル無しで回せる** — 署名を自前で計算して `/slack/events` に直接 POST すれば、
handler の検証経路（署名・RBAC・CAS・再検証・実行層）はそのまま通る。

---

## 実行層（Phase 5）

**使い捨ての kind クラスタ専用**（[D-026](decisions.md)）。実クラスタ・既存クラスタには発行しない。

```bash
brew install kind kubectl         # 未導入のとき。検証は kind v0.33.0 で実施
./k8s/setup.sh                    # クラスタ作成（--recreate で作り直す）
./k8s/make-kubeconfig.sh          # ServiceAccount の kubeconfig を var/ に出す
./k8s/make-fault-kubeconfig.sh    # 実行層の障害注入用（プロキシ経由）
kind delete cluster --name sre-safety-net   # 丸ごと消す。これが D-026 の前提

python -m agents.executor         # 実行プロセス（常駐。唯一 K8S_KUBECONFIG を持つ）
python -m agents.executor --once  # 1 周だけ（検証用）

kubectl get deploy payment-api -n dev -o yaml   # 層の外側からの裏取り（実行には kubectl を使わない）
```

---

## テスト

```bash
pytest                            # 第2層 (JSON Validator) の単体検証、および層の境界の強制
python -m tests.dump_validation_errors <out.json>   # errors() の生 JSON をエビデンスとして保存

# Rego ポリシーの単体テスト。opa CLI が無いので Docker で実行する
docker run --rm -v "$PWD/opa:/policies:ro" openpolicyagent/opa:1.21.0 test /policies -v
```

`pytest` は単体検証だけでなく、**層の境界そのものを強制している**:

- `agents/k8s.py` / `execution.py` / `executor.py` に環境名・操作名のリテラルが現れたら落ちる
- `slack/handler.py` から `agents.execution` / `agents.k8s` / `kubernetes` に到達できたら落ちる

---

## 疎通

```bash
curl -s http://localhost:4000/health
curl -s http://localhost:8181/health
```

**プロセスの死活確認のみ。推論が通る保証にはならない。**
LiteLLM は推論が全滅していても `unhealthy_count: 0` を返す（[lessons.md](lessons.md#層が自己申告する値を信じない)）。
各層の健全性は実リクエストの成否で測る。
