# Phase 1 検証結果: 第1層 LiteLLM Proxy によるリージョンフェイルオーバー

> 実行後に記入する。設計は [design.md](design.md) を参照（判定基準は書き換えない）。

**状態**: **完了（2026-09-29）**。P-01 / F-01〜F-05 / N-01 をすべて実施。

## 実施環境

| 項目 | 値 |
| --- | --- |
| 実施日 | 2026-09-29 |
| モデル ID | `jp.anthropic.claude-sonnet-4-6` / `us.anthropic.claude-sonnet-4-6`（両方ともバージョンサフィックスなしが正） |
| primary リージョン | ap-northeast-1（東京エンドポイント、日本国内で推論） |
| fallback リージョン | us-east-2（オハイオエンドポイント、US 圏内で推論） |
| LiteLLM バージョン | 1.103.0（`ghcr.io/berriai/litellm:main-stable`, digest `sha256:bd089afd...`） |
| Python | 3.12.14 (venv) |
| AWS アカウント | <redacted:aws-account-id> / SSO プロファイル `administrator-<redacted:aws-account-id>` |

## 確認事項の結果

design.md 第7章の未決事項に対する回答を記録する。

| # | 確認事項 | 結果 |
| --- | --- | --- |
| 1 | 両リージョンでの同一モデル ID の可用性 | 下記「モデルアクセス調査（2026-09-04）」を参照 |
| 2 | 実行リージョンの特定方法 | **`x-litellm-model-id` レスポンスヘッダ**を採用（`primary-jp` / `fallback-us`）。`x-litellm-model-name` に実モデル ID も返る。`GET /health` は使えない（下記「実行ブロッカー」参照） |
| 3 | `mocks/` 追加の可否 | 承認済み（D-006）。`fault_injector` コンテナは正常に起動することを確認 |
| 4 | `cooldown_time` の既定値 | 明示指定（`allowed_fails: 2` / `cooldown_time: 30`）。F-05 の結果を参照 |
| 5 | 追加レイテンシの合格ライン | **仮置き 5 秒では F-03 が不合格になる。** P-01 ベースライン 1.17〜5.39 秒（n=5, 平均 2.53 秒）に対し、F-01/F-02 はほぼ差がない一方、F-03（timeout）は約 32 秒。下記「考察」参照 |
| 6 | AWS 認証方式 | **SSO の一時クレデンシャルを `.env` に書き出す方式**で確定。`aws configure export-credentials --format env-no-export >> .env`。数時間で失効し、LiteLLM コンテナは起動時に環境変数を焼き込むため、更新後は `docker compose up -d` でコンテナ再作成が必要 |
| 7 | fallback 側のレート上限 | 本 PoC の呼び出し量では到達せず未検証。`x-litellm-response-cost` でコストは観測可能（1 リクエスト約 $0.0005） |
| 8 | LiteLLM が返すヘッダ名 | 下記「観測手段の確定」を参照 |

## モデルアクセス調査（2026-09-04）

design.md 実施手順1 の結果。**設計の前提に影響する内容が判明したため、着手前に方針を再検討する。**

### Claude 系モデルの呼び出し方式（ap-northeast-1）

`list-foundation-models` の `inferenceTypesSupported` を確認した結果:

| モデル | 呼び出し方式 |
| --- | --- |
| `anthropic.claude-3-haiku-20240307-v1:0` | **ON_DEMAND**（基盤モデル ID を直接呼べる） |
| 上記以外の Claude 系すべて（sonnet-5 / opus-5 / haiku-4-5 / sonnet-4-5 等） | **INFERENCE_PROFILE のみ** |

`list-foundation-models` に出ることと直接呼び出せることは別であり、
ON_DEMAND なのは Claude 3 Haiku のみだった。

### Sonnet 5 の推論プロファイル

| リージョン | 利用可能なプロファイル |
| --- | --- |
| ap-northeast-1 | `global.anthropic.claude-sonnet-5` のみ |
| us-east-2 | `us.anthropic.claude-sonnet-5` / `global.anthropic.claude-sonnet-5` |

**東京に `apac.` プロファイルが存在しない**ことが判明した。

### 設計への影響

