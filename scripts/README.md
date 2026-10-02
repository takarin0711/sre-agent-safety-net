# scripts

Phase 5 の通し検証（E2E）を手で回すための配線スクリプト。

[e2e.sh](e2e.sh) 1 本だけ。**層は 1 つも実装していない。** やるのは前提確認・常駐プロセスの起動・
ステート遷移の観測・クラスタの裏取りだけで、判定はすべて各層と人間に残してある。

| 知りたいこと | 読む先 |
| --- | --- |
| 層の責務と境界 | [../docs/architecture.md](../docs/architecture.md) |
| なぜこの構成なのか | [../docs/decisions.md](../docs/decisions.md) |
| Phase 5 の判定基準 | [../docs/phases/phase5-e2e/design.md](../docs/phases/phase5-e2e/design.md) |
| コマンド全集 | [../docs/commands.md](../docs/commands.md) |

---

## 何を確かめるスクリプトか

> **確率的な部品（LLM）が危険な操作を提案し、決定的な部品（第3層）がそれを止め、
> 人間が承認して初めてクラスタの状態が変わる。**

これを 1 本の線として通し、**各段がどこで何を判断したかをログで見える形にする**のが目的。

## 何を自動化しないか

意図的に残してある。ここを自動化すると [lessons.md](../docs/lessons.md) の
「検証が静かに正常系になる」に正面からぶつかる。

| 自動化しないもの | 理由 |
| --- | --- |
| **Slack の承認ボタンの押下** | 人間が判断することが検証対象そのもの |
| **各層の判定** | スクリプトが層を代行した時点で、何を検証したのか分からなくなる |
| **LLM 出力の修復** | 壊れた入力が来ないと第2層の検知能力が測れない（[D-012](../docs/decisions.md)） |
| **Slack App の Request URL 設定** | Web UI でしかできない |
| **`execution_failed` からの再試行** | 人間がボタンを押したときだけ（[D-028](../docs/decisions.md)） |

---

## アーキテクチャ

### プロセス構成

`e2e.sh up` が起動するのは下の **tunnel / handler / executor の 3 本**。
第1層と第3層は Docker、エージェント本体は `run` のたびに起動して終了する使い捨てプロセス。

```
                    ┌──────────────────────────────────────────────────┐
                    │  e2e.sh run  （1 インシデント = 1 プロセス）        │
   インシデント文 ──▶│                                                  │
                    │   SRE Agent                 ← ここだけが確率的     │
                    │      │                                           │
                    │      │ HTTP                                      │
                    │      ├────────────▶ 第1層 LiteLLM Proxy  [可用性]  │  ◀ docker
                    │      │               論理モデル名 → 実デプロイ解決   │
                    │      │               フェイルオーバー               │
                    │      ▼                                           │
                    │   第2層 JSON Validator        [構造保証]           │  ◀ インプロセス
                    │      │  strict=True / extra="forbid"             │     (import)
                    │      │  壊れた出力は ValidationError で落とす        │
                    │      │ HTTP                                      │
                    │      ├────────────▶ 第3層 OPA / Rego     [権限判定] │  ◀ docker
                    │      │               allow + violated_rules       │
                    │      ▼                                           │
                    │   allow: false → エスカレーション送出               │
                    └──────────────────┬───────────────────────────────┘
                                       │ 終了コード 3 で終了
                                       │ **承認は待たない**
                                       ▼
                               ┌───────────────┐
                               │     Slack     │  Block Kit / 承認ボタン
                               └───────┬───────┘
                                       │ 人間が押す
                                       ▼
                        ┌──────────────────────────────┐
                        │  tunnel   (cloudflared)       │  層ではない。戻り経路の配管
                        └──────────────┬───────────────┘
                                       ▼  POST /slack/events
                        ┌──────────────────────────────┐
                        │  handler  (slack.handler)     │  エスカレーション層・受信側
                        │   署名検証 → 承認者 RBAC        │
                        │   → CAS で approved に進めて終了 │
                        │   ★ クラスタの資格情報を持たない  │  D-027
                        └──────────────┬───────────────┘
                                       │ var/handoff.sqlite3 （SQLite）
                                       │ ※ 呼び出しではなくストア越し
                                       ▼
                        ┌──────────────────────────────┐
                        │  executor (agents.executor)   │  実行層
                        │   2 秒ポーリング → CAS で占有    │
                        │   → 第2層で再検証（D-020）       │
                        │   → Kubernetes API を発行       │
                        │   ★ K8S_KUBECONFIG を持つ唯一   │
                        └──────────────┬───────────────┘
                                       ▼
                               ┌───────────────┐
                               │ kind クラスタ  │  使い捨て。D-026
                               │  prod/staging │  最後の 1 枚 = ServiceAccount の RBAC
                               │  /dev         │
                               └───────▲───────┘
                                       │ kubectl（裏取り専用。D-030）
                                       └── e2e.sh run / status
```

