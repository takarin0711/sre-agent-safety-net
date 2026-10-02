# CLAUDE.md

このリポジトリで作業する際のガイド。プロジェクトの全体像は [README.md](README.md) を参照。

## このリポジトリは何か

SRE エージェント（LLM）を多層防御で囲い込む **PoC 検証用リポジトリ**。プロダクションコードではない。
「各層が期待どおり異常を検知・遮断するか」を実証することが目的であり、機能の充実や汎用化は目的ではない。

**異常系は「壊して確かめる」のが本体。** 壊れた JSON の注入、禁止コマンドの投入、ポリシーの削除といった
故意の failure injection がこのリポジトリの主要な作業であり、バグではない。

Phase 1〜5 はすべて完了済み。**現在地と残タスクは [docs/TODO.md](docs/TODO.md) が正。**
作業を再開するときは、まず TODO.md を読むこと。

## ドキュメントの地図

| 知りたいこと | 読む先 |
| --- | --- |
| 現在地・残タスク | [docs/TODO.md](docs/TODO.md) |
| 層の責務と層間インターフェース | [docs/architecture.md](docs/architecture.md) |
| なぜこの構成なのか（D-001〜D-030） | [docs/decisions.md](docs/decisions.md) |
| **過去に踏んだ失敗と検証の規律** | [docs/lessons.md](docs/lessons.md) |
| コマンド全集（障害注入・kind・Slack） | [docs/commands.md](docs/commands.md) |
| 各層の検証シナリオと判定基準 | `docs/phases/phaseN-*/design.md` |
| 実行結果とエビデンス | `docs/phases/phaseN-*/result.md` |

| 層 | 実装 | フェーズ | 設計判断 |
| --- | --- | --- | --- |
| 第1層 可用性 | LiteLLM Proxy（HTTP） | [phase1-litellm](docs/phases/phase1-litellm/design.md) | D-003〜D-009 |
| 第2層 構造保証 | Pydantic v2（インプロセス） | [phase2-pydantic](docs/phases/phase2-pydantic/design.md) | D-010〜D-014 |
| 第3層 権限判定 | OPA / Rego（HTTP） | [phase3-opa](docs/phases/phase3-opa/design.md) | D-015〜D-018 |
| エスカレーション | Slack Bolt（Block Kit） | [phase4-slack](docs/phases/phase4-slack/design.md) | D-019〜D-025 |
| 実行層 | Kubernetes API クライアント | [phase5-e2e](docs/phases/phase5-e2e/design.md) | D-026〜D-030 |

**層は役割名で呼び、実装名では呼ばない**（第2層 = JSON Validator。[D-013](docs/decisions.md)）。

## 技術スタック

- Python 3.12 / venv + pip（`requirements.txt`, `requirements-dev.txt`）— uv や Poetry は使わない
- エージェント本体は素の Python + **OpenAI SDK**（LiteLLM Proxy に OpenAI 互換エンドポイントとして接続）。
  **LangGraph 等のエージェントフレームワークは導入しない**（[D-001](docs/decisions.md)）。
  層の責務と検証結果を曖昧にしないため
- 依存サービスは Docker Compose。実行層の対象は使い捨ての kind クラスタのみ
- モデルは `claude-sonnet-4-6` に固定し、`jp.` / `us.` の推論プロファイルでマルチ地理圏に振る。
  `global.` は US を包含するため fallback 先には使わない（[D-008](docs/decisions.md)）

## よく使うコマンド

```bash
source .venv/bin/activate
docker compose up -d              # 正常系。設定を変えたときは up -d --force-recreate
python -m agents.main --incident "本番の payment-api の Pod が CrashLoopBackOff"
python -m agents.executor         # 実行プロセス（常駐。唯一 K8S_KUBECONFIG を持つ）
python -m slack.handler           # Interactivity の受信（常駐。ngrok の転送先）
pytest                            # 第2層の単体検証 + 層の境界の強制
```