1. 当初設計の「リージョン単位のフェイルオーバー」は、ON_DEMAND モデル（Claude 3 Haiku）以外では成立しない。
2. `global.` プロファイルは AWS 側が既に全球ルーティングを行うため、
   `global.` → `us.` というフォールバックは US が global の部分集合となり、
   現実には発生しない状況を検証することになる。
3. これは [D-004](../../decisions.md) の「要調査」項目に対する部分的な回答でもある。
   AWS 側が広範なフェイルオーバーを提供している以上、第1層の価値は
   「可用性の確保」よりも「観測可能性・プロバイダ非依存性」に寄る。

### 対応方針（確定）

`claude-sonnet-4-6` を採用し、primary = `jp.anthropic.claude-sonnet-4-6`（東京エンドポイント）、
fallback = `us.anthropic.claude-sonnet-4-6`（オハイオエンドポイント）とする。
`jp.` と `us.` は包含関係がないため、`global.` を使った場合の入れ子問題を回避できる。
根拠は [D-008](../../decisions.md)、[D-003](../../decisions.md) と [D-004](../../decisions.md) も更新済み。

なお副産物として、Bedrock 上に OpenAI・xAI のモデルが提供されていることが判明した。
別ベンダーへのフォールバックが Bedrock 内で完結するため、実運用化の際の選択肢として考察に含める。

## 実行ブロッカー: Anthropic use case details form 未提出（2026-09-29）

環境構築を完了し P-01 を実行したところ、**両デプロイとも同一の Bedrock エラーで失敗**した。

```
litellm.NotFoundError: BedrockException - {"message":"Model use case details have not been
submitted for this account. Fill out the Anthropic use case details form before using the
model. If you have already filled out the form, try again in 15 minutes."}
```

> **エビデンスについて**: このブロッカー時の生ログは、解消後に同名のファイルへ
> P-01 成功時のログを書き出したため**上書きして失われている**。
> 以下に引用したエラーメッセージが残存する記録のすべてである（検証手順上の失敗）。

### 原因

AWS アカウント <redacted:aws-account-id> が Bedrock の Anthropic モデル利用に必要な
**use case details form を一度も提出していない**。推論プロファイル自体は両リージョンで `ACTIVE`
であり、モデル ID にも誤りはない。アカウント側の許可が別途必要という
[TODO.md の「実行前に確認しておくとよいこと」](../../TODO.md)の懸念が的中した形。

```
$ aws bedrock get-use-case-for-model-access --region ap-northeast-1
ResourceNotFoundException: You have not filled out the request form.
```

### 解消方法

AWS コンソールの Bedrock → Model access → Anthropic の use case details フォームを提出する。
提出は**アカウント単位**であり、リージョンごとの作業ではない。反映に最大 15 分かかる。
CLI では `aws bedrock put-use-case-for-model-access --form-data <blob>` に相当するが、
会社名・業種・用途といった申告内容を含むためコンソールから提出するのが確実。

### 副産物として得られた観測（重要）

このブロッカー自体は Phase 1 のシナリオではないが、**設計時の想定を覆す事実が 2 つ得られた**。

1. **`/health` は当てにならない。** LiteLLM の `GET /health` は両デプロイを
   `healthy_endpoints` として返したにもかかわらず、実際の推論呼び出しは即座に失敗した。

   ```json
   {"healthy_endpoints":[{"aws_region_name":"ap-northeast-1","model":"jp.anthropic.claude-sonnet-4-6","model_id":"primary-jp"},
                         {"aws_region_name":"us-east-2","model":"us.anthropic.claude-sonnet-4-6","model_id":"fallback-us"}],
    "unhealthy_count":0}
   ```

   design.md 第6章で観測手段の候補に挙げていたが、**判定基準2 の根拠には使えない**。
   一方でこのレスポンスは `model_id`（`primary-jp` / `fallback-us`）と `aws_region_name` を
   返しており、`model_info.id` が LiteLLM 内部で識別子として機能していることは確認できた。

2. **N-01（両系障害）に相当する挙動を先取りで観測できた。** primary・fallback とも同一原因で
   落ちたため、意図せず N-01 と同じ条件が成立した。結果は判定基準4 の期待どおりで、
   例外は握り潰されずクライアントまで伝播し、`agents/main.py` が
   `request.error` として `NotFoundError` を構造化ログに記録している。
   LiteLLM 側のログにも fallback を試行した事実が残った。

   ```
   LiteLLM Router:DEBUG: fallback_event_handlers.py:151 - Triggered cooldown for fallback deployment fallback-us
   LiteLLM Router:ERROR: router.py:7378 - Error occurred while trying to do fallbacks - ...
   ```

   **ただしこれを N-01 の正式な結果としては採用しない。** design.md が想定する N-01 は
   「primary を障害注入で落とし、fallback も落とす」構成であり、今回は両系が
   同一のアカウント設定起因で落ちている。フォーム提出後に改めて実施する。

