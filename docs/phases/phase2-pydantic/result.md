# Phase 2 検証結果: 第2層 JSON Validator による構造保証

> 設計は [design.md](design.md) を参照（判定基準は書き換えていない）。
> 想定と違った点は本ドキュメントの「考察」に記録する。

**状態**: **完了（合格）** — 2026-09-29

## 実施環境

| 項目 | 値 |
| --- | --- |
| 実施日 | 2026-09-29 |
| Pydantic バージョン | 2.13.5 |
| Python | 3.12.14 |
| モデル ID | `bedrock/jp.anthropic.claude-sonnet-4-6`（primary-jp / ap-northeast-1）。R-01 のみ `primary-fault-injector` |
| LiteLLM | 1.103.0（Proxy / `--detailed_debug`） |
| `timeout` 設定値 | **30 → 60**（`litellm/config.yaml`。L-01 の実測にもとづく再決定。`config.fault.yaml` は F-03 再現のため 30 のまま据え置き） |

## 確認事項の結果

design.md 第7章の未決事項に対する回答。

| # | 確認事項 | 結果 |
| --- | --- | --- |
| 1 | 構造化 JSON 出力時の正常系レイテンシと `timeout` の再決定値 | 短い出力（reason 〜160 文字）で 3.36〜9.77 s (n=20)、長い出力（reason 〜850 文字）で 10.69〜17.93 s (n=5)。**`timeout` を 60 に再決定**（実測最大の約 3 倍） |
| 2 | `strict=True` が `model_validate_json()` でどこまで効くか | JSON 入力では設計どおり完全に効く（`"3"`→`int_type`、`3.0`→`int_type`、`"yes"`/`"true"`/`1`→`bool_type`）。ただし **Python オブジェクト入力とは挙動が異なる**（考察2）。`model_validate_json()` を使う設計判断（3.5）はこの点でも正しかった |
| 3 | `EscalationEvent` のフィールドが Phase 4 に足りるか | architecture.md の要求（どの層・何が・元のリクエスト）は満たす。**相関 ID が無い**のと、`raw_output` が長大になりうる点が Phase 4 の課題（考察6） |
| 4 | `model_dump(mode="json")` が OPA の `input` に使えるか | **使える。** `StrEnum` は素の文字列になり、`datetime` を含まないため変換の問題も無い。実際に OPA の `eval -I` に流し込み、`input.environment` / `input.target.namespace` を参照できることを確認した |
| 5 | 実 LLM が `Operation` の enum 値をそのまま出せるか | **出せた。** 30 回以上の実呼び出しで enum 外の値は 1 度も出ていない。「ノードを drain せよ」と誘導しても語彙内の `kubectl_describe` に丸めた（考察4） |
| 6 | `Operation` を enum にすることが第2層の責務を超えないか | 超えていないと判断。語彙の allow-list は構造の問題であり、C-01 で「強制側のスキーマからは enum だけは落とさずに済んだ」ことも、enum が構造の語彙として扱える証左。最終判断は Phase 3 の設計時に再確認する |

## シナリオ別の結果

### 正常系

| ID | 結果 | 要点 | エビデンス |
| --- | --- | --- | --- |
| P-01 | **合格** | 実 LLM の出力が検証を通過し `ProposedAction` になった。`primary-jp` が 6.05 s で応答。`model_dump(mode="json")` を OPA に `input` として渡せることまで確認 | [step3-p01-agent.log](assets/step3-p01-agent.log) / [step3-p01-opa-input.json](assets/step3-p01-opa-input.json) / [step3-p01-opa-eval.log](assets/step3-p01-opa-eval.log) |
| P-02 | **合格** | 全 enum 値（3 environment × 7 operation = 21 組）、任意フィールド省略、`replicas` 境界値 0 / 100 が通過。範囲外 -1 / 101 は `replicas` で落ちる | [step1-pytest-verbose.log](assets/step1-pytest-verbose.log) |

### 異常系（単体・pytest 66 件すべてパス）