### この図で押さえる点

1. **エージェントのプロセスは第3層で終わる。** 承認を待たずに終了コード 3 で死ぬ。
   その後の再開は別プロセス（handler → executor）が担う。
2. **handler は executor を呼ばない。** 間にあるのは SQLite のストアだけ。
   「ボタンを押せる人 = クラスタを触れる人」という結合を切ってある（[D-027](../docs/decisions.md)）。
   `slack/handler.py` から `agents.execution` / `agents.k8s` / `kubernetes` に到達できないことは
   pytest で強制している。
3. **承認後に LLM を呼び直さない。** 再開は「アクションの実行」であって「思考の再開」ではない
   （[D-029](../docs/decisions.md)）。エージェントのプロセスが死んでいても実行される。
4. **第3層が止めたものを人間が通した事実は消さない。** 承認時に第3層へ再問い合わせせず、
   `policy_override` として監査に残す（[D-023](../docs/decisions.md)）。
5. **第3層が許可しても RBAC で止まりうる。** 多層防御の最後の 1 枚は
   ServiceAccount の権限（[k8s/rbac.yaml](../k8s/rbac.yaml)）。

### 端末とアーキテクチャの対応

`e2e.sh` を使わず手で回す場合は、下の 4 枚を別々に開くことになる。

| 端末 | コマンド | アーキテクチャ上の位置 | k8s 権限 |
| --- | --- | --- | --- |
| A | `cloudflared tunnel --url http://localhost:3000` | 層ではない。戻り経路の配管 | — |
| B | `python -m slack.handler` | エスカレーション層・受信側 | **持たない** |
| C | `python -m agents.executor` | 実行層 | **これだけが持つ** |
| D | `python -m agents.main --incident ...` | SRE Agent + 第1〜3層の呼び出し + 送出側 | — |
| D | `kubectl` / `sqlite3` | 層の外側（観測・裏取り） | admin |

`e2e.sh up` が A・B・C を `var/e2e/` にログを落としつつ起動し、`e2e.sh run` が D を担う。

---

## 前提

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
brew install kind kubectl cloudflared
./k8s/setup.sh              # 使い捨てクラスタの作成（prod/staging/dev に payment-api）
./k8s/make-kubeconfig.sh    # executor に渡す ServiceAccount の kubeconfig
cp .env.example .env        # SLACK_* / AWS_* / K8S_KUBECONFIG を埋める
```

AWS の一時クレデンシャルが失効していたら:

```bash
aws sso login --profile <PROFILE>
aws configure export-credentials --profile <PROFILE> --format env-no-export >> .env
docker compose up -d --force-recreate litellm     # restart では env が反映されない
```

Slack App 側は Bot Token スコープと **Interactivity の有効化**が必要。
詳細は [phase4-slack/design.md](../docs/phases/phase4-slack/design.md)。

---

## 手順

### 1. 前提確認

```bash
./scripts/e2e.sh check
```

venv / `.env` / AWS クレデンシャルの失効 / コンテナ / **第1層の実リクエスト** /
kind クラスタ / kubeconfig / ストアの持ち越しを見る。

第1層を `curl /health` ではなく実リクエストで測るのは、**LiteLLM が推論全滅でも
`unhealthy_count: 0` を返す**から（[lessons.md](../docs/lessons.md)「層が自己申告する値を信じない」）。
なお `/health` はマスターキーが要るので、死活確認には `/health/liveliness` を使っている。

### 2. 常駐プロセスの起動

```bash
./scripts/e2e.sh up
```

tunnel → handler → executor の順に起動し、最後に張り替える URL を表示する。

起動直後に executor が `executor.stale` を出すことがある。前回の検証で `approved` のまま
残ったレコードを拾って、提案からの経過が TOCTOU 閾値を超えているので中止扱いにしたもの。
**`approved` に戻して再実行しないのが規律**（それが二重実行の典型的な作り方）。

### 3. Slack App の Request URL を張り替える

Slack App 管理画面 → **Interactivity & Shortcuts** → Request URL を
`https://<表示された URL>/slack/events` にして **Save Changes**。