---

## 観測手段の確定（design.md 第6章の表を埋める）

P-01 で取得したレスポンスヘッダの実測値。

| 手段 | 結果 | 判定 |
| --- | --- | --- |
| `x-litellm-model-id` | `primary-jp` / `fallback-us`（`model_info.id` がそのまま返る） | **採用**。判定基準2 の根拠はこれ |
| `x-litellm-model-name` | `bedrock/jp.anthropic.claude-sonnet-4-6` 等、実モデル ID | 採用（補強） |
| `x-litellm-attempted-fallbacks` | 0 / 1 | 採用。フォールバック発火の有無を直接示す |
| `x-litellm-attempted-retries` | **常に 0。F-04 で実際に 2 回リトライしても 0 のまま** | **使えない**（下記 F-04 の考察） |
| レスポンスの `model` フィールド | 論理名 `sre-agent-model`（解決後の実デプロイ名ではない） | デプロイ特定には使えない |
| `GET /health` | 呼び出しが全滅する状態でも `unhealthy_count: 0` を返した | **使えない** |
| LiteLLM のログ | `--detailed_debug` では 1 リクエストあたり数百行（P-01 の 5 リクエストで 324 KB: [`p01-litellm.log`](assets/p01-litellm.log)）。`model_info` は含まれるが可読性が低い。クレデンシャルは `AW****47` の形にマスクされる | 補助的 |
| `fault-injector` のアクセスログ | primary が実際に何回叩かれたかが分かる**唯一の手段** | **必須**（F-04 で判明） |

観測に使えた副次的な情報:

- `x-litellm-response-cost` — リクエスト単位のコスト（1 リクエスト約 $0.0005）
- `x-litellm-response-duration-ms` / `x-litellm-overhead-duration-ms` — LiteLLM 自身のオーバーヘッドは約 50ms
- `x-litellm-version` — 1.103.0

### P-01 のベースラインレイテンシ

同一プロンプトを 5 回（[`assets/p01-baseline-x5.log`](assets/p01-baseline-x5.log)）。

| n | min | max | avg |
| --- | --- | --- | --- |
| 5 | 1.166 s | 5.390 s | 2.532 s |

**ベースライン自体のばらつきが 4.2 秒ある**点が、判定基準5（追加レイテンシ 5 秒）の評価に効く。

---

## シナリオ別の結果

| ID | 結果 | 所要時間 | エビデンス | 備考 |
| --- | --- | --- | --- | --- |
| P-01 | **合格** | 1.17〜5.39 s (n=5, avg 2.53) | [`p01-agent.log`](assets/p01-agent.log) / [`p01-baseline-x5.log`](assets/p01-baseline-x5.log) | `model_id=primary-jp`, `fallbacks=0`。東京が応答 |
| F-01 | **合格** | 1.11〜1.92 s (n=3) | [`f01-rate-limit-agent.log`](assets/f01-rate-limit-agent.log) / [`f01-rate-limit-injector.log`](assets/f01-rate-limit-injector.log) | injector に 429 が 3 回。全リクエスト `model_id=fallback-us`, `fallbacks=1`, `retries=0` |
| F-02 | **合格** | 1.12〜1.91 s (n=3) | [`f02-server-error-agent.log`](assets/f02-server-error-agent.log) / [`f02-server-error-injector.log`](assets/f02-server-error-injector.log) | injector に 500 が 3 回。同上 |
| F-03 | **合格** | 31.2 / 31.96 s (n=2) | [`f03-timeout-agent.log`](assets/f03-timeout-agent.log) / [`f03-timeout-injector.log`](assets/f03-timeout-injector.log) | 例外は出ず fallback が応答。ただし**無応答型のため `timeout: 30` を待ち切ってからの切り替え**。内訳は 30 s（検知）+ 約 1.3 s（fallback の実応答） |
| F-04 | **合格（ただし観測に注意）** | 5.93 / 6.25 s (n=2) | [`f04-retries-agent.log`](assets/f04-retries-agent.log) / [`f04-retries-injector.log`](assets/f04-retries-injector.log) | injector が**1 リクエストにつき 3 回**叩かれた（初回+リトライ2回）後に fallback。順序は設計どおり。ただしヘッダは `retries=0` を返す |
| F-05 | **合格** | 復旧から約 16 秒（= ポーリング 1 周期）で primary に復帰 | [`f05-failback-agent.log`](assets/f05-failback-agent.log) / [`f05-failback-injector.log`](assets/f05-failback-injector.log) | 20 回中 1〜7 が `fallback-us`、8〜20 が `primary-fault-injector`。**cooldown 待ちは発生しなかった** |
| N-01 | **合格** | 2.25 s / 31.22 s (n=2) | [`n01-both-fail-agent.log`](assets/n01-both-fail-agent.log) / [`n01-both-fail-injector.log`](assets/n01-both-fail-injector.log) | `InternalServerError` が伝播。原因（`injected fault: server_error`）がメッセージに含まれる。2 回目が 31 秒かかった点は下記考察 |

