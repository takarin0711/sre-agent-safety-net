# TODO / 現在地

最終更新: 2026-09-04

## 現在地

**Phase 1 は設計・実装が完了し、実行待ち。** AWS クレデンシャルを `.env` に設定すれば P-01 から着手できる。
Phase 2 以降は未着手。

| フェーズ | 設計 | 実装 | 実行 |
| --- | --- | --- | --- |
| Phase 1 (LiteLLM) | 完了 | 完了 | **未実施** |
| Phase 2 (Pydantic) | 未着手 | 未着手 | 未着手 |
| Phase 3 (OPA) | 未着手 | 未着手 | 未着手 |
| Phase 4 (Slack) | 未着手 | 未着手 | 未着手 |
| Phase 5 (E2E) | 未着手 | 未着手 | 未着手 |

---

## これまでにやったこと

### リポジトリ整備

- `README.md` / `CLAUDE.md` / `.gitignore` / `requirements.txt` / `requirements-dev.txt` / `.env.example`
- ディレクトリ骨格（`agents/` `litellm/` `opa/` `slack/` `mocks/` `docs/`）

### ドキュメント体系の確立

- `docs/README.md` — 索引と運用ルール（**design.md は着手前に書き切り、結果が出た後に判定基準を書き換えない**）
- `docs/architecture.md` — 脅威モデル、設計原則、層の責務と境界、層間インターフェース、スコープ外
- `docs/decisions.md` — 設計判断ログ D-001〜D-008
- `docs/phases/phaseN-*/` — フェーズごとに `design.md` / `result.md` / `assets/`

### Phase 1 の設計

- `docs/phases/phase1-litellm/design.md` — 目的と非目的、構成、リトライとフォールバックの切り分け、
  検証シナリオ 7 件（P-01 / F-01〜F-05 / N-01）、判定基準 5 項目、観測方法、未決事項
- `docs/phases/phase1-litellm/result.md` — 記入用テンプレート + モデルアクセス調査の結果

### Phase 1 の実装

| ファイル | 内容 | 検証状況 |
| --- | --- | --- |
| `litellm/config.yaml` | 正常系。`jp.` / `us.` プロファイルの 2 デプロイ + `fallbacks` | YAML 構文 OK |
| `litellm/config.fault.yaml` | 異常系。primary を障害注入サーバに差し替え | YAML 構文 OK |
| `docker-compose.yml` | LiteLLM / fault-injector / OPA（`phase3` プロファイル） | `docker compose config` OK |
| `mocks/fault_injector.py` | 429 / 500 / timeout / success を返すモック（標準ライブラリのみ） | **4 モードとも実機確認済み** |
| `agents/main.py` | 論理モデル名のみ使用。構造化ログ、`x-litellm-*` ヘッダ観測、`--repeat` / `--interval` | 構文チェックのみ |

### AWS Bedrock の調査（完了）

- Claude で `ON_DEMAND` 可能なのは `claude-3-haiku` のみ。他はすべて**推論プロファイル必須**
- ap-northeast-1 に `jp.` プレフィックスの**日本国内限定プロファイル**が存在する
- Sonnet 5 / Opus 5 は東京では `global.` のみ（US を包含するため fallback 先に不適）
- → **`claude-sonnet-4-6` を採用**。`jp.`（東京）→ `us.`（オハイオ）の地理圏フェイルオーバー（D-008）

---

## Phase 1 の残タスク

### 準備

- [ ] `.env` を作成し AWS クレデンシャルを設定
- [ ] `python3 -m venv .venv` / `pip install -r requirements.txt -r requirements-dev.txt`
- [ ] `docker compose up -d` で LiteLLM Proxy が起動することを確認

### 検証の実行

- [ ] **P-01（正常系）** — primary が応答することを確認
  - [ ] **観測手段を確定する。** `x-litellm-*` ヘッダに何が返るかを見て、
        design.md 第6章の表（現在すべて「要確認」）を埋める。判定基準2 の根拠になる最重要項目