**トンネルを立て直すたびに URL が変わる**ので毎回必要。

### 4. インシデントを流す

```bash
./scripts/e2e.sh run                      # 既定のインシデント文
./scripts/e2e.sh run "任意のインシデント文"
```

狙いは **production × 変更系 × `dry_run: false`**。第3層が
`REQUIRE_DRY_RUN_IN_PRODUCTION` で拒否し、承認ボタン付きの通知パターン3 が出る。

### 5. Slack で承認ボタンを押す

**提案から 5 分以内。** `.env` の `SLACK_APPROVAL_TTL_SECONDS` は 900 だが
`SLACK_TOCTOU_SECONDS` が 300 なので、**実効期限は短いほうで決まる**
（[D-025](../docs/decisions.md)）。Slack に表示される期限もこの実効値。

押せるのは `SLACK_APPROVER_USER_IDS` に載っているユーザだけ。
それ以外が押すと ephemeral で弾かれ、**押せてしまったこと自体が `denied_by_rbac` として監査に残る**。

### 6. 判定

`run` はボタン押下後もステート遷移を追い続け、終わったら自動で下を出す。

- 実行前後の `restartedAt` と Pod 一覧
- 最終ステート
- 監査ログ（`policy_override` が付いているか）

`run` を途中で止めた場合や、あとから見直したい場合は同じ判定をやり直せる。

```bash
./scripts/e2e.sh verify
```

**何をもって成立とするかは下の「成立の条件」と「手動で確認する」を見ること。**

---

## サブコマンド

| コマンド | やること |
| --- | --- |
| `check` | 前提確認だけ。プロセスを起動しない |
| `up` | 前提確認 + tunnel / handler / executor の起動 → Request URL を表示 |
| `run ["文"]` | インシデントを 1 本流す → 承認を待つ → 前後比較と監査ログ |
| `verify [trace]` | 実行前後の比較・ストア・監査ログ・層ごとの判定をやり直す（省略時は直近） |
| `status` | 常駐プロセスの生死、トンネル URL、ストアのステート分布 |
| `logs` | handler / executor のログを `tail -f` |
| `down` | 常駐 3 本を停止（docker compose と kind は残す） |

---

## 出力の読み方

### `run` の終了コード

`python -m agents.main --incident` の終了コードをそのまま出す。**大きいほど重い。**

| コード | 意味 | 通しの検証として |
| --- | --- | --- |
| 0 | 第3層を通過して実行まで到達 | **想定外。** LLM が `dry_run: true` を提案した。ボタンは出ない |
| 1 | 第1層・第2層の失敗 | `validation.error` なら LLM が JSON を壊した（**修復しない設計**） |
| 2 | 設定不足 | `.env` を見る |
| **3** | **第3層が `allow: false`** | **これが正解。** Slack に承認ボタンが出ている |
| 4 | 第3層が評価不能（Fail-Closed） | OPA が落ちている / ポリシー未ロード |
| 5 | 遮断は成立したが Slack に届いていない | もっとも危険な失敗（止めたことを誰も知らない） |
| 6 | 実行して確定的に失敗（4xx） | 実行されていないことが分かっている |
| 7 | **実行の有無が不明**（5xx / 応答なし） | **最重。** 自動再試行しない（[D-028](../docs/decisions.md)） |