### F-05 の詳細（フェイルバック）

`config.fault.yaml` + `FAULT_MODE=server_error` で開始し、実行途中に injector を
`FAULT_MODE=success` で作り直して primary を「復旧」させた。

| 時刻 (UTC) | 出来事 |
| --- | --- |
| 05:46:24 | 実行開始（`--repeat 20 --interval 15`）。1〜7 回目は `fallback-us` が応答 |
| 05:47:54 頃 | injector を `FAULT_MODE=success` で作り直し（primary 復旧） |
| 05:48:02 頃 | 7 回目。まだ `fallback-us`（`attempted-fallbacks=1`） |
| 05:48:18 | **8 回目で `primary-fault-injector` に復帰**（`attempted-fallbacks=0`） |
| 〜05:51:19 | 20 回目まで primary が応答し続けた |

**復旧から最初のリクエストで戻った。**待機や手動介入は不要だった。

ただし注意点が 2 つある。

1. **cooldown が一度も発動しなかった。** 1〜7 回目のすべてで `attempted-fallbacks=1` が返っており、
   これは「primary を試して失敗し、fallback に切り替えた」ことを意味する。つまり
   `allowed_fails: 2` / `cooldown_time: 30` を設定していても、**15 秒間隔の連続失敗では
   primary は cooldown リストに入らず、毎回叩かれ続けた**。
   一方で 3 秒間隔で連続失敗させた直後は cooldown が効き、primary が一切叩かれない状態も観測している
   （下記「シナリオ間の状態汚染」）。**cooldown の発動条件は設定値から素直に予測できない。**
2. **この F-05 は primary が実 Bedrock ではなくモックである。** 復帰後の応答時間 0.05 秒は
   injector の値であり、実 Bedrock のフェイルバック時間を測ったものではない。

### N-01 の詳細（安全網の限界）

`config.fault.yaml` の fallback も injector に向けて両系を落とした。

```
InternalServerError: Error code: 500 - litellm.InternalServerError: OpenAIException -
injected fault: server_error. Received Model Group=sre-agent-model
Available Model Group Fallbacks=['sre-agent-model-fallback']
Error doing the fallback: ... injected fault: server_error
```

判定基準4 の要求（握り潰されない / 例外種別とメッセージが原因を示す）を満たす。
`agents/main.py` が `request.error` として構造化ログに記録し、終了コード 1 を返した。

**想定外だった点**: 2 回目のリクエストが **31.2 秒**かかった。1 回目は 2.2 秒で失敗している。
injector のログでは 1 回目は 05:56:27〜29 に 6 POST、2 回目は 05:57:02〜03 に 4 POST が届いており、
その間の約 33 秒はリクエストが一切届いていない。1 回目で両デプロイが cooldown に入り、
**「全デプロイが cooldown 中」の状態でクライアントが `timeout: 30` を待たされた**とみられる。

実運用上の含意は大きい。**両系障害時、2 回目以降のリクエストは即座に失敗せず 30 秒ブロックする。**
エスカレーション層（Phase 4）のタイムアウト設計は、この待ち時間を織り込む必要がある。

---

## 判定

