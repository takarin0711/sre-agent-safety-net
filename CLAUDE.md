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
- 第2層 **JSON Validator**: 実装は Pydantic v2。エージェントと同一プロセスのライブラリとして読み込む
  （層は役割名で呼び、実装名では呼ばない。理由は [D-013](docs/decisions.md)）
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
LITELLM_CONFIG_FILE=config.fault.yaml FAULT_MODE=rate_limit docker compose up -d   # 異常系 (第1層)
LITELLM_CONFIG_FILE=config.fault.yaml FAULT_MODE=malformed FAULT_FIXTURE=v01_prose \
  docker compose up -d            # 異常系 (第2層 / R-01)。200 を返し content だけ壊す
docker compose restart litellm    # Router の cooldown を初期化するとき（状態のリセット専用）
docker compose up -d --force-recreate litellm fault-injector   # 設定・env を切り替えるとき（restart では反映されない）
docker compose logs fault-injector  # primary が実際に何回叩かれたかの裏取り
docker compose down

# 異常系 (第3層)。agents/ は変更しない
docker compose stop opa                                          # X-01 接続不能
OPA_POLICY_DIR=./mocks/opa_faults/no_policy       docker compose up -d opa   # X-04 undefined
OPA_POLICY_DIR=./mocks/opa_faults/allow_as_string docker compose up -d opa   # X-05 形の逸脱
docker compose logs opa           # Decision Log。allow がポリシー評価の結果である裏取り

# AWS クレデンシャルの更新（SSO の一時クレデンシャルが失効したとき）
aws sso login --profile <PROFILE>
aws configure export-credentials --profile <PROFILE> --format env-no-export >> .env
docker compose up -d              # コンテナを作り直さないと新しい値が反映されない

# エスカレーション層 (Phase 4)
python -m slack.preview --pattern 3       # 通知パターンを 1 本送る (第1〜3層を起動せずに試せる)
python -m slack.preview --dump --out docs/phases/phase4-slack/assets   # 送らずに JSON を保存
python -m slack.readback                  # 送出したメッセージを読み戻す (届いたことの証拠。D-018)
python -m slack.handler                   # Interactivity の受信 (常駐。ngrok の転送先)
python -m slack.expire --dry-run          # 承認期限の満了検知ジョブ

# エージェント実行
python -m agents.main --prompt "hello"                            # 第2層を通らない (Phase 1 の経路)
python -m agents.main --prompt "hello" --repeat 20 --interval 15   # F-05 フェイルバック観測
python -m agents.main --incident "本番の payment-api の Pod が CrashLoopBackOff"   # 第2層 → 第3層
python -m agents.main --incident "..." --no-policy   # 第2層で止める (Phase 2 の経路の再現)
# --incident の終了コード: 0 allow / 1 第1層・第2層の失敗 / 2 設定不足 / 3 deny / 4 評価不能

# テスト
pytest                            # 第2層 (JSON Validator) の単体検証
python -m tests.dump_validation_errors <out.json>   # errors() の生 JSON をエビデンスとして保存
# Rego ポリシーの単体テスト。opa CLI が無いので Docker で実行する
docker run --rm -v "$PWD/opa:/policies:ro" openpolicyagent/opa:1.21.0 test /policies -v