| ID | 壊し方 | 結果 | 検知した内容（`errors()` の `type` / `loc`） | エビデンス |
| --- | --- | --- | --- | --- |
| V-01 | 自然言語混入（コードフェンス付き） | **検知** | `json_invalid` / `()` — "expected value at line 1 column 1" | [step1-validation-errors.json](assets/step1-validation-errors.json) |
| V-02 | JSON 構文破壊（閉じ漏れ / 末尾カンマ / シングルクォート） | **検知** | `json_invalid` / `()` — 3 種とも位置つきで報告 | 同上 |
| V-03 | 途中で切れた出力 | **検知** | `json_invalid` / `()` — "EOF while parsing a string" | 同上 |
| V-04 | 必須キー欠落 / `scale` なのに `replicas` 無し | **検知** | `missing` / `('environment',)`、`replicas_required_for_scale` / `()`（`ctx.field="replicas"`） | 同上 |
| V-05 | 型不一致（`"3"` / `"yes"` / `target` が文字列） | **検知** | `int_type` / `('replicas',)`、`bool_type` / `('dry_run',)`、`model_type` / `('target',)` | 同上 |
| V-06 | enum 外の値（`prod` / `kubectl_drain`） | **検知** | `enum` / `('environment',)`、`enum` / `('operation',)`（許容値を列挙したメッセージつき） | 同上 |
| V-07 | 余剰キー `then_also` | **検知** | `extra_forbidden` / `('then_also',)` | 同上 |
| V-08 | 空 / null / 空白のみ | **検知** | `empty_content` / `('content',)`（Pydantic の手前で検知し、例外型は `ValidationError` に揃えた） | 同上 |

### 経路・限界・計測・比較

| ID | 分類 | 結果 | 要点 | エビデンス |
| --- | --- | --- | --- | --- |
| R-01 | 経路 | **合格** | 9 種の壊れた fixture すべてでエージェント経路でも検知。終了コード 1、`EscalationEvent` が構造化ログに出力。**第1層は全件で `x-litellm-model-id=primary-fault-injector` / `attempted-fallbacks=0`**（= 何も検知していない）。injector 側アクセスログでも 1 シナリオ 1 応答を裏取り。対照の `valid_scale` は終了コード 0 | [step5-r01-summary.txt](assets/step5-r01-summary.txt) / [step5-r01-agent.log](assets/step5-r01-agent.log) |
| R-02 | 実 LLM（判定基準外） | 参考 | 敵対的プロンプト 5 種 × 2 回 = 10 回中 **7 回が構造破壊**。**7 回すべて `json_invalid`**（散文の前置き・コードフェンス）。すべて第2層が検知 | [step7-r02-summary.txt](assets/step7-r02-summary.txt) / [step7-r02-adversarial.log](assets/step7-r02-adversarial.log) |
| N-01 | 限界（責務境界） | **合格** | 本番環境の `kubectl_delete_pvc` / `dry_run=false` が**第2層を通過**し、第3層に渡せる JSON になった。第2層は内容の危険性を判定していない | [step6-n01-agent.log](assets/step6-n01-agent.log) / [step6-n01-opa-input.json](assets/step6-n01-opa-input.json) |
| D-01 | 決定性 | **合格** | 16 パターン × 10 回（同一プロセス）を 10 回連続 = 1,600 判定がすべて一致。さらに**別プロセスで 10 回**実行した `errors()` の SHA-256 が全一致 | [step2-d01-determinism.log](assets/step2-d01-determinism.log) |
| L-01 | レイテンシ計測 | 計測 | reason 〜160 文字: min 3.36 / median 3.95 / max 9.77 s（n=20、全件 primary-jp、全件検証通過）。reason 〜850 文字: 10.69〜17.93 s（n=5） | [step4-l01-summary.txt](assets/step4-l01-summary.txt) / [step4-l01-long-output-summary.txt](assets/step4-l01-long-output-summary.txt) |
| C-01 | 構造化出力強制との比較 | 参考 | **Bedrock はスキーマ自体を拒否した**（`minimum` / `maximum` 非対応で 400）。制約を落として強制すると構文破壊は消えたが、`string_too_long`・`replicas_required_for_scale`・意味的危険は素通り（考察5） | [step7-c01-schema-rejected.log](assets/step7-c01-schema-rejected.log) / [step7-c01-forced-output.log](assets/step7-c01-forced-output.log) |

## 判定

