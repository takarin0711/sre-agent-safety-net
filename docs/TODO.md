# TODO / 現在地

最終更新: 2026-10-02

## 現在地

**Phase 1 は完了（2026-09-29）。** 7 シナリオすべてを実施し総合判定は合格。
結果・考察・提言は [phases/phase1-litellm/result.md](phases/phase1-litellm/result.md)。

**Phase 2 は完了（2026-09-29）。** 全シナリオを実施し総合判定は合格。
結果・考察・提言は [phases/phase2-pydantic/result.md](phases/phase2-pydantic/result.md)。
新たな設計判断は [D-010〜D-014](decisions.md)。

**Phase 3 の設計を確定（2026-09-30）。** [phases/phase3-opa/design.md](phases/phase3-opa/design.md) を書き切った。
新たな設計判断は [D-015〜D-017](decisions.md)。

**Phase 3 は完了（2026-09-30）。** 全シナリオを実施し総合判定は合格。
結果・考察・提言は [phases/phase3-opa/result.md](phases/phase3-opa/result.md)。
新たな設計判断は [D-015〜D-018](decisions.md)。

**Phase 4 の設計を確定（2026-09-30）。** [phases/phase4-slack/design.md](phases/phase4-slack/design.md) を書き切った。
新たな設計判断は [D-019〜D-024](decisions.md)。

**Phase 4 は完了（2026-10-01）。** 全シナリオを実施し総合判定は合格。
結果・考察・提言は [phases/phase4-slack/result.md](phases/phase4-slack/result.md)。
実装中・実行中に出た設計判断は [D-019〜D-025](decisions.md)。

**Phase 5 の設計を確定（2026-10-01）。** [phases/phase5-e2e/design.md](phases/phase5-e2e/design.md) を書き切った。
新たな設計判断は [D-026〜D-030](decisions.md)。**CLAUDE.md の破壊的操作の禁止を改訂した**（[D-026](decisions.md)）。

**Phase 5 は完了（2026-10-01）。** 全シナリオを実施し総合判定は合格。
結果・考察・提言は [phases/phase5-e2e/result.md](phases/phase5-e2e/result.md)。
実装中・実行中に出た設計判断は [D-026〜D-030](decisions.md)。

**通しを実機で再現し、配線スクリプトを整備（2026-10-02）。**
Phase 5 の通しを `--once` ではなく**常駐モードで 1 本通した**（trace `a9d5311a`）。
手順を [../scripts/e2e.sh](../scripts/e2e.sh) にまとめ、[../scripts/README.md](../scripts/README.md) を書いた。
ハーネス側で踏んだ失敗は [lessons.md](lessons.md) に追加。

**PoC のロードマップ（Phase 1〜5）はこれで全て完了した。**

各フェーズの設計は、別途の仕様書「フォールバック & Handoff フロー図」を前提にしている。
**どこまでを Phase N で扱い、どこからを次に渡すかは各 design.md 第0章の対応表が正**。

| フェーズ | 設計 | 実装 | 実行 |
| --- | --- | --- | --- |
| Phase 1 (LiteLLM) | 完了 | 完了 | **完了（合格）** |
| Phase 2 (JSON Validator) | 完了 | 完了 | **完了（合格）** |
| Phase 3 (OPA) | 完了 | 完了 | **完了（合格）** |
| Phase 4 (Slack) | 完了 | 完了 | **完了（合格）** |
| Phase 5 (E2E) | 完了 | 完了 | **完了（合格）** |

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

- [x] `EscalationEvent` に相関 ID を追加 → **Phase 4 ではなく Phase 3 で導入する**（[D-016](decisions.md)）。
      Handoff 仕様書の運用原則2（ボタンの `value` は Trace ID のみ）が、第3層の拒否時点で
      Trace ID が確定していることを前提にしているため
- [x] `raw_output` の切り詰め（Block Kit のテキスト長上限。Phase 4）→ `slack/blocks.py` の `truncate()`。
      **上限超過は Slack が `invalid_blocks` で拒否する**ため、切り詰めは表示ではなく送出可否の問題だった
- [ ] 実 LLM の構造破壊率は測っていない（PoC の呼び出し量では有意な推定ができない。design.md 非目的）
- [ ] 強制スキーマの自動生成（第2層のスキーマから劣化コピーを作る仕組み）は提言に留めた

---

## Phase 3 の実装と実行（完了）

実施手順は [design.md 第8章](phases/phase3-opa/design.md)。判定基準は第5章。

実装（完了）:

- [x] `opa/policy.rego`（6 ルール。`decision` 文書 1 本で `allow` と `violated_rules` を返す。
      **`default allow := false` は書かない** — undefined を「拒否」に潰すとパターン3 と 4 が区別できなくなる）
- [x] `opa/data.json`（操作の分類・保護 namespace・環境と namespace の対応。`data.catalog` に置く）
- [x] `opa/policy_test.rego`（U-01〜U-08 + N-02。**18 件パス**。各ルールに allow / deny 両方）
- [x] `mocks/opa_faults/`（X-04 / X-05 用。`allow_as_string` / `contradictory` / `unknown_key` / `no_policy`）
- [x] `docker-compose.yml`（OPA の `profiles` を外す / `--set=decision_logs.console=true` /
      `OPA_POLICY_DIR` の変数化 / `--watch` / イメージを `1.21.0` に固定）
- [x] `agents/schemas.py`（`ViolatedRule` / `PolicyDecision` / `OpaResponse` / `EscalationEvent` 拡張）
- [x] `agents/policy.py`（`evaluate_action()` と `PolicyUnavailableError`。第3層の唯一の入口。
      **deny は戻り値、評価不能は例外**）
