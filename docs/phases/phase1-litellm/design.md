# Phase 1 設計: 第1層 LiteLLM Proxy による地理圏フェイルオーバー

> このドキュメントは**着手前**に確定させるもの。検証後に判定基準を書き換えない。
> 予想と異なる結果になった場合は [result.md](result.md) の「考察」に記録する。

関連: [D-003](../../decisions.md) 同一モデル・マルチ地理圏 / [D-004](../../decisions.md) LiteLLM 層での明示的フェイルオーバー / [D-005](../../decisions.md) 障害注入方式 / [D-008](../../decisions.md) モデルと推論プロファイルの選定

---

## 1. 目的と非目的

### 証明すること

1. **エージェントのコードとプロンプトを一切変更せずに**、primary 側（日本国内）の障害を吸収できる。
2. フェイルオーバーが発生したことを、**事後に観測可能な証拠として示せる**。
   （「エラーが出なかった」だけでは、primary が実は生きていた可能性を排除できない）
3. 全系統が落ちた場合には、**握り潰さず明示的にエラーが上がる**。安全網の限界を明示する。

### 証明しないこと（意図的にスコープ外）

- 応答品質・コスト・レイテンシの最適化
- プロバイダ全断への耐性（D-003 の通り、本 PoC では Bedrock 内に閉じる）
- 地理圏**内**のルーティング挙動（AWS の推論プロファイルに委ねる領域。[D-008](../../decisions.md) 参照）
- Slack への通知（Phase 4 の範囲。ここではエージェント側に例外が上がるところまで）

---

## 2. 構成

### 正常系

```
agents/main.py ──▶ LiteLLM Proxy ──┬──▶ ap-northeast-1 : jp.anthropic.claude-sonnet-4-6  (primary)
 OpenAI SDK          :4000         └──▶ us-east-2      : us.anthropic.claude-sonnet-4-6  (fallback)
 model="sre-agent-model"
```

### 異常系（障害注入時）

```
agents/main.py ──▶ LiteLLM Proxy ──┬──▶ mocks/fault_injector  (primary を差し替え・常に失敗)
 （コード無変更）      :4000         └──▶ us-east-2 : us.anthropic.claude-sonnet-4-6  (fallback / 実 Bedrock)
```

**フェイルオーバーの単位**: 東京エンドポイント上の **`jp.` 推論プロファイル（日本国内で推論）** を primary、
オハイオエンドポイント上の **`us.` 推論プロファイル（US 圏内で推論）** を fallback とする。
両者は**同一モデル `claude-sonnet-4-6`** であり、変わるのは推論が行われる地理圏だけである。

Bedrock の Claude は Claude 3 Haiku を除き推論プロファイル経由でしか呼べないため、
「単一リージョンを名指しする」構成はそもそも取れない（調査結果は [result.md](result.md) を参照）。
そのため障害単位は**リージョンではなく地理圏**になる。地理圏内のどの物理リージョンが応答したかは
AWS 側の領域であり観測できないが、**日本国内から US 圏へ退避したこと**は確定的に観測できるため、
本 Phase の判定基準は成立する。実運用でもそのまま採用できる構成である。

---

## 3. 設計

### 3.1 エージェントには論理モデル名だけを見せる

エージェントが知るのは `sre-agent-model` という論理名のみ。地理圏も実モデル ID も LiteLLM 側の関心事とする。
これが「エージェントは自分が守られていることを知らない」という原則の具体化であり、
判定基準3（コード差分ゼロ）を成立させる前提でもある。

```python
# agents/main.py（抜粋・設計イメージ）
client = OpenAI(base_url=os.environ["OPENAI_BASE_URL"], api_key=os.environ["LITELLM_MASTER_KEY"])
resp = client.chat.completions.create(model="sre-agent-model", messages=[...])
```

### 3.2 LiteLLM 設定（litellm/config.yaml の設計案）

