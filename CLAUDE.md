# CLAUDE.md

このリポジトリで作業する際のガイド。プロジェクトの全体像は [README.md](README.md) を参照。

## このリポジトリは何か

SRE エージェント（LLM）を多層防御で囲い込む **PoC 検証用リポジトリ**。プロダクションコードではない。
「各層が期待どおり異常を検知・遮断するか」を実証することが目的であり、機能の充実や汎用化は目的ではない。

検証は Phase 1 → 5 の順に進める。**現在地と残タスクは [docs/TODO.md](docs/TODO.md) が正**。
作業を再開するときは、まず TODO.md を読むこと。

## 技術スタック

- Python 3.12 / venv + pip（`requirements.txt`, `requirements-dev.txt`）— uv や Poetry は使わない
- エージェント本体: 素の Python + **OpenAI SDK**（LiteLLM Proxy に OpenAI 互換エンドポイントとして接続）
  - LangGraph 等のエージェントフレームワークは導入しない。層の責務と検証結果を曖昧にしないため。
- 第2層: Pydantic v2
- 第3層: OPA（Rego）。エージェントからは HTTP（`POST /v1/data/...`）で問い合わせる
- エスカレーション: Slack Bolt for Python（Block Kit + Interactivity）
- 依存サービスは Docker Compose で起動

## よく使うコマンド

```bash
# 環境
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# 依存サービス
docker compose up -d              # 正常系（両リージョンとも実 Bedrock）
LITELLM_CONFIG_FILE=config.fault.yaml FAULT_MODE=rate_limit docker compose up -d   # 異常系
docker compose restart litellm    # シナリオを切り替えるたびに必須（Router の cooldown を初期化）
docker compose logs fault-injector  # primary が実際に何回叩かれたかの裏取り
docker compose down

# AWS クレデンシャルの更新（SSO の一時クレデンシャルが失効したとき）
aws sso login --profile <PROFILE>
aws configure export-credentials --profile <PROFILE> --format env-no-export >> .env
docker compose up -d              # コンテナを作り直さないと新しい値が反映されない

# エージェント実行
python -m agents.main --prompt "hello"
python -m agents.main --prompt "hello" --repeat 20 --interval 15   # F-05 フェイルバック観測

# テスト
pytest                            # Python 側 (第2層など)
opa test opa/                     # Rego ポリシーの単体テスト

# 疎通（プロセスの死活確認のみ。推論が通る保証にはならない）
curl -s http://localhost:4000/health
curl -s http://localhost:8181/health
```

## コーディング規約

- 型ヒントは必須。層をまたぐデータ構造は必ず Pydantic モデルとして `agents/schemas.py` に定義する。
- 層の境界を越えるデータは JSON。層の実装は互いに import し合わない（HTTP かスキーマ経由で疎結合を保つ）。
- 例外は握り潰さない。各層の失敗は「検知して Slack へハンドオフする」ことが検証対象そのものなので、
  `except Exception: pass` のようなコードは PoC の目的を破壊する。
- ログは各層の判定結果（フェイルオーバー発火・`ValidationError`・`allow` の値と理由）が
  後から追えるように出力する。検証のエビデンスになる。
- コメント・ドキュメント・Slack メッセージは日本語。コード識別子は英語。

## Phase 1 の前提（完了）

- フェイルオーバー先は **Amazon Bedrock の同一モデル・マルチ地理圏**。
  モデルは `claude-sonnet-4-6` に固定し、応答品質の変化という交絡を排除して可用性だけを検証する。
  - primary: `bedrock/jp.anthropic.claude-sonnet-4-6` @ `ap-northeast-1`（日本国内で推論）
  - fallback: `bedrock/us.anthropic.claude-sonnet-4-6` @ `us-east-2`（US 圏内で推論）
- **Bedrock の Claude は Claude 3 Haiku を除き推論プロファイル必須**。単一リージョンを名指しする構成は取れないため、
  障害単位はリージョンではなく地理圏になる。`global.` プロファイルは US を包含するため fallback 先には使わない。
- 障害注入は **`mocks/fault_injector.py` によるエラー応答**（429 / 500 / timeout）。API キー無効化は使わない。
  異常系は `litellm/config.fault.yaml` を読み込ませることで実現し、**`agents/` は一切変更しない**。
- エージェントには論理モデル名 `sre-agent-model` だけを見せる。リージョンと実モデル ID は LiteLLM 側の関心事。
- **リトライ（同一デプロイへの再試行）とフォールバック（別デプロイへの切り替え）を必ず切り分ける。**
  ベースラインは `num_retries: 0`。これを崩すと「成功したのはリトライのおかげか」が判別できなくなる。

詳細は [design.md](docs/phases/phase1-litellm/design.md)、
結果は [result.md](docs/phases/phase1-litellm/result.md)（2026-09-29 完了・合格）。

### Phase 2 以降にも効く Phase 1 の知見

- **層の内部状態はシナリオ間で持ち越される。** LiteLLM Router の cooldown で実際に結果が汚染された。
  状態を持つコンポーネントを検証するときは、シナリオごとのリセット手順を必ず組み込む。
- **層が自己申告する値を鵜呑みにしない。** `x-litellm-attempted-retries` は実際に 2 回
  リトライしていても 0 を返した。層の外側（障害注入側）からの裏取りと 2 系統で判定する（[D-009](docs/decisions.md)）。
- **ヘルスチェックは可用性の指標にならない。** 推論が全滅していても `unhealthy_count: 0` が返る。
  各層の健全性は実リクエストの成否で測る。
- **エビデンスのファイル名は使い回さない。** Phase 1 でブロッカー時のログを後の成功ログで上書きした。

## 注意点

- **`litellm/` ディレクトリは pip パッケージ `litellm` をシャドーイングする。**
  リポジトリルートから `import litellm` すると、このディレクトリが名前空間パッケージとして解決されてしまう。
  LiteLLM は Docker で Proxy として動かし、エージェント側は OpenAI SDK 経由で叩く前提なので通常は問題にならないが、
  ホスト側で litellm を import する必要が出た場合はこの点を思い出すこと。
- **秘密情報は `.env` のみ。** API キー・Slack トークン・署名シークレットを
  `litellm/config.yaml` や `docker-compose.yml` に直書きしない（`os.environ/` 参照を使う）。
  `.env` と `.env.local` は gitignore 済み。
- **PoC では実際の破壊的操作を実行しない。** `allow: true` になった場合も、
  実行層はログ出力か `--dry-run` に留める。実クラスタへの `kubectl delete` は絶対に発行しない。
- **異常系は「壊して確かめる」のが本体。** API キーの無効化、壊れた JSON の注入、禁止コマンドの投入といった
  故意の failure injection がこのリポジトリの主要な作業であり、バグではない。
- Slack の Interactivity 受信には外部から到達可能な URL が必要（ngrok 等）。ローカル URL では検証できない。

## フェーズを進めるときの流れ

1. `docs/phases/phaseN-*/design.md` を**着手前に書き切る**。目的・シナリオ・判定基準・観測方法を確定させる。
2. 正常系 → 異常系の順に実装・実行する。異常系まで確認して初めてそのフェーズは完了。
3. 実行結果・生ログ・判定を `result.md` に、エビデンスを `assets/` に残す。
4. **design.md の判定基準は結果が出た後に書き換えない。** 想定と違った場合は result.md の「考察」に書く。
5. 設計判断が変わる内容なら `docs/decisions.md` に追記する。
6. ルート README のロードマップ表の「状態」列を更新する。

ドキュメントの構成と運用ルールの詳細は [docs/README.md](docs/README.md)。