| # | 判定基準 | 合否 | 根拠 |
| --- | --- | --- | --- |
| 1 | F-01〜F-03 でクライアントに例外が出ない | **合格** | F-01 3/3・F-02 3/3・F-03 2/2 がすべて HTTP 200。エージェント側に例外ゼロ |
| 2 | fallback が応答したことをエビデンスで示せる | **合格** | `x-litellm-model-id: fallback-us` を全件で取得。加えて injector のアクセスログで primary が実際に 429/500 を返したことを裏取り済み |
| 3 | `agents/` のコード差分がゼロ | **合格** | 全シナリオ通じて `git status --porcelain agents/` が空。障害注入は設定ファイルの切り替えのみで実現 |
| 4 | N-01 で明示的な例外が伝播する | **合格** | `InternalServerError` が伝播し、メッセージが原因（`injected fault: server_error`）とフォールバック試行の経緯を含む |
| 5 | 追加レイテンシが許容範囲内 | **合格**（閾値を下記のとおり確定） | 全シナリオで `timeout` 設定値 + baseline 以内。F-01/F-02 は追加ゼロ、F-03 は 31.2〜31.96 秒（`timeout: 30` + baseline 1.3 秒） |

### 判定基準5 の閾値の確定

design.md は「合格ラインは**仮置きで 5 秒**とし、P-01 の実測値を基準に result.md で確定・見直す」と
規定しているため、ここで確定させる。

#### 確定した合格ライン

> **フェイルオーバーによる追加レイテンシが `router_settings.timeout` の設定値 + baseline 以内であること。**
> かつ **`timeout` が正常時レイテンシの裾より上に設定されていること。**

| ID | 実測 | `timeout` + baseline | 判定 |
| --- | --- | --- | --- |
| F-01 (429) | 1.11〜1.92 s | —（primary が即エラーを返すため待ちなし） | 合格 |
| F-02 (500) | 1.12〜1.91 s | 同上 | 合格 |
| F-03 (timeout) | 31.2 / 31.96 s | 30 s + 約 1.3 s | 合格 |

#### 仮置きの 5 秒が誤りだった理由

**「フォールバック機構のオーバーヘッド」と「primary の死を検知するまでの時間」を混同していた。**

- 前者は実測 **37ms**（`x-litellm-overhead-duration-ms`）。無視できる。
- 後者は障害の種類で決まる。エラー応答型（429/500）は primary が即座にエラーを返すのでゼロ。
  無応答型（timeout）は**無応答であるがゆえに `timeout` を待ち切るまで生死を判断できない**。
  F-03 の 31 秒の内訳は「30 秒（検知）+ 1.3 秒（fallback の実応答）」であり、
  29 秒はフェイルオーバーのコストではなく **`timeout` の設定値そのもの**である。

さらに、**5 秒という線は構造的に達成不可能だった。**追加レイテンシを 5 秒以内に収めるには
`timeout` を 5 秒未満にする必要があるが、P-01 の baseline は**最大 5.39 秒**（下表）。
`timeout: 5` にすると健全な primary の応答がタイムアウト扱いになり、フォールバックが暴発する。

| P-01 baseline (n=5) | 経過時間 | LiteLLM オーバーヘッド | 出力トークン |
| --- | --- | --- | --- |
| 1 回目 | 5.390 s | 36.0 ms | 28 |
| 2 回目 | 1.446 s | 37.7 ms | 28 |
| 3 回目 | 3.388 s | 38.2 ms | 28 |
| 4 回目 | 1.272 s | 38.0 ms | 29 |
| 5 回目 | 1.166 s | 38.4 ms | 28 |

**5 回とも出力は同一の 28 トークン**（応答文も同一）で、LiteLLM のオーバーヘッドも一定。
つまり 1.17〜5.39 秒のばらつきは応答長でも LiteLLM でもなく、**Bedrock の呼び出しそのものに由来する。**
LLM 呼び出しがこの程度ばらつくのは正常な性質であり、`timeout` はこの裾を見て決める必要がある。

#### `timeout` 設定のトレードオフ

| | |
| --- | --- |
| **短くしたい** | 無応答型障害の検知が速くなる = フェイルオーバーが速い |
| **長くしたい** | 短すぎると健全な primary を誤って殺し、フォールバックが暴発する |