```yaml
model_list:
  - model_name: sre-agent-model            # エージェントから見える論理名
    litellm_params:
      model: bedrock/jp.anthropic.claude-sonnet-4-6   # 日本国内で推論
      aws_region_name: ap-northeast-1
      aws_access_key_id: os.environ/AWS_ACCESS_KEY_ID
      aws_secret_access_key: os.environ/AWS_SECRET_ACCESS_KEY
    model_info:
      id: primary-jp                       # 観測用の識別子

  - model_name: sre-agent-model-fallback   # フォールバック先は別の論理名にする
    litellm_params:
      model: bedrock/us.anthropic.claude-sonnet-4-6   # 同一モデル・US 圏で推論
      aws_region_name: us-east-2
      aws_access_key_id: os.environ/AWS_ACCESS_KEY_ID
      aws_secret_access_key: os.environ/AWS_SECRET_ACCESS_KEY
    model_info:
      id: fallback-us

router_settings:
  fallbacks: [{"sre-agent-model": ["sre-agent-model-fallback"]}]
  num_retries: 0
  timeout: 30
  allowed_fails: 2      # このデプロイを cooldown させるまでの許容失敗回数
  cooldown_time: 30     # cooldown 期間（秒）。F-05 のフェイルバック観測時間に直結する

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
```

**2つのデプロイに別々の `model_name` を与える**点が設計上の選択である。
同一の `model_name` にまとめると LiteLLM はロードバランシング対象として扱い、
通常時からリクエストが両方に分散するため「primary が応答した／fallback に切り替わった」を
一意に観測できなくなる。別名にして `fallbacks` で明示的に紐付けることで、
**通常時は必ず primary、失敗時のみ fallback** という決定的な挙動になる。

### 3.3 リトライとフォールバックの切り分け（この設計の肝）

LiteLLM には「同じデプロイに再試行する」機構と「別のデプロイに切り替える」機構が別々に存在する。
これを区別せずに検証すると、成功した理由が**リトライだったのかフォールバックだったのか判別できない**。

| パラメータ | 役割 | PoC ベースライン | 理由 |
| --- | --- | --- | --- |
| `num_retries` | 同一デプロイへの再試行回数 | **0** | 0 にすることで、成功＝フォールバック発火と一意に断定できる |
| `fallbacks` | 別デプロイへの切り替え | primary → fallback | 検証対象そのもの |
| `timeout` | 1リクエストの上限秒数 | 30 | タイムアウト系（F-03）の発火条件を明確にする |
| `allowed_fails` | デプロイを cooldown 対象にするまでの許容失敗回数 | **2** | F-01〜F-03 が単発の障害注入である想定に対し、確実に cooldown へ入れるための仮置き値。実測して妥当性を見直す |
| `cooldown_time` | 失敗したデプロイを除外する時間（秒） | **30** | F-05（フェイルバック）で「どれだけ待てば primary に戻るか」を観測する基準値として仮置き |

ベースラインは `num_retries: 0` で組み、**F-04 で意図的に `num_retries: 2` に変更**して
「リトライ → それでも失敗 → フォールバック」の順序が期待どおりかを別途確認する。
実運用の推奨値は F-04 の結果を踏まえて result.md で提言する。

### 3.4 障害注入コンポーネント

任意のステータスコードを返す最小の HTTP サーバを用意し、異常系では primary をこれに差し替える。

- 配置: `mocks/fault_injector.py`（ルートに `mocks/` を追加。承認済み: [D-006](../../decisions.md)）
- 挙動: 環境変数 `FAULT_MODE` で応答を切り替える
  - `rate_limit` → 429
  - `server_error` → 500
  - `timeout` → `timeout` 設定を超える時間スリープしてから応答
- OpenAI 互換の `/v1/chat/completions` を受ける

**この方式の限界（明記しておく）**: 異常系では primary エントリを Bedrock ではなく
OpenAI 互換エンドポイントとして定義することになるため、厳密には「Bedrock クライアントが返すエラー」を
再現しているわけではない。ただし当該シナリオで primary の役割は「制御された形で失敗すること」だけであり、
検証したい対象（LiteLLM のフォールバック判断とエージェントへの透過性）は成立する。
Bedrock 固有のエラー（スロットリング等）の扱いを確認する必要が出た場合は、別途追試する。

### 3.5 設定の切り替え方法

`litellm/config.yaml`（正常系）と `litellm/config.fault.yaml`（異常系）を用意し、
docker compose の環境変数で読み込む設定ファイルを切り替える。
**エージェント側は一切変更しない**ことが判定基準3の根拠になる。