- [x] `agents/main.py`（Trace ID の発番と伝播 / 第3層の呼び出し / 実行層の dry-run ログ /
      終了コード 3・4 / `--no-policy`）
- [x] `tests/fixtures/policy/`（B-02〜B-05 / N-01 の 2 種 / N-02。B-01・P-02・P-03 は Phase 2 の fixture を再利用）
- [x] `tests/test_layer3_policy.py`（decision のパースと Fail-Closed の単体。**27 件パス**。HTTP はモック）
- [x] `.env.example` / `.env`（`OPA_URL` を decision のフルパスに / `OPA_TIMEOUT_SECONDS` / `OPA_POLICY_DIR`）

設計時に想定していなかった実装上の追加・差分（**いずれも result.md に転記する**）:

- **`REQUIRE_NAMESPACE_ENVIRONMENT_MATCH` から参照系を除外した。** design.md 3.4 の表は
  参照系を除外していなかったが、除外しないと U-06（保護 namespace × 参照系は allow）および
  「参照系はどのルールにも該当しない」という構造と矛盾する。**design.md は書き換えず**、
  ポリシー側にコメントを残した（判定基準そのものは変えていない）
- `agents/log.py` に `json_safe_errors()` を移した。第2層（LLM 出力）と第3層（OPA 応答）の
  両方で `ValidationError` を JSON 化するため。`validation.py` の挙動は変えていない
- `validate_action()` / `build_escalation_event()` に **`trace_id` を必須引数として追加**（D-016）。
  Phase 2 のテスト 15 箇所を機械的に更新したが、**66 件はそのままパス**（判定内容は不変）
- **第3層の異常系ポリシーは `mocks/opa_faults/` に置いた。** `opa/` の下に置くと
  `decision` の定義が衝突して OPA が起動しなくなる。障害注入部品を `mocks/` に置く方針は D-006 と同じ
- **`mocks/fault_injector.py` は無改修。** `FAULT_FIXTURE` にサブディレクトリを書けるため
  （`policy/b02_production_delete_pod`）、第3層用の fixture もそのまま経路に流せる
- **`.env` の `OPA_URL` を更新する必要があった。** 旧値 `http://localhost:8181` のままだと
  404 → `http_error` になる。パス誤りが Fail-Closed に落ちること自体は設計どおり

実装中に判明した未決事項の回答（**result.md に転記する**）:

| # | 確認事項 | 判明したこと |
| --- | --- | --- |
| 2 | undefined の正確な形 | パス誤りでも **200 + `{"decision_id": "..."}`**（`result` キーが無い）。404 にはならない。`OpaResponse.result is None` で判定できる |
| 3 | Decision Log の形式 | `input` 全体（`trace_id` を含む）・`result`・`decision_id`・`path`・`metrics` が 1 行 JSON で出る。**レスポンスにも `decision_id` が含まれる**ため突合キーは 2 系統取れる |
| 5 | OPA のバージョン | `latest` は **1.21.0**（Rego v1 が既定）。`docker-compose.yml` で固定した。`import rego.v1` はそのまま有効 |
| 7 | `data.json` のキー衝突 | `data.catalog` はパッケージ `data.sre.agent` と衝突しない。問題なく読める |
| 8 | `opa test` とデータ | `opa test opa/` はディレクトリ内の `data.json` を**自動で読む**。モックは不要（実データと乖離しない） |

`opa` CLI はホストに入っていないため、単体テストは Docker で実行する:

```bash
docker run --rm -v "$PWD/opa:/policies:ro" openpolicyagent/opa:1.21.0 test /policies -v
```

実行（完了。design.md 4.6 の順序で実施した）:

- [x] U-01〜U-08 を `opa test` で通す（**18 件パス**。アプリを起動せずに実行できることも確認）
- [x] P-01〜P-03（allow → `execution.dry_run` まで到達。P-01 は実 LLM で `kubectl_logs` を提案）
- [x] B-01〜B-05（全件 deny・終了コード 3・`execution.dry_run` は出ない）
- [x] D-01（7 ケース × 10 ラウンド = **70 判定の SHA-256 が全一致**。OPA 再起動を挟んでも不変）
- [x] L-01（定常 3.9〜8.4 ms / n=30、初回 57.3 ms → **`OPA_TIMEOUT_SECONDS` は 3.0 を据え置き**）
- [x] X-01〜X-05（7 実行すべて Fail-Closed。各シナリオで注入の確認を記録）
- [x] N-01（ラベル詐称は部分的にしか止められない）/ N-02（第2層バイパスで `allow: true`）
- [x] M-01（`opa/data.json` の 1 行で判定を反転。コンテナの `StartedAt` 不変、`agents/` 差分ゼロ）
- [x] C-01（考察7 に構造比較として記録）
- [x] result.md / decisions.md（D-018 を追加）/ README ロードマップ / TODO.md を更新

---

## Phase 3 の結果サマリ

全シナリオ実施済み。判定基準 1〜7 すべて合格。

| ID | 結果 | 要点 |
| --- | --- | --- |
| U-01〜U-08 | 合格 | `opa test` 18 件。各ルールに allow / deny 両方のケース |
| P-01〜P-03 | 合格 | 実 LLM 経路も含め allow → 実行層（dry-run）まで到達 |
| B-01〜B-05 | 合格 | 全件 deny + 規則 ID + 人間が読む理由。**B-01 は Phase 2 の N-01 と同一入力** |
| X-01〜X-05 | 合格 | 接続不能 / timeout / 500 / undefined / 形の逸脱 3 種すべてで Fail-Closed（終了コード 4） |
| D-01 | 合格 | 70 判定の SHA-256 が完全一致（OPA 再起動を挟む） |
| N-01 | 合格 | 詐称の検知には限界がある（namespace まで揃えられると allow） |
| N-02 | 合格 | 第2層をバイパスすると `allow: true` になる（層の順序の必然性） |
| M-01 | 合格 | ポリシーの差し替えだけで判定が反転（D-002 の実証） |
| L-01 | 計測 | 第3層は第1層と桁が 3 つ違う。`timeout` の再決定は不要だった |

