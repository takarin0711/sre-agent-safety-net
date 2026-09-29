# sre-agent-safety-net

SRE 業務を担う LLM エージェントに対して、**非決定的な振る舞いを決定的な仕組みで囲い込む**ための多層防御（Safety Net）を検証する PoC リポジトリ。

LLM は「たまに壊れた出力を返す」「たまに危険な操作を提案する」「たまに API が落ちる」ことを前提とし、
エージェント本体の賢さに依存せず、**外側の決定的なレイヤーで安全性を担保する**ことを狙いとする。

---

## アーキテクチャ

```
                 ┌──────────────────────────────────────────┐
   ユーザ入力 ──▶ │            SRE Agent (agents/)            │
                 └────────────────┬─────────────────────────┘
                                  │
       ┌──────────────────────────▼───────────────────────────┐
       │ 第1層: システム・インフラ層  LiteLLM Proxy            │  可用性
       │  primary モデル障害時に sub モデルへ自動フェイルオーバー │
       └──────────────────────────┬───────────────────────────┘
                                  │ LLM 出力 (JSON)
       ┌──────────────────────────▼───────────────────────────┐
       │ 第2層: 出力・構造層  JSON Validator                   │  構造保証
       │  スキーマ違反を ValidationError として確定検知         │
       └──────────────────────────┬───────────────────────────┘
                                  │ 検証済みアクション
       ┌──────────────────────────▼───────────────────────────┐
       │ 第3層: 判断・ロジック層  OPA / Rego                   │  権限判定
       │  破壊的操作を allow: false として確定的に拒否          │
       └──────────────────────────┬───────────────────────────┘
                                  │ allow: true
                          ┌───────▼────────┐
                          │  インフラ実行   │
                          └────────────────┘

  いずれかの層で異常 ──▶ エスカレーション層: Slack Handoff (slack/)
                          Block Kit で人間に提示し、承認 / 却下 を待つ
```

| 層 | 責務 | 実装 | 失敗時の挙動 |
| --- | --- | --- | --- |
| 第1層 | LLM 呼び出しの可用性 | LiteLLM Proxy | サブモデルへ自動フェイルオーバー（エージェントは無自覚） |
| 第2層 (JSON Validator) | LLM 出力の構造保証 | Pydantic v2 | `ValidationError` → 例外ハンドラ → Slack へハンドオフ |
| 第3層 | 実行可否の決定的判定 | OPA / Rego | `allow: false` + 拒否理由 → Slack へ承認依頼 |
| エスカレーション | Human-in-the-loop | Slack Bolt (Block Kit) | 人間の承認で再開、却下で中断 |

---

## ディレクトリ構成

```
.
├── docker-compose.yml        # 全体の一括起動用 (LiteLLM / OPA / Slack handler)
├── requirements.txt          # エージェント側の依存
├── requirements-dev.txt      # テスト・開発用の依存
├── .env.example              # 必要な環境変数のテンプレート
├── agents/                   # エージェント本体 & 第2層 (JSON Validator。インプロセスのライブラリ)
│   ├── main.py
│   ├── schemas.py            #   層間インターフェース (ProposedAction / EscalationEvent)
│   └── validation.py         #   第2層の唯一の入口 (validate_action)
├── litellm/                  # 第1層: LiteLLM Proxy 設定
│   ├── config.yaml           #   正常系: Bedrock 2 リージョン
│   └── config.fault.yaml     #   異常系: primary を障害注入サーバに差し替え
├── opa/                      # 第3層: OPA ポリシー
│   ├── policy.rego
│   └── policy_test.rego      # Rego 単体テスト
├── slack/                    # エスカレーション層: Slack コールバック処理
│   └── handler.py
├── mocks/                    # 検証用の障害注入サーバ
│   └── fault_injector.py
├── tests/                    # pytest (第2層の単体検証)
│   ├── fixtures/             #   正常系 & 壊れた LLM 出力のケース集
│   └── test_layer2_validation.py
└── docs/                     # 設計と検証記録
    ├── architecture.md       # 全体設計
    ├── decisions.md          # 設計判断ログ
    └── phases/phaseN-*/      # フェーズごとの design.md / result.md
```

現在地と残タスクは [docs/TODO.md](docs/TODO.md)、設計ドキュメントの構成と運用ルールは [docs/README.md](docs/README.md) を参照。

---

## PoC 検証ロードマップ

| フェーズ | 検証対象 | 正常系シナリオ | 異常系・コールバック検証 | 判定基準 | 状態 |
| --- | --- | --- | --- | --- | --- |
| Phase 1 | 第1層 (LiteLLM) | LiteLLM 経由で通常プロンプトを送受信できる | プライマリ API を停止、または 429/500 エラーを模擬発生させる | 自動でサブモデルへフェイルオーバーし、エージェント側に通信エラーを返さない | **完了**（[結果](docs/phases/phase1-litellm/result.md)） |
| Phase 2 | 第2層 (JSON Validator) | 正常な JSON フォーマットが型検証を通過する | 自然言語混入、クォート閉じ漏れ、必須フィールド欠落 JSON を入力する | `ValidationError` を確定検知し、即座に例外ハンドラへハンドオフされる | **完了（合格）**（[設計](docs/phases/phase2-pydantic/design.md) / [結果](docs/phases/phase2-pydantic/result.md)） |
| Phase 3 | 第3層 (OPA) | 参照系コマンド（`kubectl get` 等）が `allow: true` になる | 破壊的操作（`kubectl delete` 等）を指示した JSON を入力する | OPA で `allow: false` と判定され、実行権限が確定的に拒否される | 未着手 |
| Phase 4 | エスカレーション機構 (Slack Handoff 単体) | ダミーのエラー JSON を送信し、Slack 上に Block Kit UI が正しく表示される | Slack 上で「手動で承認して実行」や「却下」ボタンを実際に押下する | ボタン押下時のイベント（Interactivity payload）をアプリ側で正しく受信・パースできる | 未着手 |
| Phase 5 | E2E (全体統合テスト) | タスクが AI の自律判断から実行まで全自動で完了する | Phase 1〜3 の各層で意図的に異常系イベントを発生させる | 各層に応じた Slack 通知が届き、人間がボタンを押すことで処理が安全に再開・中断される | 未着手 |