---

## 4. 検証シナリオ

| ID | 分類 | 注入する障害 | 期待挙動 | 主な確認点 |
| --- | --- | --- | --- | --- |
| P-01 | 正常系 | なし | primary（`jp.` / 東京）が応答 | 200 が返る。応答したデプロイが primary |
| F-01 | 異常系 | primary が 429 | fallback が応答 | クライアントに例外が出ない。応答したデプロイが fallback |
| F-02 | 異常系 | primary が 500 | fallback が応答 | 同上 |
| F-03 | 異常系 | primary が timeout | fallback が応答 | 同上。`timeout` 超過で切り替わる |
| F-04 | 切り分け | primary が 500 / `num_retries: 2` | primary に3回試行後に fallback | ログ上でリトライ回数とフォールバックの順序が確認できる |
| F-05 | 復旧 | primary を障害→正常に戻す | cooldown 明けに primary へ戻る | フェイルバックが自動で起きる。戻るまでの時間 |
| N-01 | 限界 | primary / fallback 双方が 500 | エージェントに例外が伝播 | 握り潰されない。例外種別とメッセージが原因を示している |

F-04 と F-05 は `router_settings` を変更するため、**ベースライン設定での F-01〜F-03 を先に完了させてから**実施する。

---

## 5. 判定基準

以下を**すべて**満たしたとき Phase 1 合格とする。

1. **F-01 / F-02 / F-03 のいずれにおいても、エージェント側で例外が発生しない**（HTTP 200 を受け取る）。
2. **上記3件で応答したのが fallback デプロイ（`us.` / オハイオ）であることを、ログまたはレスポンスの証拠で示せる。**
   「エラーが出なかった」だけでは不合格とする。
3. **P-01 から N-01 までを通じて `agents/` 配下のコード差分がゼロ**である（`git diff` で確認）。
4. **N-01 で明示的な例外がエージェントまで伝播する。**
   例外が握り潰されて空応答や無限リトライになった場合は不合格。
5. **フェイルオーバーによる追加レイテンシが許容範囲内**である。
   合格ラインは**仮置きで 5 秒**とし、P-01 の実測値を基準に result.md で確定・見直す。

---

## 6. 観測方法（エビデンスの取り方）

判定基準2を満たすため、どちらのデプロイが応答したかを特定する手段を確保する。以下を実機で確認し、
**確実に取得できる手段を最低1つ**エビデンスとして採用する。

| 手段 | 内容 | 状態 |
| --- | --- | --- |
| LiteLLM Proxy のログ | `docker compose logs litellm` にフォールバック発火と使用デプロイが出るか | **確認済み。出るが `--detailed_debug` で 1 リクエスト数百行。補助的にとどめる** |
| レスポンスヘッダ | LiteLLM が実行デプロイを示すヘッダを返すか（ヘッダ名は未確認） | **採用。`x-litellm-model-id`（`primary-jp` / `fallback-us`）と `x-litellm-attempted-fallbacks`。ただし `x-litellm-attempted-retries` は当てにならない** |
| レスポンスの `model` フィールド | 解決後の論理名が入るか | **確認済み。論理名 `sre-agent-model` が入るだけでデプロイ特定には使えない** |
| 消去法 | 異常系では primary は必ず失敗するため、成功応答は fallback 由来と断定できる | 確実（ただし P-01 と F-05 には使えない） |
| 障害注入側のアクセスログ | primary が実際に何回叩かれ、どのステータスを返したか | **採用（着手時に想定していなかった手段）。リトライ回数の確認とシナリオ汚染の検知に必須** |
| `GET /health` | デプロイの健全性 | **使えない。推論が全滅していても `unhealthy_count: 0` を返した** |

> 実測値と採用理由は [result.md「観測手段の確定」](result.md) を参照。
> 着手前は「最低1つ」で足りる想定だったが、実際には**ヘッダと注入側ログの2系統が必要**だった（[D-009](../../decisions.md)）。

エージェント側でも、各リクエストについて以下を構造化ログとして出力する。

- リクエスト ID、開始・終了時刻、所要時間（判定基準5の実測に使う）
- 例外の有無と種別（判定基準1・4に使う）
- レスポンスから取得できた実行デプロイの識別子