`timeout` は正常時レイテンシの裾（p99 等）より上に置かねばならず、**無応答型障害の検知時間には
「正常時の最悪値」という構造的な下限がある。**設定では消せない。

#### この数値は他のワークロードに転用できない

計測に使ったのは「こんにちは」に 28 トークンで答えるだけの呼び出しである。
Phase 2 以降の SRE エージェントは**構造化 JSON を数百〜数千トークン出力する**ため、
正常時レイテンシは桁で変わる。`timeout: 30` が Phase 2 では短すぎる可能性が高い。
**`timeout` はワークロードごとに実測して決める値であり、本 PoC の数字をそのまま使ってはならない。**

**総合判定**: **合格**（判定基準 1〜5 すべて）。

## 考察

### 1. 観測手段は 1 つでは足りなかった（design.md 第6章の想定との最大の差）

design.md は「確実に取得できる手段を**最低1つ**エビデンスとして採用する」としていたが、
**実際には 2 系統が必要だった。**

`x-litellm-model-id` は「どちらのデプロイが応答したか」を確実に示す。しかし F-04 で、
`x-litellm-attempted-retries` が**実際には 2 回リトライしているのに 0 を返す**ことが判明した。
injector のアクセスログを見て初めて、1 リクエストにつき primary が 3 回叩かれていたと分かった。

| F-04 の観測 | 値 | 実際 |
| --- | --- | --- |
| `x-litellm-attempted-retries` | 0 | **3 回 POST が届いている**（初回 + リトライ 2 回） |
| `x-litellm-attempted-fallbacks` | 1 | 正しい |

ヘッダのみを信じていれば「リトライは起きていない」と誤った結論を出していた。
design.md 3.3 が「この設計の肝」としたリトライとフォールバックの切り分けは、
**LiteLLM が返す情報だけでは成立しない。**`num_retries` の効果を確認するには、
primary 側が何回叩かれたかを primary 側で数える必要がある。
`mocks/fault_injector.py` は障害注入装置であると同時に**観測装置**だった。

なお `x-litellm-attempted-retries` は「応答に成功したデプロイでのリトライ回数」を
返している可能性がある（fallback-us でのリトライは 0 回）。いずれにせよ
primary 側のリトライ回数は取れないため、切り分けの根拠には使えない。

### 2. ヘルスチェックは可用性の指標にならない

use case form 未提出でエージェントの呼び出しが全滅していた状態で、LiteLLM の `GET /health` は
両デプロイを `healthy_endpoints` として返し `unhealthy_count: 0` だった。
**「ヘルスチェックが通ること」と「推論が通ること」は別物**であり、
第1層の可用性監視をヘルスチェックに依存させてはならない。実リクエストの成否を指標にする必要がある。

### 3. cooldown の挙動が設定値から予測できない

`allowed_fails: 2` / `cooldown_time: 30` という同一設定で、以下の 2 つを観測した。

- **F-05**: 15 秒間隔で 7 回連続失敗しても primary は cooldown に入らず、毎回叩かれ続けた
- **シナリオ間の状態汚染**（下記）: 3 秒間隔で 3 回失敗させた直後は primary が一切叩かれなくなった

`allowed_fails` を「N 回失敗したら cooldown」と読むと前者が説明できない。
実装は時間窓と失敗率を併用しているとみられるが、**設定値の意味を外形から確定できなかった**。
実運用では cooldown の発動を設定値から期待せず、実際の切り替わりをメトリクスで観測すべきである。

これは同時に、**F-05 でフェイルバックが即座に起きた理由**でもある。
cooldown に入っていなかったため「cooldown 明けを待つ」という段階自体が存在しなかった。
cooldown が効いていれば最大 30 秒の復帰遅延が乗ったはずで、
**今回の「約 16 秒で復帰」という数字は cooldown が発動しない条件下での値**である点に注意が必要。

### 4. シナリオ間の状態汚染（検証手順上の失敗と修正）

当初、F-01 の直後に fault-injector だけを作り直して F-02 を実行したところ、
**injector に POST が 1 件も届かないまま fallback が応答した。**
LiteLLM Router の cooldown 状態がプロセス内メモリに残っており、
primary が cooldown 中だったため試行すらされていなかった。

