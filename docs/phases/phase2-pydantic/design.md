# Phase 2 設計: 第2層 JSON Validator による構造保証

> このドキュメントは**着手前**に確定させるもの。検証後に判定基準を書き換えない。
> 予想と異なる結果になった場合は [result.md](result.md) の「考察」に記録する。
>
> 層は役割名 **JSON Validator** で呼ぶ。実装は Pydantic v2（[D-013](../../decisions.md)）。
> 以下で「Pydantic」と書いてあるのは、実装固有の挙動を指す箇所だけである。

関連: [D-002](../../decisions.md#d-002-ポリシー判定を-pydantic-ではなく-opa-に置く) 構造検証と権限判定の分離 /
[D-010](../../decisions.md) 第2層はインプロセスのライブラリ /
[D-011](../../decisions.md) 構造化出力の強制を第2層の代替としない /
[D-012](../../decisions.md) 自動修復リトライを入れない /
[D-013](../../decisions.md) 層の呼称 /
[D-009](../../decisions.md) エビデンスの2系統化（第2層では適用されない理由を第6章に記す）

---

## 1. 目的と非目的

### 証明すること

1. **LLM 出力の構造破壊を、確率に頼らず決定的に検知できる。**
   同じ壊れた入力に対して必ず同じ判定が出ること（第1層の cooldown のような内部状態を持たないこと）が
   第2層の存在価値そのものである。
2. **検知が「落ちた」で終わらず、どのフィールドが何の理由で不正かを特定できる。**
   設計原則3（拒否は必ず理由とセットで返す）を第2層で満たす。理由が取れなければ
   Phase 4 のエスカレーションで人間が判断できない。
3. **例外が握り潰されず、エスカレーションイベントとして構造化される。**
   第2層の失敗は単一の例外型（`ValidationError`）に正規化され、ハンドオフ経路が 1 本になること。
4. **第2層が内容の安全性を判定していないこと。**
   構造的に正しく意味的に危険な入力は**第2層を通過する**。これは欠陥ではなく責務境界の証明であり、
   第3層（OPA）が必要である根拠になる（[D-002](../../decisions.md#d-002-ポリシー判定を-pydantic-ではなく-opa-に置く)）。

### 証明しないこと（意図的にスコープ外）

- **LLM が構造を壊す頻度。** 確率的な量であり、PoC の呼び出し量では有意な推定ができない。
  第2層の前提は「一定確率で壊れる」であって、その確率の大小は設計判断に影響しない。
  実 LLM を使うシナリオ（R-02 / C-01）は考察用の材料であり、**判定基準には使わない**。
- **壊れた出力の修復。** 再プロンプトによる自動リトライは入れない（[D-012](../../decisions.md)）。
- **内容の安全性判定。** `kubectl delete` の可否は第3層の責務（Phase 3）。
- **Slack への通知。** エスカレーションイベントを**生成して構造化ログに出すところまで**。
  Block Kit への載せ方は Phase 4。
- 実行層。第3層が無い段階で実行はしない。Phase 2 の最終出力は「Phase 3 に渡せる型付きオブジェクト」。

---

## 2. 構成

### 2.1 第2層はインプロセスのライブラリである（第1層・第3層との非対称性）

```
                        ┌─ プロセス境界（HTTP）
  agents/main.py ──────▶│  LiteLLM Proxy :4000 ──▶ Bedrock          第1層
                        └─
        │ content: str | None   ← 信頼できる構造を一切仮定しない
        ▼
  ┌────────────────────────────────────────────┐
  │ agents/validation.py  +  agents/schemas.py │        第2層
  │  ProposedAction.model_validate_json(...)   │  ← 同一プロセス・同一 GIL
  └────────────────────────────────────────────┘  ← HTTP 境界を持たない
        │ ProposedAction（型付き）
        ▼                 ┌─ プロセス境界（HTTP）
  （Phase 3 で）──────────▶│  OPA :8181 POST /v1/data/...            第3層
                           └─
```

第2層は**エージェントと同一プロセスで動くライブラリ**であり、独立したサービスではない。
`docker-compose.yml` に第2層のコンテナは存在しない。この点は設計上の帰結が大きいので明記しておく。

| | 第1層 (LiteLLM) | 第2層 (JSON Validator) | 第3層 (OPA) |
| --- | --- | --- | --- |
| 実装 | LiteLLM Proxy | Pydantic v2 | OPA / Rego |
| 実行形態 | 別プロセス / 別コンテナ | **同一プロセス・ライブラリ** | 別プロセス / 別コンテナ |
| 呼び出し | HTTP (OpenAI 互換) | 関数呼び出し | HTTP (`POST /v1/data/...`) |
| 「層自体のダウン」 | ありうる | **原理的に無い**（エージェントが動いていれば動く） | ありうる（単一障害点） |
| 内部状態 | 持つ（Router の cooldown） | **持たない**（純関数） | 持たない（ポリシーは静的） |
| 判定の再現性 | 設定値から予測できなかった（Phase 1 考察3） | **入力が同じなら必ず同じ** | 同じ |

「層の実装は互いに import しない」（[architecture.md](../../architecture.md) 設計原則2）は、
**第1層・第3層の実装コードを import しない**という意味であり、第2層をライブラリとして
import することを禁じるものではない。第2層は import される以外の呼ばれ方を持たない。
この解釈を [D-010](../../decisions.md) として確定させる。

### 2.2 検証経路（正常系）

```
--incident "..."  ──▶ 第1層 ──▶ content(str) ──▶ 第2層 ──▶ ProposedAction ──▶ 構造化ログ
                                                                            （Phase 3 の input になる JSON）
```

### 2.3 検証経路（異常系）

異常系の入力は 3 つの独立した経路から与える。**どの経路で検証したかを混ぜない**。

| 経路 | 入力源 | 決定的か | 用途 |
| --- | --- | --- | --- |
| **単体** (V 系) | `tests/fixtures/` の壊れた文字列 | 決定的 | **判定基準の主軸。** pytest で自動化 |
| **経路** (R-01) | `mocks/fault_injector.py` が 200 + 壊れた content を返す | 決定的 | エージェント実行経路での検知とハンドオフ |
| **実 LLM** (R-02 / C-01) | 敵対的プロンプトで構造破壊を誘発 | **非決定的** | 考察用。判定基準に使わない |

経路検証（R-01）の構成は Phase 1 の異常系をそのまま流用する。**第1層からは正常**（HTTP 200）に見え、
壊れているのは `content` の中身だけである点が Phase 1 との違いであり、層の責務境界の実証になる。

```
agents/main.py ──▶ LiteLLM Proxy ──▶ mocks/fault_injector  (200 を返し、content に壊れた JSON を詰める)
 （検証ロジックは無変更）  :4000        FAULT_MODE=malformed / FAULT_FIXTURE=<fixture 名>
```

第1層は何も検知しない（200 なのでフォールバックもしない）。検知するのは第2層だけである。

---

## 3. 設計

### 3.1 スキーマ（`agents/schemas.py`）

第2層 → 第3層のインターフェース。[architecture.md](../../architecture.md) の約束
「最低限、対象環境・操作の種別・対象リソースを含む」を具体化する。

```python
# 設計イメージ（実装時に細部は変わりうるが、下記の設計判断は変えない）

class Environment(StrEnum):
    PRODUCTION = "production"
    STAGING = "staging"
    DEVELOPMENT = "development"

class Operation(StrEnum):
    # 参照系
    GET = "kubectl_get"
    DESCRIBE = "kubectl_describe"
    LOGS = "kubectl_logs"
    # 変更系
    ROLLOUT_RESTART = "kubectl_rollout_restart"
    SCALE = "kubectl_scale"
    # 破壊系
    DELETE_POD = "kubectl_delete_pod"
    DELETE_PVC = "kubectl_delete_pvc"

class TargetResource(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["pod", "deployment", "statefulset", "pvc", "node"]
    name: str = Field(min_length=1, max_length=253, pattern=r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
    namespace: str = Field(min_length=1, max_length=63)

class ProposedAction(BaseModel):
    """エージェントが実行しようとする 1 アクション。"""
    model_config = ConfigDict(extra="forbid", strict=True)
    action_id: str
    environment: Environment
    operation: Operation
    target: TargetResource
    replicas: int | None = Field(default=None, ge=0, le=100)
    reason: str = Field(min_length=1, max_length=1000)   # 人間が読む根拠（設計原則3）
    dry_run: bool = True

    @model_validator(mode="after")
    def _replicas_required_for_scale(self) -> "ProposedAction":
        # 「scale なのに目標値が無い」は構造の不完全性であり、構造検証の責務
        ...
```

エスカレーションイベントも同じファイルに置く（[architecture.md](../../architecture.md) 失敗時の共通フロー 2 を満たす形）。

```python
class EscalationEvent(BaseModel):
    layer: Literal["layer1_litellm", "layer2_json_validator", "layer3_opa"]
    reason: str                       # 人間が読んで判断できる文言
    errors: list[dict] = []           # ValidationError.errors() をそのまま格納
    raw_output: str | None = None     # LLM の生出力（何が来たのかを人間が見る）
    original_request: str             # 元のインシデント記述
    occurred_at: datetime
```

### 3.2 `strict=True` を明示する（これを外すと判定基準1が崩れる）

Pydantic v2 の既定は lax モードであり、**`"3"` を `3` に、`"true"` を `True` に黙って強制変換する**。
このままでは「型が違う JSON」（V-05）が検証を通過してしまい、第2層が何を保証しているのかが曖昧になる。

したがって全モデルで `strict=True` を明示する。LLM が型を間違えたなら、それは
**修復すべき入力ではなく検知すべき異常**として扱う。これは第2層を「LLM の出力を直す層」ではなく
「仕様と違う出力を止める層」と位置づける判断であり、[D-012](../../decisions.md) と同じ思想である。

> **着手時に確認する点**: Pydantic の strict モードは Python オブジェクト経由の検証と
> JSON 文字列経由の検証（`model_validate_json`）で挙動が完全に一致するとは限らない。
> V-05 で実測し、一致しない部分があれば result.md に記録する（未決事項 2）。

### 3.3 `extra="forbid"` を採る

スキーマに無いキーが来た場合、黙って捨てず `ValidationError` にする。

理由: LLM が勝手に足したフィールドを無視すると、**第3層が見ないフィールドに指示が入る余地**が残る。
`{"operation": "kubectl_get", "then_also": "delete the pvc"}` を「余分なキーは無視」で通すと、
OPA には参照系として渡り、しかし生出力を実行系に渡す実装が将来入れば抜け道になる。
仕様に無い入力は仕様違反として扱うのが構造保証の責務である。

実運用では前方互換性（新フィールド追加時に旧バージョンが落ちる）と衝突するため、
その扱いは result.md の提言で述べる。PoC では監査性を優先する。

### 3.4 前処理をしない（コードフェンスを剥がさない）

Markdown のコードフェンス（三連バッククォート）で囲まれた出力や、前後に説明文が付いた出力から、
正規表現で JSON 部分だけを抜き出す前処理は**入れない**。

理由: それは「LLM の出力を修復する」行為であり、第2層の責務ではない。
剥がして通せば V-01（自然言語混入）が検知されなくなり、第2層が何を保証しているのかが説明できなくなる。
検証の目的は「壊れた出力が確実に止まること」の実証であって、通過率を上げることではない。

実運用でフェンス剥がしまでを許容するかは設計判断の余地があるため、提言に書く。

### 3.5 検証の入口は 1 つの関数に閉じる（`agents/validation.py`）

```python
def validate_action(content: str | None, *, original_request: str) -> ProposedAction:
    """第2層の入口。失敗時は EscalationEvent を構造化ログに出してから例外を再送出する。"""
```

設計上の要点:

- **`model_validate_json()` を使う**（`json.loads()` → `model_validate()` の 2 段にしない）。
  Pydantic v2 は JSON の構文エラーも `ValidationError` に正規化するため、
  `json.JSONDecodeError` と `ValidationError` を呼び出し側で使い分ける必要がなくなる。
  **第2層の失敗が単一の例外型になり、ハンドオフ経路が 1 本になる**（証明すること 3）。
- **`content is None` / 空文字列だけは Pydantic の手前で検査する。**
  第1層が 200 を返しつつ `content` が `null` になるケース（V-08）は「文字列ですらない」ため
  Pydantic に渡せない。ここだけ層の内部で明示的に検知し、**同じエスカレーションイベントに落とす**。
- **例外は握り潰さない。** ログを出して `raise`。`except Exception: pass` はこの PoC の検証対象を破壊する。

### 3.6 プロンプトに書くこと / 書かないこと

設計原則1（エージェントは自分が守られていることを知らない）との線引きを明示する。

| プロンプトに | 可否 | 理由 |
| --- | --- | --- |
| 出力スキーマの提示（キー名・型・enum 値） | **書く** | これは防御ではなく**インターフェースの提示**。書かなければ会話が成立しない |
| 「JSON のみを出力せよ」 | **書く** | 同上 |
| 「危険な操作は避けよ」「本番では delete するな」 | **書かない** | 書いた時点でその防御は確率的なものに退化する。第3層の責務 |
| 「フェイルオーバーに注意せよ」 | **書かない** | 第1層の責務（Phase 1 と同じ） |

### 3.7 CLI（`agents/main.py`）

Phase 1 の `--prompt` は**そのまま残す**。Phase 1 のエビデンスの再現性を壊さないため。
第2層を通す経路は `--incident` として追加する。

```bash
python -m agents.main --prompt "hello"                      # Phase 1 の経路（第2層を通らない）
python -m agents.main --incident "本番の payment-api の Pod が CrashLoopBackOff"   # Phase 2 の経路
```

`--incident` 経路の終了コード: 検証成功 `0` / `ValidationError` `1`（判定基準3 で使う）。

### 3.8 障害注入コンポーネントの拡張

`mocks/fault_injector.py` に `FAULT_MODE=malformed` を追加する。
**HTTP 200 と OpenAI 互換のレスポンス形を保ったまま**、`choices[0].message.content` に
`FAULT_FIXTURE` で指定した fixture の中身を詰めて返す。

- fixture は `tests/fixtures/` を単体検証と**共用する**。同じ入力で単体と経路の両方を検証できる。
- 既存の 429 / 500 / timeout モードには手を入れない（Phase 1 の再現性を保つ）。

**この方式の限界**: 実 LLM が実際に出す壊れ方と、こちらで書いた fixture の壊れ方は一致しない。
fixture は「起こりうる壊れ方の代表」であって網羅ではない。実 LLM 由来の壊れ方は R-02 で補うが、
そちらは非決定的なので判定基準には使わない。この非対称性は自覚しておく。

---

## 4. 検証シナリオ

### 4.1 正常系

| ID | 入力 | 期待挙動 | 主な確認点 |
| --- | --- | --- | --- |
| P-01 | 実 LLM（`--incident`）| 検証を通過し `ProposedAction` になる | 第1層 → 第2層 → 型付きオブジェクトが繋がる。JSON 化して OPA の `input` にできる |
| P-02 | 正常な JSON fixture（単体）| 全フィールドが正しく型付けされる | enum 全値、任意フィールド省略、`replicas` 境界値（0 / 100）が通る |

### 4.2 異常系（単体・決定的・pytest で自動化）

| ID | 壊し方 | 入力例 |
| --- | --- | --- |
| V-01 | 自然言語混入 | 「承知しました。以下のアクションを提案します。」+ コードフェンスで囲んだ JSON + 「ご確認ください。」 |
| V-02 | JSON 構文破壊 | クォート閉じ漏れ / 末尾カンマ / シングルクォート |
| V-03 | 途中で切れた出力 | `{"action_id": "a1", "environment": "produ`（`max_tokens` 到達の再現）|
| V-04 | 必須キー欠落 | `environment` が無い / `operation: kubectl_scale` なのに `replicas` が無い |
| V-05 | 型不一致 | `replicas: "3"` / `dry_run: "yes"` / `target: "payment-api"`（オブジェクトでなく文字列）|
| V-06 | enum 外の値 | `environment: "prod"` / `operation: "kubectl_drain"`（語彙に無い操作）|
| V-07 | 余剰キー | スキーマ外の `then_also` が混入（3.3 の設計判断の検証）|
| V-08 | 空 / null | `content: null` / `""` / 空白のみ（3.5 の Pydantic 手前の検査）|

### 4.3 経路・実 LLM・限界・計測

| ID | 分類 | 内容 | 期待挙動 | 判定基準に使うか |
| --- | --- | --- | --- | --- |
| R-01 | 経路 | `fault_injector` が 200 + V-01〜V-08 の fixture を返す | エージェント経路で `ValidationError`、エスカレーションイベント生成、終了コード 1。**第1層は何も検知しない** | **使う** |
| R-02 | 実 LLM | 敵対的プロンプト（「まず考えを述べてから JSON を出せ」等）で構造破壊を誘発 | 壊れたら第2層が検知する | 使わない（非決定的） |
| N-01 | 限界 | 構造的に正しく意味的に危険な入力<br>`{"environment": "production", "operation": "kubectl_delete_pvc", ...}` | **第2層を通過する。** 第3層に渡る | **使う**（責務境界の証明） |
| D-01 | 決定性 | V-01〜V-08 を 10 回連続実行 | 毎回同一の判定・同一のエラー内容 | **使う** |
| L-01 | 計測 | 構造化 JSON 出力時の正常系レイテンシを実測（n ≥ 20）| — | 使わない（計測） |
| C-01 | 比較 | `response_format` / tool use による構造化出力の強制を入れた場合との比較 | — | 使わない（考察） |

**L-01 の位置づけ**: Phase 1 から持ち越した論点。`timeout: 30` は「こんにちは」に 28 トークンで答える
呼び出しでの値であり、数百〜数千トークンの構造化 JSON では正常時レイテンシが桁で変わる可能性がある。
`timeout` が正常時レイテンシの裾より下にあると、**健全な primary を誤って殺して不要なフォールバックを起こす**。
L-01 の実測値をもとに `litellm/config.yaml` の `timeout` を再決定し、根拠を result.md に残す。
これは第1層の設定変更だが、Phase 1 の判定を覆すものではない（当時の値は当時のワークロードに対して妥当）。

**C-01 の位置づけ**: 「Bedrock/LiteLLM 側で構造化出力を強制すれば第2層は不要ではないか」という
当然出る問いに答えるための材料。強制は V-01〜V-03（構文レベル）の発生率を下げうるが、
V-06（enum 外）・V-04（`scale` に `replicas` が無い等の相互依存）・N-01（意味の危険性）は
**原理的に強制では守れない**。そして「確率を下げる対策」と「決定的に遮断する仕組み」は
別物であるという本 PoC の主張そのものに関わる。詳細は result.md の考察に書く。

### 4.4 実施順序

V 系（単体）→ D-01（決定性）→ P-01（実 LLM 正常系）→ L-01（レイテンシ実測と `timeout` 再決定）
→ R-01（経路）→ N-01（限界）→ R-02 / C-01（考察材料）。

単体を先に完成させる。実 LLM を先に触ると、落ちた原因が第2層なのかプロンプトなのか
第1層なのか切り分けられなくなる。

---

## 5. 判定基準

以下を**すべて**満たしたとき Phase 2 合格とする。

1. **V-01〜V-08 のすべてで `ValidationError`（または V-08 の Pydantic 手前の検査）が発生し、
   どのフィールドが何の理由で不正かが `errors()` から特定できる。**
   検証を通過してしまうケースが 1 つでもあれば不合格。「落ちた」だけで理由が取れない場合も不合格。
2. **D-01 で、同一入力に対する判定が 10 回連続で完全に一致する。**
   第1層（cooldown で結果が変わった）との対比が第2層の価値であり、ここが揺らぐなら層の位置づけが崩れる。
3. **R-01 で、エージェント実行経路でも検知され、握り潰されず、
   `EscalationEvent` が構造化ログに出力され、終了コードが非ゼロになる。**
   イベントには「どの層で」「何が」「元のリクエストは何だったか」が含まれること。
   加えて、**第1層がこの異常を検知していないこと**（HTTP 200・フォールバック無し）を示せること。
4. **N-01 で、構造的に正しく意味的に危険な入力が第2層を通過する。**
   ここで止まってしまう場合、第2層がポリシー判定を始めている（[D-002](../../decisions.md#d-002-ポリシー判定を-pydantic-ではなく-opa-に置く) 違反）ため不合格。
5. **P-01 で実 LLM の出力が検証を通過し、`model_dump(mode="json")` が
   そのまま OPA の `input` として使える JSON になる。**
   Phase 3 の前提が満たされることの確認。

**判定基準に使わないもの**: R-02 / C-01（非決定的）、L-01（計測であり合否ではない）。

---

## 6. 観測方法（エビデンスの取り方）

| 手段 | 内容 | 用途 |
| --- | --- | --- |
| `pytest -v` の出力 | V 系・D-01 の結果一覧 | 判定基準1・2。`assets/` に保存 |
| `ValidationError.errors()` の生 JSON | どのフィールド・どのバリデータで落ちたか | 判定基準1。**「落ちた」ではなく「なぜ落ちたか」のエビデンス** |
| エージェントの構造化ログ | `event=validation.ok` / `validation.error` + `EscalationEvent` | 判定基準3・5 |
| プロセスの終了コード | `echo $?` | 判定基準3（握り潰されていないこと） |
| `x-litellm-model-id` / `x-litellm-attempted-fallbacks` | R-01 で第1層が正常応答していること | 判定基準3 の「第1層は検知しない」 |
| `docker compose logs fault-injector` | どの fixture を何回返したか | R-01 の裏取り |

### 第2層に [D-009](../../decisions.md)（2 系統のエビデンス）が要らない理由

第1層では「層の自己申告（ヘッダ）」が実態と食い違い、層の外側からの裏取りが必須だった。
第2層では**入力と出力の両方を我々が完全に握っており**、判定は純関数で、層の内部状態も無い。
`errors()` は層の自己申告だが、同じ入力を再投入すれば必ず再現するため独立検証が成立する。

ただし **R-01 は例外**で、第1層を経由する以上「第1層が本当に何もしていないか」は
第1層のヘッダと injector のログで裏取りする。上表の下 2 行がそれに当たる。

生データは `assets/` に置き、result.md から参照する。**ファイル名は使い回さない**（Phase 1 の失敗）。

---

## 7. 未決事項・確認事項

着手時に確認し、判明した内容は result.md に記録する。設計判断が変わる場合は decisions.md を更新する。

1. **構造化 JSON 出力時の正常系レイテンシと、`timeout` の再決定値。** → L-01 で実測して確定する。
2. **`strict=True` が `model_validate_json()` でどこまで効くか。** Pydantic は JSON モードで
   一部の型変換を許容する可能性がある。V-05 で実測し、Python オブジェクト経由との差分を記録する。
   差があれば、差のある型（`bool` / `int` / `datetime` 等）について検証方法を設計し直す。
3. **`EscalationEvent` のフィールドが Phase 4 の Block Kit に足りるか。** Phase 2 では
   architecture.md の要求（どの層・何が・元のリクエスト）を満たす最小限に留め、不足は Phase 4 で追加する。
4. **`model_dump(mode="json")` が OPA の `input` にそのまま使えるか。** `datetime` や `StrEnum` の
   JSON 化が Rego 側で扱いやすい形になるかを P-01 で確認する。
5. **実 LLM が `Operation` の enum 値をそのまま出せるか。** `kubectl_delete_pod` のような
   スネークケースの語彙を守れず自然な `kubectl delete pod` を出す可能性がある。
   守れない場合、それは V-06 として正しく検知されるが、正常系（P-01）が安定しないため
   プロンプトの提示方法（3.6）を調整する。**スキーマ側を LLM に合わせて緩めることはしない。**
6. **`Operation` を enum にすることが第2層の責務を超えないか。**
   本設計では「語彙にあるか」を第2層、「その語彙の要素を今この対象に実行してよいか」を第3層とする。
   enum は allow-list（未知の操作は構造検証で落ちる）であり、自由文字列にすると第3層が
   deny-list を持つことになって漏れる。この線引きの妥当性は Phase 3 の設計時に再確認する。

---

## 8. 実施手順

実装:

- [ ] `agents/schemas.py`（`ProposedAction` / `TargetResource` / enum / `EscalationEvent`）
- [ ] `agents/validation.py`（`validate_action()`。第2層の唯一の入口）
- [ ] `agents/main.py` に `--incident` 経路を追加（`--prompt` は変更しない）
- [ ] `tests/fixtures/`（V-01〜V-08 + 正常系。構文が壊れているものは `.txt` にする）
- [ ] `tests/test_layer2_validation.py`（V 系 + D-01 + N-01）
- [ ] `mocks/fault_injector.py` に `FAULT_MODE=malformed` を追加（既存モードは変更しない）

実行:

1. V-01〜V-08 と P-02 を pytest で通す。`assets/` に `pytest -v` の出力を保存。
2. D-01（10 回連続）を実行し、判定の決定性を記録する。
3. P-01 を実 LLM で実行し、未決事項 4・5 を確認する。
4. L-01 でレイテンシを実測（n ≥ 20）し、`timeout` を再決定して根拠を残す。
5. R-01 を `FAULT_MODE=malformed` で実行する。**シナリオ切り替えごとに `docker compose restart litellm`**
   （Phase 1 の知見。第1層の状態汚染を避ける）。
6. N-01 を実行し、第2層を通過することを確認する。
7. R-02 / C-01 を実行し、考察材料を集める。
8. result.md に結果・考察・実運用への提言（`extra` の前方互換性、フェンス剥がしの許容、
   構造化出力強制との併用、修復リトライの是非）を書き、
   ルート README のロードマップ表の「状態」と TODO.md を更新する。