取得したログ・レスポンスの生データは `assets/` に置き、result.md から参照する。

---

## 7. 未決事項・確認事項

着手時に確認し、判明した内容は result.md に記録する。設計判断が変わる場合は decisions.md を更新する。

1. ~~**両リージョンで同一モデル ID が有効化されているか。**~~ → 調査済み。結果と設計への影響は
   [result.md「モデルアクセス調査」](result.md) を参照。Claude 3 Haiku 以外は推論プロファイル必須であり、
   Sonnet 5 は東京に `apac.` プロファイルが無いことが判明した。フェイルオーバーの単位を再検討中。
2. ~~**応答したデプロイの特定方法**（第6章の表）。~~ → 確定。`x-litellm-model-id` を採用。詳細は上表と [D-009](../../decisions.md)。
3. ~~**`mocks/` ディレクトリをルートに追加してよいか。**~~ → 承認済み（[D-006](../../decisions.md)）。
4. ~~**`cooldown_time` の既定値**と、F-05 で待つべき時間。~~ → `allowed_fails: 2` / `cooldown_time: 30`（秒）で仮置き。
   **実測の結果、同一設定で cooldown が発動する / しないの両方を観測し、発動条件を外形から確定できなかった。**
   F-05 では cooldown に入らなかったため待ち時間は発生していない。[result.md 考察3](result.md)。
5. ~~**追加レイテンシの合格ライン**（仮置き 5 秒の妥当性）。~~ → 確定。
   **「`timeout` 設定値 + baseline 以内」**とした。仮置きの 5 秒は、フォールバック機構の
   オーバーヘッド（実測 37ms）と primary の死を検知するまでの時間（`timeout` 設定値）を
   混同した数字であり、かつ baseline 最大が 5.39 秒あるため構造的に達成不可能だった。
   [result.md「判定基準5 の閾値の確定」](result.md)。
6. ~~**AWS 認証方式**~~ → 環境変数のアクセスキーで仮置き（承認済み）。
   `AWS_SESSION_TOKEN` も compose から渡せるようにしてあるため、SSO の一時クレデンシャルにも対応可能。
7. **fallback 側の課金・レート上限**。異常系を繰り返すと fallback にリクエストが集中する。
   → 本 PoC の呼び出し量では上限に到達せず**未検証のまま**。コストは `x-litellm-response-cost` で観測できた（1 リクエスト約 $0.0005）。
8. ~~**LiteLLM が返すヘッダ名**。~~ → 確定。[result.md「観測手段の確定」](result.md) に実測値を記載。

---

## 8. 実施手順

**全シナリオ実施済み（2026-09-29 完了）。結果は [result.md](result.md)。**

実装:

- [x] `litellm/config.yaml` / `litellm/config.fault.yaml`
- [x] `docker-compose.yml`（正常系・異常系を環境変数で切り替え）
- [x] `mocks/fault_injector.py`（429 / 500 / timeout / success）
- [x] `agents/main.py`（論理モデル名のみ使用。構造化ログとヘッダ観測を実装）

実行（すべて完了）:

1. ~~未決事項1（モデル可用性）の確認~~ → 完了。`claude-sonnet-4-6` を `jp.` / `us.` プロファイルで使う（[D-008](../../decisions.md)）。
2. ~~**P-01（正常系）**を通す。同時に第6章の観測手段（応答デプロイの特定方法）を確定させる。~~ → 完了。
3. ~~**F-01 / F-02 / F-03** をベースライン設定で実行し、エビデンスを `assets/` に保存する。~~ → 完了。
4. ~~`router_settings` を変更して **F-04 / F-05** を実行する。~~ → 完了。
5. ~~**N-01** を実行し、安全網の限界を記録する。~~ → 完了。

> **実行手順上の追記**: シナリオを切り替えるたびに `docker compose restart litellm` が必要だった。
> 着手前の設計では想定していなかったが、これを省くと LiteLLM Router の cooldown 状態が持ち越され、
> 「primary を一度も叩かずに fallback が応答する」無効なエビデンスが得られてしまう（[D-009](../../decisions.md)）。
6. result.md に結果・考察・実運用への提言を書き、ルート README のロードマップ表の「状態」を更新する。