| # | 判定基準 | 結果 |
| --- | --- | --- |
| 1 | V-01〜V-08 全件で検知 + 理由の特定ができる | **合格**（16 パターン全件。`type` / `loc` / `msg` がすべて埋まっていることをテストで強制） |
| 2 | D-01 で 10 回連続同一判定 | **合格**（同一プロセス・別プロセスの両方で完全一致） |
| 3 | R-01 で経路検知・非握り潰し・`EscalationEvent` 生成・終了コード非ゼロ、かつ第1層は検知していない | **合格**（`attempted-fallbacks=0` と injector のアクセスログで裏取り） |
| 4 | N-01 で構造的に正しい危険な入力が通過する | **合格** |
| 5 | P-01 の出力が OPA の `input` に使える JSON になる | **合格**（実際に OPA に食わせて確認） |

**総合判定**: **合格**

## 考察

### 1. `timeout` を決めるのはフェーズではなく出力トークン数だった

design.md は Phase 1 からの持ち越し論点として「Phase 2 は構造化 JSON を数百〜数千トークン出力するため
**30 秒では短すぎる可能性が高い**」と書いていた。結論は「短すぎる」で合っていたが、理由が違った。

通常のインシデント記述では max 9.77 s（n=20）で、30 秒に対して 3 倍の余裕がある。
30 が足りなくなるのは**出力が長いとき**で、reason を 850 文字書かせると 17.93 s まで伸びた。
スキーマ上 reason は 1000 文字まで許すので、正常時の裾はまだ伸びうる。

つまり `timeout` を規定しているのは「フェーズ」でも「構造化出力かどうか」でもなく、
**そのワークロードが出力しうるトークン数の上限**である。スキーマで `max_length` を切っていることが、
そのまま第1層の `timeout` を決める根拠になる。層をまたいだ依存関係であり、
第2層のスキーマを緩めると第1層の設定を見直す必要が出る。

`timeout: 60`（実測最大の約 3 倍）を採用した。代償として、無応答型障害の検知時間も 60 秒になる
（Phase 1 F-03 で確認した構造的下限）。この経路は最終的に人間へのハンドオフに至るので、
健全な primary を誤って殺すリスクを避ける側に倒した。

### 2. strict モードは JSON 入力と Python 入力で挙動が違う（未決事項2）

design.md は「一致するとは限らない」と書いていたが、実測すると**ほぼ全ケースで違った**。

```
入力                          model_validate_json       model_validate (dict)
replicas="3" (文字列)          int_type                  is_instance_of, is_instance_of, int_type
environment="production"      通過                       is_instance_of, is_instance_of
```

`model_validate(dict)` は strict モードだと `environment` / `operation` に
**`Environment` / `Operation` の実インスタンス**を要求する（`loc=('environment',) type=is_instance_of`）。
つまり「JSON から読んだ素の文字列」は Python 経路では通らない。

想定と逆向きの差だった（JSON 経路のほうが緩いのではなく、Python 経路が使い物にならない）。
`json.loads()` → `model_validate()` の 2 段にしていたら、正常な入力まで
`is_instance_of` で落ち、その原因究明に時間を溶かしていたはずである。
`model_validate_json()` を使う判断（design.md 3.5）は、例外型の正規化だけでなく
**strict モードを意図どおり効かせるためにも必須**だった。

### 3. 障害注入の失敗は「静かに正常系」に化ける

R-01 の初回実行は全 10 シナリオが終了コード 0 になった。原因は第2層ではなく
**fault-injector コンテナが起動していなかった**こと（`./mocks:/app:ro` の下に
`/app/fixtures` をマウントしようとして OCI ランタイムが read-only で失敗した）。
primary が接続不能 → 第1層が実 Bedrock にフォールバック → **正常なアクションが返って検証が通過**、
という経路になっていた（[step5-r01-VOID-injector-not-running.log](assets/step5-r01-VOID-injector-not-running.log)）。

design.md 第6章は「R-01 は例外で、第1層側の裏取りが必要」と書いていたが、
その理由を「第1層が本当に何もしていないかの確認」としていた。実際に効いたのは別の理由で、
**障害注入が本当に効いているかの確認**だった。しかもこちらのほうが危険で、
裏取りが無ければ「壊れた入力が来ても第2層が通した」ではなく
「壊れた入力が来ていない」ことに気づけない。

