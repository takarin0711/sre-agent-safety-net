# TODO / 現在地

最終更新: 2026-09-29

## 現在地

**Phase 1 は完了（2026-09-29）。** 7 シナリオすべてを実施し総合判定は合格。
結果・考察・提言は [phases/phase1-litellm/result.md](phases/phase1-litellm/result.md)。

**Phase 2 は完了（2026-09-29）。** 全シナリオを実施し総合判定は合格。
結果・考察・提言は [phases/phase2-pydantic/result.md](phases/phase2-pydantic/result.md)。
新たな設計判断は [D-010〜D-014](decisions.md)。

**次は Phase 3（OPA）。着手前に design.md を書くこと。**

| フェーズ | 設計 | 実装 | 実行 |
| --- | --- | --- | --- |
| Phase 1 (LiteLLM) | 完了 | 完了 | **完了（合格）** |
| Phase 2 (JSON Validator) | 完了 | 完了 | **完了（合格）** |
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

## Phase 1 の結果サマリ

7 シナリオすべて実施済み。判定基準 1〜5 すべて合格（判定基準5 の閾値は result.md で確定）。

| ID | 結果 | 要点 |
| --- | --- | --- |
| P-01 | 合格 | `primary-jp` が応答。baseline 1.17〜5.39 s (n=5) |
| F-01 (429) | 合格 | `fallback-us` が応答。追加レイテンシほぼゼロ |
| F-02 (500) | 合格 | 同上 |
| F-03 (timeout) | 合格 | 例外は出ないが無応答型のため `timeout: 30` を待ち切る（約 31 s） |
| F-04 (`num_retries: 2`) | 合格 | リトライ 2 回 → フォールバックの順序を injector 側で確認 |
| F-05 (フェイルバック) | 合格 | 復旧後の最初のリクエストで primary に復帰。cooldown は発動せず |
| N-01 (両系障害) | 合格 | `InternalServerError` が原因つきで伝播 |

### 実行中に判明した重要事項

- **`x-litellm-attempted-retries` は信用できない**（実際に 2 回リトライしても 0 を返す）。
  エビデンスは「ヘッダ + 障害注入側のアクセスログ」の2系統が必須（[D-009](decisions.md)）
- **`GET /health` は可用性の指標にならない**。推論が全滅していても `unhealthy_count: 0` を返す
- **cooldown の発動条件が設定値から予測できない**。同一設定で発動する / しないの両方を観測
- **シナリオ間で Router の状態が汚染される**。各シナリオ前に `docker compose restart litellm` が必要
- Bedrock の **Anthropic use case details form** が未提出だと、推論プロファイルが `ACTIVE` でも
  全呼び出しが `NotFoundError` になる（2026-09-29 に提出して解消）

### Phase 2 に持ち越す論点（Phase 2 で解決済み）

> L-01 で実測し、`litellm/config.yaml` の `timeout` を **30 → 60** に再決定した。
> 決めていたのは「フェーズ」ではなく**出力トークン数の上限**だった（phase2 result.md 考察1）。

- **`timeout` はワークロードごとに実測して決める。** Phase 1 の `timeout: 30` は
  「こんにちは」に 28 トークンで答えるだけの呼び出しでの値。Phase 2 は構造化 JSON を
  数百〜数千トークン出力するため正常時レイテンシが桁で変わる。**30 秒では短すぎる可能性が高い**
- `timeout` は正常時レイテンシの裾より上に置く必要がある（短すぎると健全な primary を誤って殺す）。
  無応答型障害の検知時間には「正常時の最悪値」という構造的な下限がある

### 積み残し（Phase 1 としては完了扱い）

- [ ] 実 Bedrock の障害時挙動は未検証（異常系の primary はモック。design.md 3.4 の既知の限界）
- [ ] cooldown の正確な発動条件
- [ ] fallback 側のレート上限（本 PoC の呼び出し量では未到達）
- [ ] 実 Bedrock でのフェイルバック所要時間
- [ ] 正常時レイテンシの信頼できる分布（baseline は n=5 で p99 を語れない）
- [ ] 地理圏によるレイテンシ分布の差（us-east-2 は安定、ap-northeast-1 に外れ値。標本不足で未結論）

---

## Phase 2 の実装と実行（完了）

実施手順は [design.md 第8章](phases/phase2-pydantic/design.md)。

実装（完了）:

- [x] `docs/phases/phase2-pydantic/design.md` を書く
- [x] `agents/schemas.py`（`ProposedAction` / `TargetResource` / enum / `EscalationEvent`）。
      `strict=True` と `extra="forbid"` は `LAYER2_MODEL_CONFIG` に集約して全モデルに適用
- [x] `agents/validation.py`（`validate_action()` = 第2層の唯一の入口。`model_validate_json()` を使い、
      失敗を単一の例外型に正規化する）
- [x] `agents/main.py` に `--incident` 経路を追加（`--prompt` の挙動とログ出力は不変）
- [x] `tests/fixtures/`（V-01〜V-08 + 正常系 + N-01。構文が壊れているものは `.txt`）
- [x] `tests/test_layer2_validation.py`（V 系 + D-01 決定性 + N-01 責務境界。66 件パス）
- [x] `mocks/fault_injector.py` に `FAULT_MODE=malformed` を追加（既存 3 モードは無変更）