### 実行中に判明した重要事項

- **また注入が効いていなかった。今回は「偽の不合格」に見えた。** `docker compose restart` は
  コンテナを作り直さないため `command` の `${LITELLM_CONFIG_FILE}` が更新されない。
  確認すべきは注入器の状態ではなく**リクエストが実際にその経路を通ったこと**（[D-018](decisions.md)）。
  危なかったのは B-01 が偶然「終了コード 0」で終わっていたこと。見逃せば
  「第3層が本番の PVC 削除を許可した」と誤読していた
- **`default allow := false` を書かなかったことが効いた。** X-04（ポリシー未ロード）で
  OPA は 200 + `result` 無しを返し、「評価して拒否した」と区別できた。
  default を 1 行書いていたら、安全性が不明な操作に承認ボタンを出していた
- **Rego の undefined は allow 側に倒れる。** 第2層をバイパスした入力はルールが発火せず通る。
  層の順序は安全上の必要条件であって、整理のための並びではない
- **第3層は input の真正性を検証できない。** environment と namespace を揃えて詐称されると通る。
  実運用では RBAC / Admission Controller が最後の砦になる
- **違反は複数返る。** Handoff 仕様書の通知パターン3 は 1 件しか想定していない（Phase 4 で要調整）

### 積み残し（Phase 3 としては完了扱い）

- [ ] decision のスキーマが Rego と `agents/schemas.py` で二重管理（片方を変えると Fail-Closed する）
- [ ] 環境と namespace の対応が `opa/data.json` に固定（実運用ではクラスタ由来のデータにすべき）
- [ ] Decision Log の外部永続化は未実施（`docker compose logs opa` で確認しただけ）
- [ ] ポリシーの bundle 化・署名検証は未実施
- [x] `EscalationEvent` に承認期限・承認者ロールの判定材料が無い（Phase 4 で決める）→
      **足さずに済んだ。** 期限はストア（`expires_at`）、ロールは `SLACK_APPROVER_USER_IDS` に置いた
      （phase4 result.md 未決事項11）

### Phase 4 に持ち越す論点

- **通知パターン3 は違反を複数表示できる形にする。** B-01 / B-04 で 2 件返った。
  優先度付けでは解かないこと（通知の都合を Rego に持ち込むと第3層の責務が濁る）
- **`EscalationEvent` に承認期限・承認者ロールの判定材料が無い。** ステートストア側に持たせるか
  イベントに足すかを Phase 4 の設計で決める
- **`policy_override`（人間が deny を上書きして実行した事実）は OPA の Decision Log に残らない。**
  実行側で記録する必要がある（Handoff 仕様書 運用原則4）
- **ボタンの `value` に入るのは Trace ID だけ**（運用原則2）。Phase 3 で導入済み（[D-016](decisions.md)）
- **Slack 経路でも「届いたことの証拠」を先に決めてから実行する**（[D-018](decisions.md)）

---

## Phase 4 の実装と実行（完了）

実施手順は [design.md 第8章](phases/phase4-slack/design.md)。

### Phase 4 (Slack)

**設計は完了**（[design.md](phases/phase4-slack/design.md)）。以下は実施手順（design.md 第8章）の写し。

確定した方針:

- Interactivity は **HTTP + ngrok**（Socket Mode にすると署名検証という検証対象が消えるため。C-01 で構造比較）
- ステートストアは **SQLite**（コンテナを増やさず排他・永続・TTL・監査を同一ファイルに置ける）
- 承認押下は **実行層（dry-run）まで到達させる**。`policy_override` を監査に残す
- 実ワークスペースを使う。送出の異常系は `SLACK_API_BASE_URL` を `mocks/fault_injector.py` に向けて注入する
- **受信と実行を分けない**（[D-024](decisions.md)）。実行層が dry-run ログなので 3 秒ルールに対して桁が 5 つ違う。
  実運用の構成（Receiver / Executor を SQS で分離）との差分は提言に書く
- **Slack は 3 秒を超えると同じ payload を再送する。** 二重押下の検証は連打（A-03）ではなく
  再送（G-06）を主軸にする。pytest で決定的に再現できるため
- **ステートの語彙は Phase 4 で到達しないものも含めて決め切る**（design.md 3.5.1）。
  とくに `execution_failed`（確定的な失敗・再試行可）と `unknown`（実行有無が不明・再試行不可）を分ける

実装（**完了 2026-10-01**。単体テスト 47 件追加、全 140 件パス）:

- [x] `slack/blocks.py`（`EscalationEvent` → Block Kit。純関数。Slack SDK を import しない）
- [x] `slack/masking.py`（AWS キー / Slack トークン / Bearer / key=value / URL 埋め込み認証）
- [x] `slack/store.py`（SQLite。CAS / TTL / 冪等 / TOCTOU / 監査ログ）
- [x] `slack/notifier.py`（`chat.postMessage` / スレッド返信 / `chat.update` / エフェメラル）
- [x] `slack/handler.py`（Bolt。署名検証・RBAC・CAS・復元時の再検証・実行層・監査）
- [x] `slack/expire.py`（TTL 満了検知ジョブ。`--dry-run` つき）
- [x] `agents/escalation.py`（ハンドオフ経路の 1 本化。既存のログ行は不変）
- [x] `agents/execution.py`（`execute_dry_run()` を切り出し。承認経路からも同じ関数を呼ぶ）
- [x] `agents/main.py` / `validation.py` / `policy.py`（終了コード 5 / `--no-slack`）
- [x] `tests/test_slack_blocks.py`（S-01〜S-08 + D-01）/ `test_slack_store.py`（T-01〜T-07）/
      `test_slack_signature.py`（G-01〜G-06 + RBAC）/ `test_escalation.py`（E 系の単体側）