`--incident` の終了コード: 0 実行成功 / 1 第1層・第2層の失敗 / 2 設定不足 / 3 deny / 4 評価不能 /
5 通知の送出失敗 / 6 実行が確定的に失敗 / 7 実行の有無が不明（最重）。

**障害注入・kind・Slack preview・Rego テストは [docs/commands.md](docs/commands.md)。**

## コーディング規約

- 型ヒントは必須。層をまたぐデータ構造は必ず Pydantic モデルとして `agents/schemas.py` に定義する
- 層の境界を越えるデータは JSON。**第1層・第3層の実装コードは import しない**（HTTP で疎結合を保つ）。
  第2層は同一プロセスのライブラリなので import して使う（[D-010](docs/decisions.md)）
- **例外は握り潰さない。** 各層の失敗は「検知して Slack へハンドオフする」ことが検証対象そのものなので、
  `except Exception: pass` のようなコードは PoC の目的を破壊する
- ログは各層の判定結果（フェイルオーバー発火・`ValidationError`・`allow` の値と理由）が
  後から追えるように出力する。検証のエビデンスになる
- コメント・ドキュメント・Slack メッセージは日本語。コード識別子は英語

## 層をまたぐ不変条件

**コードを書くときに常に効く制約。** 背景は各 `design.md` と [decisions.md](docs/decisions.md)。

### 第1層・第2層

- エージェントには論理モデル名 `sre-agent-model` だけを見せる。リージョンと実モデル ID は LiteLLM 側の関心事
- **リトライ（同一デプロイへの再試行）とフォールバック（別デプロイへの切り替え）を必ず切り分ける。**
  ベースラインは `num_retries: 0`
- 第2層のモデルには `strict=True` と `extra="forbid"` を必ず付け、入口は `model_validate_json()` を通す
  （`json.loads()` を挟まない）。既定の lax モードは `"3"` → `3` を黙って変換する
- **LLM 出力を修復しない**（[D-012](docs/decisions.md)）。コードフェンス剥がしも `ValidationError` からの
  再プロンプトも入れない。修復すると「決定的に検知できる」という検証対象そのものが消える
- **構造化出力の強制をベースラインに入れない**（[D-011](docs/decisions.md)）。壊れた入力が来ないと検知能力が測れない
- プロンプトには**出力スキーマは書く**が、**「危険な操作を避けよ」は書かない**。
  書いた時点でその防御が確率的になる（第3層の責務。[D-002](docs/decisions.md)）

### 第3層

- **`default allow := false` を書かない。** ポリシー未ロードは undefined として表面化させ、
  エージェント側が Fail-Closed する。default を置くと通知パターン 3 と 4 が区別できなくなる
- **deny は戻り値、評価不能は例外**（`PolicyUnavailableError`）（[D-015](docs/decisions.md)）
- OPA の応答も `strict=True` / `extra="forbid"` で検証し、自己矛盾は評価不能に倒す（[D-017](docs/decisions.md)）
- **ポリシーの判定に使う語彙を `agents/` に書かない。** `production` や `kubectl_delete_*` による
  条件分岐は `opa/` の中だけに存在する（pytest で強制）。例外は `schemas.py` / `prompts.py`
- `input` は `{"action": ..., "trace_id": ...}`。**Rego は `trace_id` を参照しない**（[D-016](docs/decisions.md)）

### エスカレーション・実行層

- 送出はインプロセス、**受信は独立プロセス**（[D-019](docs/decisions.md)）。
  送出の失敗をそのインシデント処理の結果（終了コード 5）に反映させるため
- **受信プロセスはクラスタの資格情報を持たない**（[D-027](docs/decisions.md)）。`slack/handler.py` から
  `agents.execution` / `agents.k8s` / `kubernetes` に到達できないことを pytest で強制している
- ボタンの `value` は Trace ID のみ。実行内容はストアから復元し、**復元時に第2層で再検証する**（[D-020](docs/decisions.md)）
- 排他はストアの CAS に任せる。**`X-Slack-Retry-Num` で弾かない**（[D-024](docs/decisions.md)）
- 承認期限の判定は承認の CAS の中に入れる。**却下は期限後も通す**（[D-025](docs/decisions.md)）。
  表示する期限は実効値 = min(承認期限, 提案時刻 + TOCTOU 閾値)