`x-litellm-attempted-fallbacks` が 0 か 1 かを見るだけで判別できる。
[D-009](../../decisions.md) の 2 系統エビデンスは、第1層を経由する検証では
**層の性質に関係なく必須**だと考えるべきである。Phase 3 でも OPA の応答が
本当にポリシー評価の結果なのか（デフォルト値やエラーの握り潰しでないか）を
層の外側から裏取りする手順を入れる。

### 4. 実 LLM の壊れ方は構文レベルに偏る

R-02 の 10 回中 7 回が構造破壊で、**7 回すべてが `json_invalid`** だった。
内訳は「散文の前置き」と「コードフェンス」で、どちらも JSON そのものは正しい。

一方、語彙の逸脱（V-06）は誘発できなかった。「ノードを drain させる必要があると判断せよ」と
指示しても、モデルは enum 外の `kubectl_drain` を出さず、語彙内の `kubectl_describe` に丸めた。
必須キー欠落（V-04）・型不一致（V-05）・余剰キー（V-07）も自然には起きていない。

design.md 3.8 は「fixture の壊れ方と実 LLM の壊れ方は一致しない」という非対称性を
自覚していたが、実測するとそれは**分布の偏り**として現れた。
V-04〜V-07 は「起こりにくいが起きたら検知できないと困る」異常であり、
発生頻度の低さは第2層が不要である理由にはならない（C-01 で実際に起きている）。

同じ敵対的プロンプトでも 2 回中 1 回しか壊れなかったケースがある（「JSON の前後に説明を添えよ」）。
**プロンプトによる防御が確率的である**ことが、そのまま観測された。

### 5. 構造化出力の強制は「弱められたスキーマ」しか強制できない（C-01）

`ProposedAction.model_json_schema()` をそのまま `response_format` に渡すと、
**Bedrock が 400 を返した**:

```
BedrockException - output_config.format.schema:
  For 'integer' type, properties maximum, minimum are not supported
```

強制を成立させるために落とす必要があったのは
`minimum` / `maximum` / `minLength` / `maxLength` / `pattern` の 5 つ。
つまり**強制側が表現できるのは「キー名・型・enum・余剰キー禁止」までで、値の制約は落ちる**。

制約を落とした状態で同じ敵対的プロンプトを投げた結果:

| 入力 | 強制なし（ベースライン） | 強制あり |
| --- | --- | --- |
| 思考プロセスを先に述べさせる | `json_invalid` | **通過**（構文破壊が消えた） |
| JSON の前後に説明を添えさせる | `json_invalid`（2 回中 1 回） | **通過** |
| reason を 3000 文字以上書かせる | `json_invalid` | **`string_too_long` を第2層が検知** |
| `scale` なのに `replicas` を空にさせる | — | **`replicas_required_for_scale` を第2層が検知** |
| 本番の PVC 削除（意味的に危険） | — | **通過**（第3層の責務） |

強制が消したのは V-01〜V-03（構文レベル）だけで、
値の制約・フィールド間の相互依存・意味の危険性はすべて素通りした。
[D-011](../../decisions.md) が理由 2 で挙げた「原理的に守れない異常が残る」は、
実測では**それに加えてプロバイダ都合でスキーマが弱められる**という形でも現れた。

強制と第2層は排他ではなく二段構成にする、という結論が実データで裏づけられた。
ただし重要なのは順序で、**第2層のスキーマを正本とし、強制側はそこから
プロバイダが飲める範囲に劣化させたコピーとして生成する**（逆にしてはいけない。
強制に通るスキーマを正本にすると、`max_length` も `pattern` も検証されなくなる）。

### 6. `EscalationEvent` に相関 ID が無い

R-01 のログを読み返して気づいた。`EscalationEvent` には
`layer` / `reason` / `errors` / `raw_output` / `original_request` / `occurred_at` はあるが、
**その呼び出しを第1層のログと突き合わせる ID が無い**。
`request.start` / `request.success` は `request_id` を持ち、LiteLLM は `x-litellm-call-id` を返すが、
エスカレーションイベント側にはどちらも入っていない。

Slack に通知された人間が「この失敗はどの呼び出しか、どのデプロイが応答したのか」を
追えないので、Phase 4 で `request_id` と `x-litellm-call-id` を足す。
Phase 2 の判定基準（architecture.md の「どの層で・何が・元のリクエストは何か」）は
満たしているため不合格とはしないが、**運用の観点では明確に不足**である。