# 疎通（プロセスの死活確認のみ。推論が通る保証にはならない）
curl -s http://localhost:4000/health
curl -s http://localhost:8181/health
```

## コーディング規約

- 型ヒントは必須。層をまたぐデータ構造は必ず Pydantic モデルとして `agents/schemas.py` に定義する。
  第2層のモデルには `strict=True` と `extra="forbid"` を必ず付ける（既定の lax モードは `"3"` を `3` に
  黙って変換するため、外すと型不一致を検知できなくなる）。
- 層の境界を越えるデータは JSON。**第1層・第3層の実装コードは import しない**（HTTP で疎結合を保つ）。
  第2層は同一プロセスのライブラリなので import して使う。この線引きは [D-010](docs/decisions.md)。
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

## Phase 2 の前提（完了）

- 第2層は役割名 **JSON Validator**、実装は Pydantic v2。**エージェントと同一プロセスのライブラリ**であり、
  HTTP 境界を持たず、独立したコンテナも無い（[D-010](docs/decisions.md) / [D-013](docs/decisions.md)）。
  したがって第2層には「層自体のダウン」という障害モードが存在しない。第1層・第3層との非対称性。
- **全モデルに `strict=True` と `extra="forbid"` を付ける。** 既定の lax モードは `"3"` → `3`、
  `"yes"` → `True` を黙って変換するため、外すと型不一致シナリオ（V-05）が検証を通過し判定基準が崩れる。
- **LLM 出力を修復しない。** コードフェンス剥がしなどの前処理も、`ValidationError` からの
  再プロンプトも入れない（[D-012](docs/decisions.md)）。修復すると最終結果が確率的になり、
  「決定的に検知できる」という検証対象そのものが消える。
- **構造化出力の強制（`response_format` / tool use）はベースラインに入れない**（[D-011](docs/decisions.md)）。
  壊れた入力が来ないと検知能力が測れない。強制との比較は C-01 として別途実施する。
- 異常系は「層の障害」ではなく**入力の異常**の注入。単体 fixture / `fault_injector` 経由 / 実 LLM の
  3 経路に分離し、**非決定的な実 LLM 由来のシナリオは判定基準に使わない**。
- **構造的に正しく意味的に危険な入力は第2層を通過させる**（N-01）。これは欠陥ではなく責務境界の証明で、
  ここで止めると第2層がポリシー判定を始めたことになる（[D-002](docs/decisions.md) 違反）。
- プロンプトには**出力スキーマは書く**（インターフェースの提示）が、**「危険な操作を避けよ」は書かない**
  （書いた時点でその防御が確率的になる。第3層の責務）。

詳細は [design.md](docs/phases/phase2-pydantic/design.md)、
結果は [result.md](docs/phases/phase2-pydantic/result.md)（2026-09-29 完了・合格）。

### Phase 3 以降にも効く Phase 2 の知見

- **障害注入が効いていないと検証は「静かに正常系」になる。** injector が起動せず第1層が実 Bedrock に
  フォールバックし、R-01 の全シナリオが終了コード 0 になった。異常系では「注入が届いたこと」を
  層の外側から必ず確認する（[D-014](docs/decisions.md)）。**全部合格しているときほど疑う。**
- **上位層の正しい挙動が下位層の障害注入を打ち消す。** 第1層のフォールバックは可用性としては正しく、
  同時に「第2層に壊れた入力を届ける」意図を無効化する。層を重ねるほどこの干渉は起きやすい。
- **strict モードは JSON 入力と Python 入力で挙動が違う。** `model_validate(dict)` は strict だと
  enum に実インスタンスを要求し、素の文字列は `is_instance_of` で落ちる。
  第2層の入口は必ず `model_validate_json()` を通す（`json.loads()` を挟まない）。
- **`timeout` を決めるのは出力トークン数の上限であり、それはスキーマの `max_length` で決まる。**
  スキーマを変えたら第1層の `timeout` を見直す（L-01 で 30 → 60 に再決定）。
- **構造化出力の強制は「弱められたスキーマ」しか強制できない。** Bedrock は `minimum` / `maximum` /
  `minLength` / `maxLength` / `pattern` を受け付けない。第2層のスキーマを正本とし、
  強制側はそこから劣化させたコピーとして扱う。

## Phase 3 の前提（完了）

- 第3層は **OPA / Rego**。エージェントとは別プロセスで、`POST /v1/data/sre/agent/decision` に
  問い合わせる。`allow` と `violated_rules` を**1 回の評価で同時に**受け取る（拒否と理由は不可分）。
- **`default allow := false` を書かない。** ポリシー未ロードは `result` ごと返らない（undefined）状態として
  表面化させ、エージェント側が Fail-Closed する。default を置くと「評価して拒否した」と
  「ポリシーが無い」が同じ `allow: false` に潰れ、Slack の通知パターン3 と 4 を区別できなくなる。
- **deny は戻り値、評価不能は例外**（`PolicyUnavailableError`）。遮断されたという結果は同じでも、
  人間に見せるものと次の一手が違う（[D-015](docs/decisions.md)）。
- **OPA の応答を無検証で信じない。** `PolicyDecision` は `strict=True` / `extra="forbid"` で、
  `allow` の型不一致・未知のキー・自己矛盾（違反つき allow / 理由なき deny）はすべて
  評価不能に倒す（[D-017](docs/decisions.md)）。
- **ポリシーの判定に使う語彙を `agents/` に書かない。** `production` や `kubectl_delete_*` による
  条件分岐は `opa/` の中だけに存在する（pytest で強制している）。
  例外は `schemas.py` / `prompts.py` で、そこは語彙の定義と提示であって判定ではない。
- **`input` は `{"action": ..., "trace_id": ...}`。Rego は `trace_id` を参照しない**（[D-016](docs/decisions.md)）。
  判定材料と監査の突合キーを構造的に分ける。Trace ID は OPA の Decision Log に残り、
  Slack のボタン `value` に入るのもこの値だけになる。
- 実行層は dry-run ログのみ。`allow: true` かつ `dry_run: false` でも実 `kubectl` は発行しない。

詳細は [design.md](docs/phases/phase3-opa/design.md)、
結果は [result.md](docs/phases/phase3-opa/result.md)（2026-09-30 完了・合格）。

### Phase 4 以降にも効く Phase 3 の知見

- **`docker compose restart` は設定を切り替えない。** コンテナを作り直さないため `command` の
  `${LITELLM_CONFIG_FILE}` も環境変数も古いままになる。設定を変えるときは
  **`up -d --force-recreate`**、`restart` は状態（Router の cooldown 等）の初期化にのみ使う。
- **注入が効いていることの確認は「注入器が動いているか」では足りない**（[D-018](docs/decisions.md)）。
  リクエストが実際にその経路を通ったこと（`x-litellm-model-id` / 直叩きの生応答）で確認する。
  注入の失敗は偽の合格にも**偽の不合格**にもなる。どちらに転ぶかは運である。
- **Rego の undefined は allow 側に倒れる。** 必須フィールドが欠けた入力はルールが発火せず通る。
  第2層が先に立っているのは安全上の必要条件であり、整理のための並びではない。
  だからといって Rego に構造検証を書かない（[D-002](docs/decisions.md) を逆に崩す）。
- **第3層は input の真正性を検証できない。** environment と namespace を揃えて詐称されると通る。
  第3層が評価しているのは「エージェントの自己申告」である。
- **`timeout` の決め方は層によって難易度が違う。** 第3層は定常 4 ms 台で `timeout: 3.0` の
  52 倍の余裕があり、実測した結果「変える必要がない」が結論になった。規律は同じでも結論は同じとは限らない。

## 注意点

- **`litellm/` ディレクトリは pip パッケージ `litellm` をシャドーイングする。**
  リポジトリルートから `import litellm` すると、このディレクトリが名前空間パッケージとして解決されてしまう。
  LiteLLM は Docker で Proxy として動かし、エージェント側は OpenAI SDK 経由で叩く前提なので通常は問題にならないが、
  ホスト側で litellm を import する必要が出た場合はこの点を思い出すこと。
- **`slack/` ディレクトリは slack_sdk 同梱の非推奨 `slack` パッケージをシャドーイングする。**
  `litellm/` と同じ構造。リポジトリルートから実行する限り本リポジトリの `slack/` が勝つので問題ないが、
  **別ディレクトリに置いたスクリプトから `from slack import ...` すると SDK 側が解決され**、
  `aiohttp` が無いと ImportError になる（実際に踏んだ）。
  検証用のスクリプトはリポジトリルートに置くか、`python -m slack.<module>` で実行する。
  `slack_bolt` / `slack_sdk` 自身はこの互換パッケージを参照しないため、本体の動作には影響しない。
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