- [ ] **F-01（429）** — フォールバック発火。エビデンスを `assets/` に保存
- [ ] **F-02（500）** — 同上
- [ ] **F-03（timeout）** — 同上
- [ ] **F-04（`num_retries: 2`）** — リトライ → フォールバックの順序を確認
- [ ] **F-05（フェイルバック）** — `--repeat 20 --interval 15` で primary に戻る時点を観測
- [ ] **N-01（両系障害）** — 明示的な例外が伝播することを確認（安全網の限界）

### 記録

- [ ] `result.md` のシナリオ別結果・判定・考察を記入
- [ ] `README.md` ロードマップ表の「状態」列を更新

### 実行中に確定させる未決事項（design.md 第7章）

- [ ] **#4** `cooldown_time` の既定値と、F-05 で待つべき時間
- [ ] **#5** 追加レイテンシの合格ライン（**仮置き 5 秒**の妥当性。P-01 の実測値を基準に確定）
- [ ] **#7** fallback 側の課金・レート上限
- [ ] **#8** LiteLLM が返すヘッダ名（`agents/main.py` が全て記録するので実行すれば判明）

### 実行前に確認しておくとよいこと

- [ ] Bedrock の**モデルアクセス許可**が両リージョンで有効か。プロファイルが `ACTIVE` でも
      アカウント側の許可が別途必要な場合がある。最初の呼び出しで判明する
- [ ] LiteLLM の Bedrock 呼び出しで `bedrock/` プレフィックスのままでよいか
      （`bedrock/converse/` が必要なケースがないか）。P-01 で判明する

---

## Phase 2 以降の準備タスク

いずれも着手時に `design.md` を先に書く。

### Phase 2 (Pydantic)

- [ ] `docs/phases/phase2-pydantic/design.md` を書く
- [ ] `agents/schemas.py` に層間インターフェースを定義（対象環境 / 操作種別 / 対象リソースを含む）
- [ ] 壊れた JSON のケース集を用意（自然言語混入 / クォート抜け / 必須キー欠落）
- [ ] pytest で単体テスト

### Phase 3 (OPA)

- [ ] `docs/phases/phase3-opa/design.md` を書く
- [ ] `opa/policy.rego` / `opa/policy_test.rego`
- [ ] `docker compose --profile phase3 up -d` で OPA が起動することを確認
- [ ] エージェント → OPA の HTTP 呼び出しを実装
- [ ] **OPA 自体が単一障害点になる問題**の扱いを設計で決める（D-002 の影響欄）

### Phase 4 (Slack)

- [ ] `docs/phases/phase4-slack/design.md` を書く
- [ ] Slack App 作成、Bot トークンと署名シークレット取得
- [ ] ngrok 等で Interactivity Request URL を用意（**ローカル URL では検証できない**）
- [ ] `slack/handler.py`（Block Kit + Interactivity payload の受信・パース）

### Phase 5 (E2E)

- [ ] `docs/phases/phase5-e2e/design.md` を書く
- [ ] **中断・再開の状態管理を自前で実装する必要がある**（D-001 でフレームワークを入れないと決めたため）。
      Phase 5 の設計時に D-001 の再検討余地あり

---

## 成果物に載せるべき論点（考察用のメモ）

検証結果そのものより、提案として効くのはこちら。忘れないうちに記録しておく。

- **第1層の価値の重心が「可用性」ではない。** AWS が `global.` プロファイルで既に広範な
  フェイルオーバーを提供している以上、Bedrock 内に閉じる限り LiteLLM が可用性に追加する価値は限定的。
  第1層の本当の価値は**観測可能性・ベンダー非依存性・第2層以降との一貫性**にある（D-004）
- **ベンダー横断フォールバックが Bedrock 内で完結する。** `us.openai.gpt-5.6-*` や `us.xai.grok-4.6` が
  提供されており、別クラウドのアカウントもキーも用意せず config に 1 エントリ足すだけで実現できる（D-003）
- **「AWS の推論プロファイルで足りる範囲」と「LiteLLM でないと守れない範囲」の切り分け。**
  そのまま提案書の骨子になる