各フェーズは着手前に `docs/phases/phaseN-*/design.md` で設計を確定させ、実行後に `result.md` へ結果を記録する。

---

## セットアップ

### 前提

- Python 3.12+
- Docker / Docker Compose
- **AWS アカウント**（Bedrock。primary / fallback とも同一アカウントで、キーは 1 系統でよい）
  - 対象リージョン（`ap-northeast-1` / `us-east-2`）で推論プロファイルが `ACTIVE` であること
  - **Bedrock の Anthropic use case details form が提出済みであること。**
    未提出だとプロファイルが `ACTIVE` でも全呼び出しが `NotFoundError` になる
    （AWS コンソール → Bedrock → Model access。アカウント単位・反映に最大 15 分）

### 手順

```bash
# 1. 仮想環境
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# 2. 環境変数
cp .env.example .env

# 3. AWS クレデンシャル
#    静的キーならそのまま .env に書く。SSO なら一時クレデンシャルを末尾に追記する。
aws sso login --profile <PROFILE>
aws configure export-credentials --profile <PROFILE> --format env-no-export >> .env

# 4. 依存サービス起動 (LiteLLM Proxy / OPA)
docker compose up -d

# 5. 疎通確認（プロセスが生きているかの確認であり、推論が通る保証ではない）
curl -s http://localhost:4000/health   # LiteLLM
curl -s http://localhost:8181/health   # OPA
```

> **`/health` は可用性の指標にはならない。** Phase 1 で、推論が全滅している状態でも
> `unhealthy_count: 0` を返すことを実測した。実際に通るかは `python -m agents.main` で確かめる。

> **SSO の一時クレデンシャルは数時間で失効する。** LiteLLM コンテナは起動時に環境変数を
> 焼き込むため、更新後は `docker compose up -d` でコンテナを作り直さないと反映されない。

---

## フェーズ別の実行方法

> 各フェーズの実装が進むにつれてコマンドを追記する。

### Phase 1: Bedrock 地理圏フェイルオーバー

同一モデル `claude-sonnet-4-6` を 2 つの地理圏に配置し、primary の障害を fallback が吸収することを検証する。
primary は東京エンドポイントの `jp.` プロファイル（日本国内で推論）、fallback はオハイオエンドポイントの
`us.` プロファイル（US 圏内で推論）。障害はモックサーバから 429 / 500 / timeout を返して注入する。

設計: [docs/phases/phase1-litellm/design.md](docs/phases/phase1-litellm/design.md) /
**結果: [docs/phases/phase1-litellm/result.md](docs/phases/phase1-litellm/result.md)**

```bash
# P-01 正常系: 両リージョンとも実 Bedrock
docker compose up -d
python -m agents.main --prompt "hello"

# F-01 異常系: primary が 429 を返す（FAULT_MODE=server_error / timeout も同様）
LITELLM_CONFIG_FILE=config.fault.yaml FAULT_MODE=rate_limit docker compose up -d
docker compose restart litellm        # ← 必須。Router の cooldown 状態を持ち越さない
python -m agents.main --prompt "hello"

# F-05 フェイルバック観測: 一定間隔で繰り返し、primary に戻る時点を見る
python -m agents.main --prompt "hello" --repeat 20 --interval 15

# どちらのデプロイが応答したかの確認（エージェントの構造化ログに x-litellm-* が出る）
# 併せて primary が実際に叩かれたかを注入側で裏取りする
docker compose logs fault-injector
```

**シナリオを続けて実行するときは、毎回 `docker compose restart litellm` を挟むこと。**
LiteLLM Router の cooldown 状態はプロセス内メモリに残るため、挟まないと
「primary を一度も叩かずに fallback が応答する」状態になり、結果が汚染される（[D-009](docs/decisions.md)）。

> モデルアクセスは調査済み（[調査結果](docs/phases/phase1-litellm/result.md) / [D-008](docs/decisions.md)）。
> AWS クレデンシャルを `.env` に設定すればそのまま実行できる。

### Phase 2 (JSON Validator)

**完了（2026-09-29・合格）**。判定基準 1〜5 すべて合格（[result.md](docs/phases/phase2-pydantic/result.md)）。
第2層はエージェントと同一プロセスのライブラリなので、追加のサービス起動は不要
（`agents/schemas.py` + `agents/validation.py`）。

```bash
pytest                                                    # V 系（壊れた LLM 出力の検知）
python -m agents.main --incident "本番の payment-api の Pod が CrashLoopBackOff"

# 経路検証（R-01）: 第1層は 200 を返し、壊れているのは content だけ
LITELLM_CONFIG_FILE=config.fault.yaml FAULT_MODE=malformed FAULT_FIXTURE=v01_prose \
  docker compose up -d
```

### Phase 3〜5

（未着手）

---

## 設計上の原則

1. **エージェントは自分が守られていることを知らない。** フェイルオーバーもポリシー判定も、エージェントのプロンプトやコードの外側で完結させる。
2. **拒否は必ず理由とセットで返す。** `allow: false` だけでは人間が判断できない。
3. **PoC では実際の破壊的操作を実行しない。** 実行層はログ出力または `--dry-run` に留める。
4. **層をまたぐデータは常に JSON。** 層の境界を疎結合に保ち、単体で差し替え・テストできるようにする。