- [x] `tests/fakes.py` / `conftest.py`（**全テストで Slack 送出を隔離する autouse fixture**）
- [x] `slack/preview.py`（通知パターンのプレビュー）/ `slack/readback.py`（届いたことの読み戻し）/
      `slack/app_manifest.yml`（Slack App の構成。スコープの取り違えを防ぐ）
- [x] `.env.example` / `.gitignore`（承認者リスト / TTL / TOCTOU / `SLACK_API_BASE_URL` / ストア）

設計時に想定していなかった実装上の追加・差分（**いずれも result.md に転記する**）:

- **ストアの `approve` に承認期限の判定を CAS の中へ入れた。** design.md 3.5 の表は
  有効期限を満了ジョブの責務としていたが、それだと「期限は過ぎたがまだ pending」の窓で
  承認が通る。**期限切れは満了ジョブの都合であって承認可否の条件である**ため、
  `UPDATE ... WHERE status='pending' AND expires_at > ?` に含めた（T-03）。
  却下は期限後も通す（人間が明示的に断る操作は常に安全側）
- **`execution_failed` に Phase 4 で到達する。** design.md 3.5.1 は「実行層が dry-run なので
  到達しない」と書いたが、**ストアからの復元に失敗した場合**（T-07）が該当した。
  実行を試みて確定的に失敗しており、ストアを直せば再試行できるので `unknown` ではない
- **Slack API 呼び出しを notifier に一本化した。** Bolt は**リクエストごとに新しい `WebClient` を
  組み立てる**（`app.py` の `client_per_request`）ため、リスナーに注入される client では
  `SLACK_API_BASE_URL` による障害注入（E-04）が効かず、テストからも差し替えられない
- **`token_verification_enabled` の既定は True にする**（未決事項12 への部分回答）。
  True だと Bolt は**起動時に 1 回だけ** `auth.test` する。False にすると**リクエストごとに**飛ぶため、
  3 秒ルールの観点でも True が正しい
- **テストが実 Slack API に出ていた。** 上記を踏むまで、`auth.test` が毎リクエスト
  `https://slack.com/api/auth.test` を叩いていた。`conftest.py` に autouse の隔離 fixture を置き、
  `SLACK_*` を落として ストアを一時ファイルに向けるようにした（**Phase 2・3 のテストも保護される**）
- **RBAC の承認者リストが未設定なら誰も承認できない。** 未設定が「全員許可」になるほうが
  事故として重いため、認可も Fail-Closed に倒した
- **`--no-slack` / 未設定時に `slack.skipped` の行が 1 行増える。** 既存の行は 1 バイトも
  変わっていない（`test_escalation.py` でキー構成ごと固定した）が、行数は増える
- **提案アクションをシェルコマンド文字列として描画しない。** `kubectl delete pvc ...` の形に
  すると (1) 操作ごとのテンプレートが通知層に入り D-021 に抵触し、(2) 発行しないコマンドを
  人間に見せることになる。汎用の整形（`operation / kind/name @ namespace (env) dry_run=`）にした

事前準備（実機）。**チャンネルを作らず Bot との DM で検証する**:

- [x] Slack App を作成（**From manifest** に [slack/app_manifest.yml](../slack/app_manifest.yml) を貼る。
      新 UI に「From scratch」は無く、手で作る場合は **Blank app**）。
      Bot トークン（`xoxb-`）と署名シークレットを取得
- [x] Bot スコープ（manifest に含む）: `chat:write` / `im:write`（DM を開く）/
      `im:history`（**判定基準7 の読み戻し**。チャンネルで行う場合は `channels:history`）。
      **この 3 つで足りた**（`auth.test` の `x-oauth-scopes` で確認。未決事項3）
- [x] `SLACK_CHANNEL_ID` に**自分のユーザ ID（`U...`）** を入れる。
      `conversations.open` で DM（`D...`）に解決される（`slack/notifier.py`）
- [x] `SLACK_APPROVER_USER_IDS` に自分のユーザ ID。**未設定だと誰も承認できない**
- [x] `python -m slack.preview --pattern 3` で通知を 1 本出す（**2026-10-01 実施。DM に着弾を確認**）
- [x] `python -m slack.readback` で読み戻し（`value` が Trace ID のみ・confirm つきを実機で確認）
- [x] ngrok 等で Interactivity Request URL を用意（**ローカル URL では検証できない**）。
      転送先は `SLACK_HANDLER_PORT`（既定 3000）、パスは `/slack/events`。
      本フェーズでは cloudflared を使用。**押下を伴うシナリオの大半（G-06 / N-01 / E-04）は
      署名を自前で計算した直接 POST で駆動でき、トンネル無しで実施できた**（result.md 考察8）

**DM で検証できないこと**（result.md に限界として記録する）:

- 運用原則5 の「チーム全体への状況同期」。DM は自分にしか見えない
- A-03 の「複数オペレーターによる同時押下」。**同一人物の連打と G-06（Slack の再送）で代替する**
  （もともと判定基準の主軸は G-06 に置いている。D-024）

実行（design.md 4.7 の順序）。**2026-10-01 に実機検証を実施（Slack DM + cloudflared）**:

- [x] S 系（Block Kit 単体）→ T 系（ストア）→ G 系（署名検証）を pytest で通す（**145 件パス**）
- [x] **A-01 承認 → 実行層（dry-run）→ `chat.update` → 監査に `policy_override`**（判定基準5・7）
- [x] **A-02 却下** / **A-05 RBAC 拒否**（承認者リストから外して押した。`denied_by_rbac` を記録）
- [x] **A-03 二重押下 → G-06（Slack の再送）で代替**。署名付きリクエストを 2 回投げ、
      2 回目は `lock_conflict`。監査の `execute` は 1 件だけ（[D-024](decisions.md) どおり）
- [x] **A-04 TTL 失効**（TTL 60 秒 → `slack.expire`）。元メッセージを「期限切れ・未実行」に更新し、
      再通知は**ボタン無し**で送出
- [x] **E-01〜E-03 送出障害**。3 件とも `notify_failed` / `delivery_failed` / 実行層に未到達。
      **注入が届いた証拠を injector のアクセスログで取得**（[D-018](decisions.md)）
- [x] **P-01〜P-03**（**実際の層の失敗から** 3 パターンを出した）。終了コード 3 / 1 / 4、
      **ストアにエントリができたのは P-01 だけ**、パターン4 にボタン無し
- [x] **E-04**（`chat.update` だけ失敗）。承認・実行は成立し UI だけが古い。再押下は `lock_conflict`
- [x] **N-01**（ストア改変）。型違反は弾けたが、**型として正しい別アクションへの改変は通った**
- [x] **N-02**（handler 停止中の押下）。ストアがバイト単位で一致。**押下の事実が残らない**
- [x] **D-01**（決定性）。3 パターン × プロセス内 10 回 + 別プロセス 3 回で SHA-256 完全一致
- [x] **L-01**（計測）。`chat.postMessage` p50 0.220 / max 0.530 秒（n=50）→ **10.0 秒を据え置き**
- [x] **未決事項2 の実測**。Slack は上限超過を黙って切らず `invalid_blocks` で拒否する
- [x] C-01（Socket Mode との構造比較）/ C-02（LiteLLM alerting との比較）→ result.md 考察8・9
- [x] result.md を書く（[phase4-slack/result.md](phases/phase4-slack/result.md)）

実機で見つかった実装の不備（**押してみなければ出なかったもの**。result.md 考察に回す）:

- **RBAC ミドルウェアが 404 を返していた。** `next()` を呼ばずに終えると Bolt が
  レスポンスを作れず 404 になり、**Slack からは失敗に見えて再送の対象**になる（3 秒ルール）。
  拒否は正常な処理結果なので `BoltResponse(status=200)` を返すよう修正。
  併せて Bolt がリクエスト本文（Slack の verification token を含む）を警告ログに
  出していたのも止まった
- **承認期限の表示が実効値でなかった。** TTL 15 分を表示しながら TOCTOU 5 分で `stale` に
  倒れていた（実機で踏んだ）。**実効的な窓は短いほうで決まる**ため、
  `actionable_until()` = min(承認期限, 提案時刻 + TOCTOU 閾値) を表示するよう修正。
  Handoff 仕様書の既定値（TTL 15 分 / STALE 5 分）はこの矛盾を内包している
- **`stale` の監査に「誰が押して弾かれたか」が残っていなかった**（`actor=None`）。
  押した事実は記録すべきなので `actor` を渡すよう修正
- **最初に失敗するのは `chat.postMessage` ではなく `conversations.open`。**
  送出先を DM にしたため API 呼び出しが 1 段増えた（チャンネル宛てには無い経路）
- `違反件数` フィールドは見出しと二重だったため、**承認期限**に差し替えた

## Phase 4 の結果サマリ

全シナリオ実施済み。判定基準 1〜9 すべて合格。
結果・考察・提言は [phases/phase4-slack/result.md](phases/phase4-slack/result.md)。

| ID | 結果 | 要点 |
| --- | --- | --- |
| S / T / G 系 | 合格 | pytest 145 件。ワークスペース無しで決定的に検証できる |
| P-01〜P-03 | 合格 | 実際の層の失敗から 3 パターン。**ストアにエントリができたのは P-01 だけ** |
| A-01〜A-05 | 合格 | 承認 → 実行層（dry-run）→ `chat.update` → 監査に `policy_override` |
| G-06 | 合格 | Slack の再送で実行は 1 回だけ（ヘッダではなくストアの CAS で弾く） |
| E-01〜E-03 | 合格 | 送出失敗。遮断は成立したまま `notify_failed` / 終了コード 5 |
| E-04 | 合格 | `chat.update` だけ失敗。**UI だけが古く、ストアが正**。再押下は `lock_conflict` |
| N-01 | 合格（限界） | ストア改変。型違反は弾けるが、**型として正しい別アクションは通る** |
| N-02 | 合格（限界） | handler 停止中の押下。**押下の事実がどこにも残らない** |
| D-01 | 合格 | 3 パターン × 13 回生成の SHA-256 が完全一致 |
| L-01 | 計測 | `chat.postMessage` p50 0.220 / max 0.530 秒（n=50）。`timeout` は 10.0 据え置き |

### 実行中に判明した重要事項

- **期限切れは満了ジョブの都合ではなく承認可否の条件だった**（[D-025](decisions.md)）。
  ジョブに任せると「期限は過ぎたがまだ pending」の窓で承認が通る。CAS の 1 文に入れる。
  **却下は期限後も通す**（人間が明示的に断る操作は常に安全側）
- **押してみなければ出なかった不備が 4 件あった**（単体テストは全部通っていた）。
  共通点は「1 回の押下」ではなく**押下と状態の関係**に関する不備であること
- **切り詰めは表示の都合ではなく送出可否そのものだった。** Slack は上限超過を黙って切らず
  `invalid_blocks` で拒否する。切り詰めが無ければ長い reason で通知が全損していた