設計時に想定していなかった実装上の追加（いずれも design.md の判断を変えるものではない）:

- `agents/log.py` — 構造化ログを `main.py` と `validation.py` で共用するため切り出した。
  出力フォーマットは Phase 1 と同一で、`--prompt` 経路のログは 1 バイトも変わらない
- `agents/prompts.py` — インシデント用プロンプト。design.md 3.6 の「書くこと / 書かないこと」を
  1 ファイルに閉じ込め、enum 値はスキーマ定義から生成して語彙の食い違いを防ぐ
- `conftest.py`（リポジトリルート） — `tests/` から `agents` を import するため
- `content: null` は fixture ファイルで表現できないため、`FAULT_FIXTURE=null_content` を予約名とした
- fixture が見つからない場合、fault-injector は**起動時に終了する**。
  実行時に 500 を返すと第1層がフォールバックして実 Bedrock が応答し、
  「壊れた content を返したはずが正常なアクションが返る」という誤ったシナリオになるため

実行（完了。単体 → 実 LLM の順で実施した）:

- [x] V-01〜V-08 / P-02 を pytest で通す（66 件パス。`pytest -v` を `assets/` に保存）
- [x] D-01（同一プロセス 1,600 判定 + 別プロセス 10 回の SHA-256 が全一致）
- [x] P-01（実 LLM）→ 未決事項 4・5 を確認（OPA に `input` として投入できる / enum 逸脱なし）
- [x] L-01 レイテンシ実測（n=20 + 長出力 n=5）→ **`timeout` を 30 → 60 に再決定**
- [x] R-01 経路検証（9 fixture。第1層の `attempted-fallbacks=0` と injector ログで裏取り）
- [x] N-01 限界（本番の PVC 削除が第2層を**通過**することを確認）
- [x] R-02 / C-01（考察材料。判定基準には使っていない）


## Phase 2 の結果サマリ

全シナリオ実施済み。判定基準 1〜5 すべて合格。

| ID | 結果 | 要点 |
| --- | --- | --- |
| P-01 | 合格 | 実 LLM の出力が検証を通過。`model_dump(mode="json")` を OPA に `input` として投入できた |
| P-02 | 合格 | enum 全 21 組・任意フィールド省略・`replicas` 境界値 0 / 100 が通過 |
| V-01〜V-08 | 合格 | 16 パターン全件検知。`type` / `loc` / `msg` から理由を特定できる |
| D-01 | 合格 | 同一プロセス 1,600 判定 + 別プロセス 10 回の SHA-256 が完全一致 |
| R-01 | 合格 | 経路でも検知・終了コード 1・`EscalationEvent` 生成。第1層は `attempted-fallbacks=0` |
| N-01 | 合格 | 本番の `kubectl_delete_pvc` / `dry_run=false` が第2層を通過（責務境界の証明） |
| L-01 | 計測 | reason 〜160 文字で max 9.77 s (n=20)、〜850 文字で max 17.93 s (n=5) |
| R-02 | 参考 | 敵対的プロンプト 10 回中 7 回が構造破壊。**全件 `json_invalid`**（散文 / コードフェンス） |
| C-01 | 参考 | Bedrock はスキーマを拒否（`minimum`/`maximum` 非対応）。制約を落とすと構文破壊は消えるが、値の制約・相互依存・意味の危険性は素通り |

### 実行中に判明した重要事項

- **障害注入が効いていないと「静かに正常系」になる。** injector が起動せず第1層が実 Bedrock に
  フォールバックし、R-01 の全 10 シナリオが終了コード 0 になった（[D-014](decisions.md)）
- **strict モードは JSON 入力と Python 入力で挙動が違う。** `model_validate(dict)` は
  enum に実インスタンスを要求し、素の文字列は `is_instance_of` で落ちる。
  `model_validate_json()` を使う設計は例外型の正規化だけでなく strict を効かせるためにも必須だった
- **実 LLM の壊れ方は構文レベルに偏る。** enum 逸脱・型不一致・必須キー欠落は自然には起きなかった
  （「drain せよ」と誘導しても語彙内の `kubectl_describe` に丸めた）
- **構造化出力の強制は「弱められたスキーマ」しか強制できない。**
  `minimum` / `maximum` / `minLength` / `maxLength` / `pattern` を落とさないと Bedrock が 400 を返す
- **`EscalationEvent` に相関 ID が無い。** 第1層のログと突き合わせられないので Phase 4 で追加する

### 積み残し（Phase 2 としては完了扱い）

- [ ] `EscalationEvent` に `request_id` / `x-litellm-call-id` を追加（Phase 4）
- [ ] `raw_output` の切り詰め（Block Kit のテキスト長上限。Phase 4）
- [ ] 実 LLM の構造破壊率は測っていない（PoC の呼び出し量では有意な推定ができない。design.md 非目的）
- [ ] 強制スキーマの自動生成（第2層のスキーマから劣化コピーを作る仕組み）は提言に留めた

---

## Phase 3 以降の準備タスク

いずれも着手時に `design.md` を先に書く。

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