この結果は「F-02 で 500 を注入してフォールバックした」ようにヘッダ上は見えるが、
**実際には 500 が一度も発生していない**ため、エビデンスとして無効である。
以降は各シナリオの前に `docker compose restart litellm` を挟んで Router の状態を初期化し、
injector 側のアクセスログで「primary が実際に叩かれ、期待したステータスを返した」ことを
毎回確認する手順に変更した。F-01 と F-02 は取り直している。

**複数の障害シナリオを連続実行する検証では、層の内部状態のリセットを手順に組み込まなければ
結果が汚染される。**Phase 3（OPA）以降でも同じ注意が要る。

### 5. 第1層の価値は可用性よりも観測可能性にあるという仮説の補強

D-004 で挙げていた論点が、実測で裏付けられた。

- フォールバックによる**可用性の改善は確かに機能した**（F-01〜F-03 で例外ゼロ）。
  ただしこれは AWS の `global.` 推論プロファイルでも相当部分が得られる。
- 一方、`x-litellm-model-id` / `x-litellm-attempted-fallbacks` / `x-litellm-response-cost` /
  `x-litellm-overhead-duration-ms` が**リクエスト単位で取れることは、Bedrock 単体では得られない**。
  「今どちらの地理圏で推論しているか」をアプリケーション層が知れることが、第1層の実質的な価値だった。
- LiteLLM 自身のオーバーヘッドは約 50ms であり、この観測可能性のコストは無視できる。

### 6. 障害注入が「エージェントのコード差分ゼロ」で成立した

判定基準3 は全シナリオで満たされた。config ファイルの差し替えだけで 429 / 500 / timeout /
両系障害を作り分けられ、`agents/` には一切触れていない。
これは第2層以降でも維持すべき性質である（層の境界が疎結合であることの実証）。

## 実運用に向けた提言

### 設定値

| 項目 | 推奨 | 理由 |
| --- | --- | --- |
| `num_retries` | **0〜1** | エラー応答型の障害では fallback が即座に効くためリトライの価値が薄い。F-04 では 2 回のリトライが約 4 秒の遅延を生んだだけだった。同一デプロイの一過性エラーを拾いたい場合のみ 1 |
| `timeout` | **ワークロードごとに正常時レイテンシを実測し、その裾（p99 等）の少し上に置く** | 無応答型障害の追加レイテンシは `timeout` の値そのもの。短すぎると健全な primary を誤って殺す（本 PoC の baseline は最大 5.39 秒あり、`timeout: 5` なら暴発していた）。**本 PoC の 30 秒は 28 トークン応答での値であり、構造化 JSON を出す Phase 2 以降には転用できない** |
| `cooldown_time` / `allowed_fails` | **設定はするが挙動を当てにしない** | 同一設定で発動する / しないの両方を観測した。切り替わりの実態はメトリクスで見る |

### 監視すべき指標

- `x-litellm-model-id` の分布 — どの地理圏で推論しているかの実態。**第1層導入の最大の戦果**
- `x-litellm-attempted-fallbacks` の発生率 — primary の健全性の代理指標
- `x-litellm-response-cost` — フォールバック時にコストが変動する場合の検知
- **実リクエストの成否**。`GET /health` は使わない（考察2）

### 本 PoC で分からなかったこと

- **実 Bedrock 障害時の挙動。** 異常系の primary はモックであり、Bedrock 固有のエラー
  （スロットリング、推論プロファイル側の障害）を再現したものではない（design.md 3.4 の既知の限界）
- **cooldown の正確な発動条件。** 外形からは確定できなかった
- **fallback 側のレート上限。** 本 PoC の呼び出し量では到達しなかった
- **実 Bedrock でのフェイルバック所要時間。** F-05 の primary はモックのため未測定
- **正常時レイテンシの信頼できる分布。** baseline は n=5 であり、p99 を語れる標本数ではない。
  `timeout` を実運用値に決めるには改めて計測が要る
- **地理圏によるレイテンシ分布の差。** us-east-2 側の応答（F-01/F-02/F-05 合計 13 回）は
  1.11〜1.97 秒に収まり外れ値が無かったのに対し、ap-northeast-1 側（n=6）には 5.39 / 3.39 秒が出た。
  **標本数が足りず結論は出せない**が、`timeout` を地理圏ごとに変える必要があるかもしれない
- **`bedrock/converse/` プレフィックスの要否。** `bedrock/` のままで全シナリオ成功したため、
  必要になるケースは確認していない