- **送出の失敗は 3 系統に残るが、受信の失敗はどこにも残らない。** 多層防御の最外周には外側が無い
- **検証の大半はトンネル無しで回せた。** 署名を自前で計算した直接 POST で handler の検証経路は
  そのまま通る。トンネルが要るのは「人間が実際に押す」ときだけ

### 積み残し（Phase 4 としては完了扱い）

- [ ] **`execution_failed` に `executed_at` が立つ**（実行層に到達していないのに時刻が入る）。
      排他は `status` の CAS で行っているので実害は無いが、列名と意味がずれている。
      design.md 3.5.1 は `execution_failed` を「再試行可」としているが、
      **現実装には戻す遷移が無く終端状態**である（result.md 考察11）
- [ ] **`chat.postEphemeral` が DM で失敗する**（`invalid_arguments`）。
      RBAC 拒否と競合の通知が本人に届かない。チャンネルで検証すれば解消するはず（未検証）
- [ ] A-03 の「複数オペレーターによる同時押下」は DM では実施できていない（G-06 で代替）
- [ ] 運用原則5 の「チーム全体への状況同期」は DM では検証できていない
- [ ] LiteLLM の alerting に `trace_id` を載せられるかは未確認（result.md 考察9）
- [ ] 承認待ちエントリの死活監視（N-02 の提言2）は未実装

### Phase 5 に持ち越す論点

- **実行が本物になると壊れる前提が 3 つある**: `ack()` の 3 秒ルール / handler の権限 /
  `execution_failed` の意味（result.md 申し送り）
- **Phase 4 で注入できなかったものが Phase 5 で注入できる**: 通知パターン5（実行時エラー）、
  `unknown` ステート、`execution_failed` からの再試行、承認待ち中に対象が変わる TOCTOU

---

## Phase 5 の設計と実施手順

**設計は完了**（[design.md](phases/phase5-e2e/design.md)）。以下は実施手順（design.md 第8章）の写し。

確定した方針:

- **実行層を本物にする。** 対象は使い捨ての kind クラスタのみ。実クラスタへは発行しない（[D-026](decisions.md)）。
  `ProposedAction.dry_run` を **server-side dry-run** に対応づける（初めてこの値が意味を持つ）
- **受信と実行を分離する**（[D-027](decisions.md)）。`slack/handler.py` は `approved` にするところまでで終え、
  実行は `agents/executor.py`（常駐）が行う。**handler に kubeconfig を与えない**。
  [D-024](decisions.md) が明示していた「実行が実 API 呼び出しになった時点で成立しない」条件が満たされたため
- **実行結果は「サーバからの応答があったか」だけで 3 分類する**（[D-028](decisions.md)）。
  2xx → `executed` / 4xx → `execution_failed` / 5xx・タイムアウト・接続断 → `unknown`。
  **`unknown` に再試行ボタンを出さない**（パターン4 に承認ボタンを出さないのと同じ規律）
- **再開は「アクションの実行」であって「思考の再開」ではない**（[D-029](decisions.md)）。
  承認後に LLM を呼び直さない。**これが [D-001](decisions.md) の再検討に対する結論**
- **実行層は Kubernetes API クライアント**（[D-030](decisions.md)）。`kubectl` のサブプロセスにしない。
  裏取りには `kubectl` を使う
- 終了コードに **6**（確定的な失敗）と **7**（実行有無が不明）を足す。**7 が最重**
- ngrok / cloudflared は**人間が実際に押すシナリオだけ**に要る（Phase 4 考察8）

実装（**2026-10-01 に単体まで完了。pytest 220 件パス**）:

- [x] `k8s/kind-cluster.yaml` / `workloads.yaml` / `rbac.yaml` / `audit-policy.yaml` /
      `setup.sh` / `make-kubeconfig.sh`（namespace は `opa/data.json` と一致させた）
- [x] `agents/k8s.py`（`operation × kind` → API のディスパッチ表 **16 件**。語彙の漏れは K-06 で検査）
- [x] `agents/execution.py`（実 API の発行。**応答の有無だけで 3 分類**して返す）
- [x] `agents/executor.py`（常駐。`claim` → 実行 → 記録 → 通知。起動時に `executing` を `unknown` に倒す）
- [x] `slack/store.py`（`executing` / `abandoned`、`execution_started_at`、`retry_count`、
      `claim_for_execution()` / `retry()` / `abandon()` / `recover_executing()`、既存 DB のマイグレーション）
- [x] `slack/blocks.py`（パターン5 の 2 種。ボタンの有無は `kind` と `status` だけで決まる）
- [x] `slack/handler.py`（**`agents.execution` の import を外した**。承認は `approved` まで。再試行 / 中止を追加）
- [x] `agents/schemas.py` / `agents/escalation.py` / `agents/main.py`（終了コード 6 / 7）
- [x] `tests/test_execution_dispatch.py`（K 系 **63 件**）/ `tests/test_executor_state.py`（W 系 12 件）/
      `tests/fakes.py` の `FakeApis` / `conftest.py`（**テストが実クラスタに触れないようにする**）
- [x] `.env.example` / `requirements.txt`（`kubernetes>=31`）
- [ ] `mocks/k8s_proxy.py`（X-02 用。**`fault_injector.py` は改修しない**）

**Phase 4 のテストを Phase 5 の構成に合わせて更新した**（承認と実行が別プロセスになったため）。
T-05 / T-06 / T-07 / G-01 / G-05 / G-06 は `handler.approve()` 1 本から
「承認 → `executor.run_once()`」の通しに変えた。**確かめている性質は変えていない**
（押した内容と実行内容の一致・壊れたレコードが実行層に到達しないこと・再送でも実行は 1 回）。
むしろ「実行層のログが無い」ではなく**「API を 1 度も呼んでいない」**で見るようになり、厳しくなっている。

事前準備（**完了**）:

- [x] kind v0.33.0 を導入（brew）。ノードイメージを
      `kindest/node:v1.37.0@sha256:a1ed56cf…` に**ダイジェストまで固定**
- [x] `./k8s/setup.sh` でクラスタ構築 → **`kind delete cluster` で消せることを先に確認し、作り直した**
- [x] ServiceAccount の長命トークン（`kubernetes.io/service-account-token` の Secret）から
      kubeconfig を生成し `var/` に置いた（**未決事項2 の回答**。`kubectl create token` の短命トークンは
      検証中の失効が RBAC 403 と区別できないため使わない）
- [x] `kubectl auth can-i` で権限表（design.md 3.10）と一致を確認
      （`assets/prep-auth-can-i.txt`）

実行（**完了 2026-10-01**。design.md 4.7 の順序で実施した）:

- [x] K 系（63 件）→ W 系（12 件）を pytest で通す。**クラスタを作る前に固めた**
- [x] **R 系（RBAC）。R-01 が本フェーズの中心**: 第3層が deny → 人間が `policy_override` で承認 →
      **403 で止まり、PVC は同一 UID のまま残った**。3 件とも受信プロセスに kubeconfig なし
- [x] Z 系（E2E 通し 7 本。実 LLM 版と fixture 固定版の両方）。前後で `resourceVersion` / `uid` を取り、
      Z-02 / Z-04 / Z-05 は**クラスタが完全に無変化**であることを確認
- [x] X 系（実行の異常 6 本）。**X-01b で「実行されたのに unknown」を両面から記録した**
- [x] N 系（限界 3 本）/ D-01（SHA-256 完全一致）/ L-01（p50 1.2 ms / max 20.9 ms）/ C 系（考察9〜11）
- [x] 回帰（pytest 220 件・`opa test` 18 件・Phase 1 F-01 の再実行）
- [x] result.md / decisions.md（D-026〜D-030 に実測を追記）/ CLAUDE.md / README / TODO.md を更新

実行中に追加したもの:

- [x] `mocks/k8s_proxy.py`（4 モード。標準ライブラリのみ）/ `k8s/make-fault-kubeconfig.sh`
- [x] `litellm/config.fault_all.yaml`（**第1層の全滅**。Phase 1 の N-01 が偶発的な観測しか
      残していなかった穴も埋めた）
- [x] `k8s/workloads.yaml` に**名前が固定された Pod**（`payment-worker`）。
      Deployment の Pod は名前が毎回変わり、fixture から決定的に指名できない
- [x] `tests/fixtures/phase5/`（R / Z / N 系の入力 7 件）

### Phase 5 の結果サマリ

全シナリオ実施済み。判定基準 1〜12 すべて合格。

| ID | 結果 | 要点 |
| --- | --- | --- |
| K / W 系 | 合格 | pytest 220 件。実行層のディスパッチと結果分類をクラスタ無しで固定 |
| R-01〜R-04 | 合格 | **第3層が deny → 人間が承認 → RBAC が 403 で止める。** 対象は同一 UID のまま残存 |
| Z-01〜Z-06 | 合格 | 入口からクラスタの状態変化まで到達。止まったときは `resourceVersion` が完全一致 |
| Z-05 | 合格（限界） | 第1層の全滅では **Slack に 1 件も出ない**（多層防御で唯一人間に届かない層） |
| X-01b | 合格 | **`generation` 2→3 と変わっているのに記録は `unknown`**。D-028 の実例 |
| X-02〜X-05 | 合格 | 5xx / 接続断 / クラッシュ / 再試行投入すべて安全側。`unknown` は再試行させない |
| N-01 | 合格（限界） | 同名で作り直された対象を消してしまう。時間ベースの TOCTOU では防げない |
| N-02 | 合格（限界） | **`opa/data.json` の 1 行で本番 Pod 削除が `allow: true` になり、RBAC だけが止めた** |
| N-03 | 合格（限界） | executor 停止中は `approved` で滞留。Slack 上は「承認済み」に見える |
| D-01 | 合格 | 実行層の組み立て・パターン5 とも 10 回の SHA-256 が完全一致 |
| L-01 | 計測 | p50 1.2 ms / p95 3.8 ms / max 20.9 ms（n=140）。`timeout` 10.0 は max の約 478 倍 |

### 実行中に判明した重要事項

- **「実行されたのに不明」は実在した**（X-01b）。上流が 200 を返し `generation` が変わったのに
  応答だけが届かない。**5xx や無応答を「失敗・再試行可」としていたら本番を 2 回再起動していた**
- **もっとも重要な注入が設計どおりには効かなかった**（考察2）。kind は localhost にあるため、
  タイムアウトを縮めても**リクエストが届く前に**切れる。要るのは「送った後に切ること」だった。
  **ローカルで検証すると再現しにくくなる障害がある**
- **ポリシーを独立に変更できるということは、独立に壊せるということでもある**（N-02）。
  第3層は自分の参照データが間違っていることを検知できない
- **分離は権限のために選んだが、付いてきたのは「落ちたことを観測できる」性質だった**（考察4）
- **[D-001](decisions.md) の再検討は「代償ではなかった」で決着した**（考察11）。
  checkpointer が解くのは思考の再開であって、本 PoC が必要としたのはアクションの再開

### 事前準備・実装で判明したこと

- **未決事項4 の回答: server-side dry-run は `delete` にも効く。** `dryRun=All` で **200** が返り、
  監査ログにもリクエストが残る一方、Pod も Deployment も変化しない。
  「黙って実行しない」のではなく「API サーバまで届いて永続化されない」ことを実測できた