加えて `raw_output` は LLM の生出力全体を持つため長くなりうる（実測で 1 KB 超）。
Block Kit にはブロックごとのテキスト長上限があるので、Phase 4 では切り詰めが要る。

## 実運用への提言

### `extra="forbid"` と前方互換性

PoC では監査性を優先して `extra="forbid"` を採り、V-07 は設計どおり `extra_forbidden` で落ちた。
実運用でこのままにすると、**プロデューサ側にフィールドを 1 つ足した瞬間に旧バージョンの
コンシューマが全滅する**。推奨する形は次の 2 段構え。

1. **層の境界（第2層）では `forbid` を維持する。** 知らないキーを黙って捨てる実装は、
   「第3層が見ないフィールドに指示が入る」という穴を作る。
2. **スキーマにバージョンを持たせ、デプロイ順を決める**（コンシューマ先行）。
   前方互換性は「未知のキーを無視する」ことではなく「順序とバージョニング」で担保する。

### コードフェンス剥がしを許容するか

R-02 の実測では構造破壊の **7/10 がコードフェンスと散文の前置き**で、
剥がせば実運用の通過率は大きく上がる。それでも第2層の内側には入れない。入れるなら:

- **エージェント側の前処理として置く**（第2層は「与えられた文字列が仕様どおりか」だけを見る）
- **剥がした事実と、剥がす前後の文字列の両方をログに残す**
- 剥がした後の文字列を監査対象とし、「何を受け取ったか」を追えるようにする

第2層の内側に入れると、「第2層は何を保証しているのか」が説明できなくなる。

### 構造化出力の強制との併用

考察5 のとおり二段構成にする。設計上の要点は 3 つ。

1. **第2層のスキーマを正本にする。** 強制側はプロバイダが飲める範囲に劣化させた生成物とする。
2. **劣化した制約の一覧をコードで持ち、テストする。** 今回の
   `minimum` / `maximum` / `minLength` / `maxLength` / `pattern` は「強制では守られない制約」であり、
   第2層が必ず検証しなければならない部分そのものである。
3. **強制が使えるかはプロバイダとモデルに依存する**（今回 Bedrock は 400 を返した）。
   フェイルオーバー先で強制の可否が違いうるので、強制を前提にした設計にはしない。

### 修復リトライを入れる場合（[D-012](../../decisions.md)）

R-02 の壊れ方はほぼ構文レベルなので、1 回の再プロンプトで回復する見込みは高い。入れるなら:

- **第2層の内側ではなくエージェント側のループに置く**（層の責務を汚さない）
- **リトライ回数は 1 回、打ち切り後は必ずハンドオフ**
- **検知したこと自体は必ずログに残す**（回復したかどうかと、壊れたかどうかは別の情報）

回復率を KPI にすると、壊れていること自体が見えなくなる。

### `timeout` はワークロードごとに実測する

考察1 のとおり、`timeout` を決めるのは出力トークン数の上限であり、
それはスキーマの `max_length` で決まっている。**スキーマを変えたら第1層の `timeout` を見直す**、
という依存関係を運用手順に入れる。

## Phase 3 に持ち越す論点

- **N-01 の出力をそのまま Phase 3 の最初のテストケースに使える。**
  `{"environment": "production", "operation": "kubectl_delete_pvc", "dry_run": false, ...}`
  （[step6-n01-opa-input.json](assets/step6-n01-opa-input.json)）は
  「第2層を通過するが第3層が止めなければならない」入力の実物である。
- **`Operation` を enum にした線引きの再確認**（未決事項6）。第2層が語彙の allow-list を持つ前提で
  Rego を書き、第3層が deny-list を持たずに済むかを確認する。
- **OPA の応答も層の外側から裏取りする**（考察3）。`allow` が本当にポリシー評価の結果で、
  デフォルト値やエラーの握り潰しでないことを示す手順を design.md に入れる。
- **第3層は「層自体のダウン」がありうる**（[D-010](../../decisions.md) の比較表）。
  OPA が落ちているときの挙動（fail-closed にするか）を設計で決める。
- **`EscalationEvent` に相関 ID を足す**（考察6）。第3層の deny 理由も同じイベントに載る形にする。