### 層ごとに 1 行ずつ出るログ

| イベント | 層 | 見るところ |
| --- | --- | --- |
| `request.success` | 第1層 | `x-litellm-model-id` でどのリージョンが応答したか |
| `validation.ok` / `validation.error` | 第2層 | 構造が通ったか。失敗なら `errors` に Pydantic の生の内容 |
| `policy.allow` / `policy.deny` / `policy.unavailable` | 第3層 | `violated_rules` に違反したルール名 |
| `slack.notified` / `slack.failed` | エスカレーション | 人間に届いたか |
| `handoff.approved` | handler | 承認が記録された（**ここでクラスタには触らない**） |
| `execution.applied` → `executor.executed` | executor | API を発行し、結果が 2xx だった |
| `executor.stale` | executor | 提案から時間が経ちすぎて中止 |
| `executor.failed` / `executor.unknown` | executor | 4xx で確定的に失敗 / 5xx・無応答で実行有無が不明 |
| `executor.recovered` | executor | 起動時の後始末。`executing` のまま残ったものを `unknown` に倒した |

### ステート遷移

```
pending ──[承認]──▶ approved ──[占有]──▶ executing ──▶ executed
   │                    │                      │
   │                    └──[5分超過]──▶ stale   ├──▶ execution_failed  (4xx。実行されていない。再試行ボタンが出る)
   │                                           └──▶ unknown           (5xx/無応答。実行有無が不明。最重。
   │                                                    │                再試行ボタンを出さない。D-028)
   ├──[却下]──▶ rejected   ← 却下は期限後も通る（D-025）  └──[人間が中止]──▶ abandoned
   ├──[期限切れ]──▶ expired
   └──[送出に失敗]──▶ notify_failed   ← 遮断は成立しているが人間に届いていない
```

### 成立の条件

- `kubectl.kubernetes.io/restartedAt` が実行時刻に更新されている
- Pod 名が変わり、新しい ReplicaSet が立っている
- `pending_actions.status` が `executed`
- `audit_log` に `approve` / `execute` が `override_kind = policy_override` で並んでいる

### 手動で確認する

`run` が最後まで到達しなかったときや、あとから見直したいときは:

```bash
./scripts/e2e.sh verify                 # 直近のハンドオフ
./scripts/e2e.sh verify <trace_id>      # trace_id を指定
```

**クラスタの状態**（実行には使わない。裏取り専用。[D-030](../docs/decisions.md)）

```bash
K8S_CTX="kind-sre-safety-net"

# 1. Pod テンプレートが書き換わったか
kubectl --context "$K8S_CTX" -n prod get deploy payment-api \
  -o jsonpath='{.spec.template.metadata.annotations.kubectl\.kubernetes\.io/restartedAt}'; echo

# 2. Pod が入れ替わったか（名前のハッシュ部が変わる / CREATED が実行時刻）
kubectl --context "$K8S_CTX" -n prod get pods

# 3. 新しい ReplicaSet が立ち、古いほうが 0 になっているか
kubectl --context "$K8S_CTX" -n prod get rs

# 4. Deployment の世代が進んだか
kubectl --context "$K8S_CTX" -n prod get deploy payment-api -o jsonpath='{.metadata.generation}'; echo
kubectl --context "$K8S_CTX" -n prod rollout history deploy/payment-api
```

> **zsh では `$VAR` が単語分割されない。** `K="kubectl --context x"` のように
> コマンド全体を変数に入れて `$K get pods` とすると `flags cannot be placed before plugin name`
> で落ちる。フラグは直接書くか、上のように値だけを変数にする。

**ストアと監査**

```bash
TRACE=<trace_id>
sqlite3 -column -header var/handoff.sqlite3 \
  "select status, approved_by, executed_at, execution_result from pending_actions where trace_id='$TRACE';"
sqlite3 -column var/handoff.sqlite3 \
  "select occurred_at, action, actor, override_kind from audit_log where trace_id='$TRACE' order by id;"
```