- **未決事項8 の回答: kind で監査ログを有効にできる。** k8s 1.31 以降の kubeadm は v1beta4 で
  **`extraArgs` が map ではなくリスト**になっている点だけ注意。403 が RBAC によるものである裏取りは
  `authorization.k8s.io/decision: forbid` で取れる
- **未決事項5 の回答: タイムアウト時に送信済みかは判別できない。** Python クライアントが
  `ApiException` に変換するのは `SSLError` だけで、他は urllib3 の例外がそのまま出る。
  **これ自体が [D-028](decisions.md) の裏付けになった**
- **`~/.kube/config` へのフォールバックを禁止した。** `load_kube_config()` を引数なしで呼ぶと
  kind の **admin** が拾われ、**RBAC で止まるはずの操作が全部通って判定基準5 が静かに無意味になる**。
  専用の `K8S_KUBECONFIG` を必須にした（未設定なら実行しない）
- **K-06（語彙の漏れ検査）が自分のコメントで落ちた。** 「語彙を書くな」と書いた注釈に語彙が入っていた。
  検査に例外を足さず**コメントのほうを書き換えた**（例外を足すと検査が緩む）
- **エビデンス生成スクリプトが全件 `no` を返していた**（zsh は未クォートの変数を単語分割しない）。
  危うく「権限が一切付いていない」という誤った証拠を保存するところだった。
  **全部同じ値のときほど疑う**（Phase 2 の教訓の変奏）
- **`ruff --fix` が `mocks/fault_injector.py` を書き換えていた**（`# noqa: N802` の削除）。
  design.md が「改修しない」と定めたファイルなので revert した。自動修正の範囲にも注意がいる

### 積み残し（Phase 5 としては完了扱い）

- [ ] executor を**別プロセスとして複数**同時起動する検証（W-01 はスレッドで確認）
- [x] 常駐モードのポーリング遅延の体感評価 → **2026-10-02 に解消。** 承認 07:43:31.59 →
      実行 07:43:32.00 で、`EXECUTOR_POLL_SECONDS=2.0` に対し体感遅延は 2 秒以内。
      待たされている感覚は無かった
- [ ] `retry_count` 上限到達後の通知文面（ボタンは消えるが、上限到達を文面で明示していない）
- [ ] 実クラスタ（ネットワーク越し）でのレイテンシと `unknown` 発生率
- [ ] `kubectl_describe` の代替実装は Event を数えるだけで、人間が読む情報になっていない
- [ ] node（クラスタスコープ）に `namespace` が申告される非対称の整理
- [ ] 監査ログの外部保全・ポリシー bundle の署名検証（Phase 3・4 からの継続）

---

## 通しの実機再現とハーネス整備（2026-10-02）

Phase 5 の判定は [phases/phase5-e2e/result.md](phases/phase5-e2e/result.md) が正で、**ここはそれを
変えるものではない**。やったのは「もう一度同じことを人の手で通せるか」の確認と、その配線の整備。

### 通した 1 本

trace `a9d5311a-c46e-40d2-b3d8-482c4b4f6686`

| 層 | 結果 |
| --- | --- |
| 第1層 | `primary-jp`（`jp.anthropic.claude-sonnet-4-6`）。retries 0 / fallbacks 0 |
| 第2層 | `validation.ok`。LLM は自分で `dry_run: false` を選んだ |
| 第3層 | `policy.deny` — `REQUIRE_DRY_RUN_IN_PRODUCTION` |
| エスカレーション | `slack.notified` → 63 秒後に承認（`policy_override`） |
| 実行層 | `execution.applied` → `executor.executed`。`rollout restart deployment prod/payment-api generation=3` |

クラスタ側の裏取り: `restartedAt` が `2026-10-01T10:06:28` → `2026-10-02T07:43:31.970792+00:00`、
ReplicaSet が `7f95dcf454`(2/2) → `77cc5c5fb6`(2/2)、旧 RS は 0 にスケールダウン。

**プロンプトには「危険な操作を避けよ」を一切書いていない。** 止めたのは確率的な部品ではなく
第3層であり、それを人間が通した事実は `policy_override` として監査に 2 行残っている。

### 整備したもの

- `scripts/e2e.sh` — 前提確認（`check`）・常駐 3 本の起動（`up`）・インシデント投入と承認待ち（`run`）・
  事後検証（`verify`）・停止（`down`）
- `scripts/README.md` — アーキテクチャ図、端末と層の対応、手順、出力の読み方、成立の条件、つまずきどころ
- `architecture.md` を Phase 5 の実態に合わせた（実行層が dry-run のままだった）
- `commands.md` の疎通確認を `/health/liveliness` に修正（`/health` はマスターキーが要る）

**層は 1 つも代行していない。** 承認ボタンの押下と各層の判定は自動化の対象外（[../scripts/README.md](../scripts/README.md)）。

### ハーネス側で踏んだ失敗（lessons.md に追加済み）

- **ERR トラップが `PIPESTATUS` を上書きし、第3層の `deny`(3) を「通過」(0) と報告した。**
  層は正しく止めていて、嘘をついたのは観測側だった。**層の出力を加工して受け取る場所は、
  それ自体が検証対象になる**
- `set -e` + `pipefail` の下で空振りした `grep` がスクリプトを無言で殺した
- 承認待ちを無音にしていたため、待っているのか固まっているのか区別できなかった
- 実行直後のスナップショットが Rolling Update の途中を写した

### ここから出た積み残し

- [ ] `scripts/e2e.sh` の `up` / `run` に自動テストが無い（`check` / `status` / `verify` は実行で確認済み）
- [ ] 異常系（`execution_failed` / `unknown` / `stale`）を `e2e.sh` から再現する導線が無い。
      いまは [commands.md](commands.md) の障害注入を手で併用する必要がある
- [ ] トンネルの URL が変わるたびに Slack App の Request URL を手で張り替えている

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