- **確認強度・承認権限を操作の種別で出し分けない**（[D-021](docs/decisions.md)）。
  危険度は第3層が `violated_rules` として既に返している
- 承認時に第3層へ再問い合わせしない。`policy_override` として監査に残す（[D-023](docs/decisions.md)）
- **承認後に LLM を呼び直さない。** 再開は「アクションの実行」であって「思考の再開」ではない（[D-029](docs/decisions.md)）
- **`~/.kube/config` へフォールバックしない。** 専用の `K8S_KUBECONFIG` が未設定なら実行しない
- 実行結果は応答の有無で 3 分類: 2xx → `executed` / 4xx → `execution_failed` /
  **5xx・タイムアウト・接続断 → `unknown`**。**`unknown` に再試行ボタンを出さない**（[D-028](docs/decisions.md)）
- 実行は Kubernetes API クライアント、**裏取りは `kubectl`**（[D-030](docs/decisions.md)）
- 実行層にもポリシーの語彙を書かない（`agents/k8s.py` / `execution.py` / `executor.py`。pytest で強制）
- 送出の失敗（`notify_failed` / 終了コード 5）・実行の失敗（`execution_failed`）・
  更新の失敗（`slack.failed` + `method=chat.update`）は**すべて別の値**にする

## 注意点

- **`litellm/` ディレクトリは pip パッケージ `litellm` をシャドーイングする。**
  リポジトリルートから `import litellm` すると、このディレクトリが名前空間パッケージとして解決される。
- **`slack/` ディレクトリは slack_sdk 同梱の非推奨 `slack` パッケージをシャドーイングする。**
  別ディレクトリに置いたスクリプトから `from slack import ...` すると SDK 側が解決され ImportError になる（実際に踏んだ）。
  検証用スクリプトはリポジトリルートに置くか `python -m slack.<module>` で実行する。
- **秘密情報は `.env` のみ。** API キー・Slack トークン・署名シークレットを
  `litellm/config.yaml` や `docker-compose.yml` に直書きしない（`os.environ/` 参照を使う）。
- **破壊的操作を発行してよいのは、使い捨ての kind クラスタに対してだけ**（[D-026](docs/decisions.md)）。
  **実クラスタ・既存クラスタへの `kubectl delete` は絶対に発行しない。** 対象は
  `kind delete cluster` で丸ごと消せるものに限り、権限は ServiceAccount の RBAC で絞る。
- **`assets/` に置く前に必ず秘密情報を除去する。** LiteLLM は `--detailed_debug` で起動しており、
  ログに master key・AWS の識別子・リクエスト本文が含まれうる。`assets/` は gitignore していない。

## フェーズを進めるときの流れ

1. `docs/phases/phaseN-*/design.md` を**着手前に書き切る**。目的・シナリオ・判定基準・観測方法を確定させる。
2. 正常系 → 異常系の順に実装・実行する。異常系まで確認して初めてそのフェーズは完了。
3. 実行結果・生ログ・判定を `result.md` に、エビデンスを `assets/` に残す。
4. **design.md の判定基準は結果が出た後に書き換えない。** 想定と違った場合は result.md の「考察」に書く。
5. 設計判断が変わる内容なら [docs/decisions.md](docs/decisions.md) に追記する。
6. 新たに踏んだ失敗は [docs/lessons.md](docs/lessons.md) に追記する。**CLAUDE.md には足さない。**
   CLAUDE.md に入れてよいのは「今後コードを書くときの挙動を変える制約」だけで、検証の記録ではない。
7. ルート README のロードマップ表の「状態」列を更新する。

**異常系を回す前に [docs/lessons.md](docs/lessons.md) の「検証が静かに正常系になる」を読むこと。
全部合格しているときほど疑う。**