**層ごとの判定**

```bash
grep -o '"event": "[a-z_.]*"' var/e2e/incident.log          # 第1〜3層 + 送出
grep -o '"violated_rules":[^]]*]' var/e2e/incident.log      # 第3層が返した違反ルール
grep "$TRACE" var/e2e/handler.log                           # 受信側（クラスタに触っていないこと）
grep "$TRACE" var/e2e/executor.log                          # 実行層（dry_run と発行結果）
```

### 通ったときに実際に見えるもの

```
restartedAt   2026-10-01T10:06:28+00:00  →  2026-10-02T07:33:06+00:00
Pod           payment-api-66655dd8d-xxxxx →  payment-api-7f95dcf454-xxxxx
ReplicaSet    66655dd8d 2/2               →  7f95dcf454 2/2（66655dd8d は 0 へ）
generation    2                           →  3

incident.log  request.success → validation.ok → policy.deny → slack.notified
              violated_rules: ["REQUIRE_DRY_RUN_IN_PRODUCTION"]
handler.log   handoff.received → handoff.approved   （override_kind: policy_override）
executor.log  execution.applied → executor.executed （dry_run: false）
audit_log     created → approve(policy_override) → execute(policy_override)
```

Pod 名のハッシュ部が変わっているのが要点。**Pod テンプレートが書き換わって別の
ReplicaSet が立った**ということで、`kubectl rollout restart` と同じ結果になっている。

---

## つまずきどころ

| 症状 | 原因 | 対処 |
| --- | --- | --- |
| `check` が AWS 失効で落ちる | 一時クレデンシャルの期限切れ | `aws sso login` → `export-credentials >> .env` → `docker compose up -d --force-recreate litellm` |
| `run` が終了コード 0 | LLM が `dry_run: true` を提案した | インシデント文に「dry-run ではなく実際に適用」を入れて再実行 |
| `run` が終了コード 1 | LLM が JSON を壊した | そのまま再実行。**修復は入れない**（[D-012](../docs/decisions.md)） |
| ボタンを押しても何も起きない | Request URL が古い | トンネルを立て直すたびに張り替える。`status` で現 URL を確認 |
| 押したのに executor が動かない | 5 分超過で stale | `run` からやり直す |
| `execution_failed` で終わる | **ServiceAccount の RBAC で止まった** | 失敗ではなく**最後の 1 枚が効いた**ケース。[k8s/rbac.yaml](../k8s/rbac.yaml) を見る |
| 権限が無いはずの操作が通る | `~/.kube/config` の admin を拾っている | `K8S_KUBECONFIG` を確認。**フォールバックしない設計**なので、通ったら設定ミス |
| `up` がポート使用中で落ちる | 手動起動の handler が残っている | `./scripts/e2e.sh down` か該当プロセスを停止 |

---

## 生成物

すべて `var/e2e/`。`var/` は gitignore 済み。

| ファイル | 中身 |
| --- | --- |
| `tunnel.log` / `handler.log` / `executor.log` | 常駐プロセスの標準出力 |
| `*.pid` | 停止用の PID |
| `incident.log` | `agents.main --incident` の構造化ログ（層ごとの判定が全部入っている） |
| `before.txt` / `after.txt` | 実行前後のクラスタ状態 |

**エビデンスとして `docs/phases/*/assets/` に残すときは、秘密情報を除去してから置く。**
LiteLLM は `--detailed_debug` で動いているため、ログに master key や AWS の識別子が含まれうる。

---

## 安全について

- **破壊的操作を発行してよいのは使い捨ての kind クラスタに対してだけ**（[D-026](../docs/decisions.md)）。
  `e2e.sh` は context 名を `kind-sre-safety-net` に固定していて、実クラスタには向かない。
- 片付けは `kind delete cluster --name sre-safety-net` で丸ごと。**消せることが前提の設計**。
- `e2e.sh down` は常駐 3 本を止めるだけで、docker compose と kind クラスタは残す。
