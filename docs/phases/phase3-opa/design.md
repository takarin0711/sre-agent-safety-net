# Phase 3 設計: 第3層 OPA による権限判定と Fail-Closed

> このドキュメントは**着手前**に確定させるもの。検証後に判定基準を書き換えない。
> 予想と異なる結果になった場合は [result.md](result.md) の「考察」に記録する。
>
> 第3層は製品名 **OPA** で呼ぶ。第2層だけを役割名で呼ぶ理由は [D-013](../../decisions.md)。
> 本フェーズの成果物は Rego ポリシーそのものであり、実装固有の知見が検証結果の本体になる。

関連: [D-002](../../decisions.md#d-002-ポリシー判定を-pydantic-ではなく-opa-に置く) 構造検証と権限判定の分離（本フェーズはその実証） /
[D-009](../../decisions.md) エビデンスの2系統化 /
[D-010](../../decisions.md) 第2層はインプロセス（第3層との非対称性） /
[D-012](../../decisions.md) 自動修復リトライを入れない /
[D-014](../../decisions.md) 障害注入が効いていることを層の外側から確認する /
本フェーズで確定させる判断: **D-015**（Fail-Closed と単一障害点の扱い）/ **D-016**（Trace ID の持ち方）/ **D-017**（OPA の応答を信じない）

---

## 0. Handoff 仕様書との対応（どこまでを Phase 3 で扱うか）

別途の仕様書「フォールバック & Handoff フロー図」は、第1層〜第3層＋エスカレーション層＋実行層を
**通しで**定義している。Phase 3 はそのうち「図1 の第3層の 3 分岐」と「図3 の入口まで」を担当する。
**どこまでを実装し、どこからを Phase 4 / 5 に渡すかを先に固定する**（ここが曖昧だと、
Slack が無いことを理由に第3層の検証が中途半端に終わる）。

| 仕様書の要素 | Phase 3 で扱うか | 扱い方 / 先送りする理由 |
| --- | --- | --- |
| 図1「第3層 `allow: true`」分岐 | **扱う** | 実行層は dry-run ログのみ（CLAUDE.md の禁止事項。実 `kubectl` は発行しない） |
| 図1「第3層 `allow: false`」分岐 | **扱う** | `violated_rule` つきで拒否し、パターン3 相当の `EscalationEvent` を生成する。**Slack 送出は Phase 4** |
| 図1「第3層 応答なし / Timeout」分岐 | **扱う（本フェーズの主戦場）** | Fail-Closed。パターン4 相当の `EscalationEvent` を生成する |
| 通知パターン3 / 4 の Block Kit ペイロード | 扱わない | Phase 4。Phase 3 は**そこに載せる情報をイベントとして揃える**ところまで |
| 通知パターン5（実行時エラー） | 扱わない | 実行層が dry-run のため注入できない。Phase 5 |
| 図3 の承認フロー（承認 / 却下 / TTL / 二重押下） | 扱わない | Phase 4・5 |
| 運用原則1「思考プロセスの維持」 | **部分的に扱う** | `reason`（LLM の判断理由）と提案アクションをイベントに含める。要約・マスキング・スレッド分割は Phase 4 |
| 運用原則2「承認内容の同一性保証」 | **前提だけ扱う** | 「ボタンの `value` は Trace ID のみ」を成立させるには、**第3層の時点で Trace ID が存在している**必要がある。Phase 3 で Trace ID を導入する（3.5 / D-016）。署名検証・ストアからの再取得は Phase 4 |
| 運用原則3「2段階確認」 | 扱わない | Slack UI の話。Phase 4 |
| 運用原則4「承認ステートの一貫性」 | **インターフェースだけ扱う** | ステートストア・分散ロック・TTL は Phase 4 の責務。Phase 3 は「Trace ID をキーに承認対象を復元できるだけの情報をイベントに載せる」ことで足りるかを確認する |
| 運用原則5「メッセージ状態の動的更新」 | 扱わない | Phase 4 |
| 「App は待機せず解放」（図3 の注記） | **扱う（ただし PoC の形で）** | 拒否されたエージェントプロセスは**承認を待たずに終了する**。終了コードで分岐を表現する（3.8） |
| OPA の Decision Log と監査ログの役割分担 | **扱う** | Decision Log を「層の外側からの裏取り」に使う（D-014 の第3層版）。`policy_override` の記録は Phase 4 |

**この表の要点**: 仕様書の承認フローは「第3層が理由つきで拒否し、その事実が Trace ID で追跡できる」ことに
全面的に依存している。したがって Phase 3 の合否は **Slack が無くても判定できる**。
逆に、ここで `violated_rule` と Trace ID が揃わなければ Phase 4 は成立しない。

---

## 1. 目的と非目的

### 証明すること

1. **「実行してよいか」の判定を、エージェントのコードとプロンプトの外側で決定的に下せる。**
   同じアクションに対して必ず同じ `allow` が出ること、そして**ポリシーの変更がアプリの変更を伴わない**こと。
   これが [D-002](../../decisions.md#d-002-ポリシー判定を-pydantic-ではなく-opa-に置く)（Pydantic に書かず OPA に置く）の実証になる。
2. **拒否が理由とセットで返る。**
   `allow: false` に必ず規則 ID と人間が読める理由が伴う（[architecture.md](../../architecture.md) 設計原則3）。
   理由が取れない拒否は、仕様書の通知パターン3 を構成できない＝エスカレーションが機能しない。
3. **第3層が評価できないとき、安全側に倒れる（Fail-Closed）。**
   OPA の停止・タイムアウト・5xx・ポリシー未ロード（undefined）・decision の形の逸脱、
   いずれの場合も `allow` に化けず、実行層に到達せず、人間へのハンドオフに向かう。
   **「評価できなかった」と「評価して拒否された」を取り違えない**（仕様書のパターン4 と パターン3 の分離）。
4. **第2層を通過した危険なアクションが、第3層で止まる。**
   Phase 2 の N-01（本番の `kubectl_delete_pvc`）がここで拒否される。層の連結の実証。
5. **第3層は構造を保証しない（層の順序に必然性がある）。**
   必須フィールドを欠いた入力を OPA に直接投げると、Rego の undefined セマンティクスにより
   **ルールが発火せず `allow: true` になる**。第2層が先に立っていることが第3層の前提である（N-02）。

### 証明しないこと（意図的にスコープ外）

- **ポリシーの網羅性。** 「本番で危険な操作」を漏れなく列挙することは PoC の目的ではない。
  検証するのは**ポリシーを外部化した仕組みが決定的に効くこと**であり、ルールの本数ではない。
- **OPA の可用性対策。** 冗長化・サイドカー化・WASM 埋め込み・ローカルキャッシュは採らない。
  単一障害点であることを隠さず、Fail-Closed で受け止める（[D-015](../../decisions.md)）。
- **拒否からの自動リカバリ。** deny を LLM に戻して再提案させない（[D-012](../../decisions.md) と同じ思想）。
  deny は確定的な結果であり、次に動くのは人間である。
- **実際の破壊的操作。** 実行層は dry-run ログのみ。`allow: true` かつ `dry_run: false` でも実 `kubectl` は発行しない。
- **Slack への通知と承認フロー。** Phase 4（第0章の表）。
- **input の真正性。** エージェントが `environment` を詐称した場合の検知には限界がある（N-01）。

---

## 2. 構成

### 2.1 第3層は HTTP 境界を持つ（第2層との非対称性）

```
  agents/main.py
        │ ProposedAction（第2層を通過した型付きオブジェクト）
        ▼
  agents/policy.py  ── POST /v1/data/sre/agent/decision ──▶ ┌─────────────┐
   （第3層の唯一の入口）        {"input": {...}}              │  OPA :8181  │
        ◀── {"result": {"allow": ..., "violated_rules": []}} │  opa/*.rego │
        │                                                    └─────────────┘
        │                                                       │ Decision Log (stdout)
        ├── allow: true  ─▶ 実行層（dry-run ログのみ）           ▼
        ├── allow: false ─▶ EscalationEvent(policy_denied)    docker compose logs opa
        └── 評価不能     ─▶ EscalationEvent(policy_unavailable)   ← 層の外側からの裏取り
```

[D-010](../../decisions.md) で整理した非対称性が、ここで実害として出る。

| | 第2層 (JSON Validator) | 第3層 (OPA) |
| --- | --- | --- |
| 実行形態 | 同一プロセス・ライブラリ | **別プロセス・別コンテナ** |
| 呼び出し | 関数呼び出し | HTTP（`POST /v1/data/...`） |
| 「層自体のダウン」 | 原理的に無い | **ありうる（単一障害点）** |
| 失敗の種類 | 入力が異常 | **入力が異常 + 層が異常**の2系統 |
| 判定の再現性 | 入力が同じなら必ず同じ | ポリシーとデータが同じなら必ず同じ（内部状態を持たない） |
| 異常時の既定の挙動 | 例外送出（安全側） | **設計しないと「例外を握り潰して通す」になりうる** |

第2層の異常系は「入力の異常」だけだった。第3層は**層そのものが落ちうる**ため、
Phase 1（層の障害）と Phase 2（入力の異常）の**両方の性質を持つ**。
したがって異常系シナリオも 2 系統に分ける（4.3 拒否 / 4.4 層の障害）。

### 2.2 検証経路

```
正常系（allow）  --incident ─▶ 第1層 ─▶ 第2層 ─▶ 第3層 ─▶ allow:true  ─▶ 実行層(dry-run)  終了コード 0
拒否（deny）     --incident ─▶ 第1層 ─▶ 第2層 ─▶ 第3層 ─▶ allow:false ─▶ Escalation(pattern3) 終了コード 3
評価不能         --incident ─▶ 第1層 ─▶ 第2層 ─▶ 第3層 ─▶ ✗          ─▶ Escalation(pattern4) 終了コード 4
第2層で失敗      --incident ─▶ 第1層 ─▶ 第2層 ✗（第3層には問い合わせない）                    終了コード 1
```

**第2層が失敗したら第3層には問い合わせない。** 仕様書の図1 と同じ順序だが、これは見た目の整合ではなく
安全上の要請である。構造未保証のデータを Rego に渡すと、欠落したフィールドを参照するルールが
undefined になって発火せず、**違反ゼロ＝ `allow: true` に化ける**（N-02 で実証する）。

### 2.3 エージェントは「何が禁止されているか」を知らない

設計原則1 との線引きを明示する。第3層の存在をエージェントから隠すことはできない
（問い合わせる主体がエージェントだから）。隠すのは**判定の中身**である。

| エージェントが知ること | 可否 | 理由 |
| --- | --- | --- |
| ポリシーエンジンに実行可否を問い合わせること | **知る** | 呼び出す主体である以上、避けられない |
| 問い合わせ先の URL とタイムアウト | **知る** | 接続設定であって判定ではない |
| 評価不能なら実行してはいけないこと（Fail-Closed） | **知る** | これは層の判定ではなく**エージェントの規律**。ここだけは自前で持つ |
| どの環境のどの操作が禁止か | **知らない** | Rego の中だけに存在する。`agents/` に environment / operation による分岐を書かない（判定基準7） |
| 禁止を避けるように提案を調整すること | **しない** | プロンプトに「危険な操作を避けよ」を書かないのは Phase 2 から一貫（3.6 の線引き） |

---

## 3. 設計

### 3.1 判定は 1 リクエスト 1 decision 文書で受け取る

問い合わせ先は `POST /v1/data/sre/agent/decision` の 1 本に固定し、
`allow` と `violated_rules` を**同時に**返させる。

```jsonc
// リクエスト
{"input": {"action": { ...ProposedAction の JSON... }, "trace_id": "..."}}

// レスポンス
{"result": {"allow": false,
            "violated_rules": [{"rule": "PROHIBIT_PVC_DELETE", "message": "..."}]}}
```

`allow` と理由を別クエリにしない理由: 2 回問い合わせると、その間にポリシーが差し替わりうるし、
片方だけ成功する失敗モードが増える。**拒否と理由は不可分**（設計原則3）なので、同じ評価結果から取り出す。

**`default allow := false` を書かない。** 代わりに「ポリシーが読み込まれていなければ `result` 自体が返らない
（undefined）」という OPA の挙動をそのまま使い、エージェント側で Fail-Closed する。
`default` を置くと、**「ポリシーが評価して拒否した」と「ポリシーが存在しない」が同じ `allow: false` に潰れる**。
前者は仕様書のパターン3（人間の承認で実行しうる）、後者はパターン4（そもそも安全性が不明）であり、
人間に見せるべきものが違う。区別できる形を優先する。

### 3.2 OPA の応答を無検証で信じない（[D-017](../../decisions.md)）

第2層は「LLM の出力を信じない」層だった。同じ態度を**第3層の応答**にも適用する。
`allow` は実行可否そのものなので、ここが素通りすると多層防御の最後の 1 枚が紙になる。

```python
# agents/schemas.py に追加する（設計イメージ）

class ViolatedRule(BaseModel):
    model_config = LAYER2_MODEL_CONFIG        # strict=True / extra="forbid" を共用する
    rule: str = Field(min_length=1)           # 規則 ID（例: PROHIBIT_PVC_DELETE）
    message: str = Field(min_length=1)        # 人間が読む理由（日本語）

class PolicyDecision(BaseModel):
    model_config = LAYER2_MODEL_CONFIG
    allow: bool
    violated_rules: list[ViolatedRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _deny_must_have_reason(self) -> "PolicyDecision":
        # 理由なき拒否も、違反つきの許可も、ポリシー側のバグである。
        # どちらも「評価不能」として扱い、allow には倒さない。
        ...

class OpaResponse(BaseModel):
    # エンベロープだけは extra を許す。OPA が decision_id / metrics / provenance を足すため。
    # 「層をまたぐデータ」ではなく OPA の API 表現なので、ここは forbid しない。
    model_config = ConfigDict(extra="ignore", strict=True)
    result: PolicyDecision | None = None      # None = undefined = 評価不能
    decision_id: str | None = None            # Decision Log との突合に使う
```

設計上の要点:

- **`OpaResponse.model_validate_json(response.text)` を使う。** 第2層で得た知見
  （strict は JSON 入力と Python 入力で挙動が違う。`model_validate(dict)` は enum に実インスタンスを要求する）
  をそのまま適用する。`response.json()` → `model_validate(dict)` の 2 段にしない。
- **`result` が無い（undefined）= 評価不能。** `allow: false` として扱わない（3.1 の理由）。
- **`allow: true` なのに `violated_rules` が非空、`allow: false` なのに空** は自己矛盾であり、
  ポリシー側のバグ。どちらも**評価不能**に倒す（allow には絶対に倒さない）。
- **`extra="forbid"` を decision に付ける。** Rego が返すキーが増えたら気づけるようにする。
  気づかずに無視すると、将来「新しい条件つき許可」を見落とす。

### 3.3 Fail-Closed の定義（何をもって「評価できなかった」とするか）

「OPA が落ちたら止める」だけでは足りない。**止まらずに通ってしまう経路**を列挙して塞ぐ。
以下はすべて `PolicyUnavailableError` に正規化し、実行層に到達させない。

| # | 失敗モード | 具体例 | 対応シナリオ |
| --- | --- | --- | --- |
| 1 | 接続不能 | OPA コンテナ停止、ポート違い、DNS 失敗 | X-01 |
| 2 | タイムアウト | 応答が `OPA_TIMEOUT_SECONDS` を超える | X-02 |
| 3 | HTTP エラー | 5xx / 4xx（パス誤り・リクエスト不正を含む） | X-03 |
| 4 | undefined | 200 だが `result` が無い（ポリシー未ロード / パッケージ名誤り） | X-04 |
| 5 | decision の形の逸脱 | `allow` が文字列、`violated_rules` 欠落、未知のキー、自己矛盾 | X-05 |
| 6 | JSON として壊れている | 200 だが本文が JSON でない | X-05 に含める |

**エージェント側でリトライしない。** [D-012](../../decisions.md)（第2層の修復リトライを入れない）と同じ理由で、
リトライを挟むと「評価不能だったが最終的に通った」が確率的に起き、検知の決定性が測れなくなる。
実運用で短いリトライを入れる是非は result.md の提言に書く。

**deny は例外にしない。** `evaluate_action()` は拒否を**戻り値**（`PolicyDecision(allow=False, ...)`）で返し、
評価不能だけを例外にする。両者を同じ例外型にすると、仕様書のパターン3 とパターン4 を
呼び出し側が取り違える。**遮断されたという結果は同じでも、人間に見せるべきものと次の一手が違う。**

```python
# agents/policy.py（設計イメージ）

class PolicyUnavailableError(RuntimeError):
    """第3層が評価できなかった。allow ではない。deny でもない。"""

def evaluate_action(action: ProposedAction, *, trace_id: str,
                    original_request: str) -> PolicyDecision:
    """第3層の唯一の入口。評価不能は PolicyUnavailableError（Fail-Closed）。"""
```

### 3.4 ポリシー（`opa/policy.rego`）とデータ（`opa/data.json`）

ルールは**少なく、説明可能に**する。網羅は目的ではない（1. 非目的）。
仕様書の通知パターン3 に出てくる `PROHIBIT_PRODUCTION_PVC_DELETE` の書式に合わせ、規則 ID は大文字スネークとする。

| 規則 ID | 条件 | 意図 |
| --- | --- | --- |
| `PROHIBIT_PVC_DELETE` | `operation == kubectl_delete_pvc`（**全環境**） | データ消失は不可逆。環境に依らず人間の承認を要する |
| `PROHIBIT_PRODUCTION_DESTRUCTIVE` | `environment == production` かつ 破壊系 | 本番の破壊的操作。`PROHIBIT_PVC_DELETE` と**意図的に重なる**（理由が複数返ることの検証） |
| `PROHIBIT_PROTECTED_NAMESPACE` | `target.namespace` が保護 namespace かつ 参照系以外 | クラスタ基盤への操作。環境に依らない |
| `REQUIRE_DRY_RUN_IN_PRODUCTION` | `environment == production` かつ 変更系 かつ `dry_run == false` | 本番の変更は dry-run 経由に限る |
| `PROHIBIT_PRODUCTION_SCALE_TO_ZERO` | `environment == production` かつ `kubectl_scale` かつ `replicas == 0` | 実質的な停止。**フィールド相互依存の判定**（第2層でも強制出力でも書けない領域） |
| `REQUIRE_NAMESPACE_ENVIRONMENT_MATCH` | `target.namespace` が `environment` の namespace 一覧に無い | ラベルと実体の食い違いを検知する。N-01 の限界の議論に使う |

参照系（`kubectl_get` / `kubectl_describe` / `kubectl_logs`）は**どのルールにも該当しない**ため、
違反ゼロ＝ `allow: true` になる。allow を書くのではなく「違反が無いこと」で allow が決まる構造にする。

```rego
# opa/policy.rego（設計イメージ）
package sre.agent

import rego.v1

decision := {
	"allow": count(violated_rules) == 0,
	# 順序を固定する。D-01（決定性）で毎回同一の JSON になることを見るため。
	"violated_rules": sort([v | some v in violations]),
}

violations contains {
	"rule": "PROHIBIT_PVC_DELETE",
	"message": sprintf("PVC の削除は不可逆なため自動実行を許可しません（対象: %v/%v）",
		[input.action.target.namespace, input.action.target.name]),
} if input.action.operation == "kubectl_delete_pvc"
```

**操作の分類と保護対象は `opa/data.json` に外出しする。**

```jsonc
{"catalog": {
  "read_only_operations":   ["kubectl_get", "kubectl_describe", "kubectl_logs"],
  "mutating_operations":    ["kubectl_rollout_restart", "kubectl_scale"],
  "destructive_operations": ["kubectl_delete_pod", "kubectl_delete_pvc"],
  "protected_namespaces":   ["kube-system", "kube-public", "kube-node-lease"],
  "environment_namespaces": {"production": ["prod"], "staging": ["staging"], "development": ["dev"]}
}}
```

理由: **「ルール（判定の形）」と「データ（対象の一覧）」を分ける**と、保護対象の追加が
ポリシーのレビューではなくデータのレビューで済む。実運用でクラスタ情報を外部から供給する構成
（OPA の bundle / `data` API）への移行余地もここで示せる。トップレベルのキーは
パッケージパス `data.sre.agent` と衝突しないよう `catalog` とする（未決事項 7）。

**第2層の `Operation` enum との関係**（Phase 2 未決事項6 の最終確認）:
第2層は「語彙にあるか」を allow-list で保証し、第3層は「その語彙の要素を今この対象に実行してよいか」を判定する。
`data.json` の 3 分類は enum の**部分集合による分類**であり、語彙そのものの定義ではない。
enum に無い操作は第2層で落ちるため、第3層が deny-list を持つ必要がない。この線引きの妥当性を result.md で結論づける。

### 3.5 Trace ID（相関 ID）の導入（[D-016](../../decisions.md)）

Phase 2 の積み残し「`EscalationEvent` に相関 ID が無い」をここで解消する。Phase 4 ではなく Phase 3 で入れる理由は、
仕様書の運用原則2（**ボタンの `value` には Trace ID のみを格納する**）が、
「拒否された時点で Trace ID が確定している」ことを前提にしているため。

- **粒度**: 1 インシデント処理 = 1 Trace ID。`--incident` の 1 回の実行で発番し、
  第1層の `request_id`、第2層の検証ログ、第3層の decision、エスカレーションイベントのすべてに載せる。
- **`input` の形**: `{"action": {...}, "trace_id": "..."}` のエンベロープにする。
  **Rego は `input.trace_id` を参照しない。** 参照した瞬間、Trace ID によって判定が変わりうることになり、
  ポリシーの決定性が壊れる。判定材料（`action`）と監査の突合キー（`trace_id`）を構造的に分離する。
- **効果**: OPA の Decision Log に `input` がそのまま記録されるため、**Trace ID で
  エージェントのログと OPA の判定記録を突合できる**。これが D-014 で要求した「層の外側からの裏取り」になる。
- **Phase 2 との差分**: `EscalationEvent` に `trace_id` が増え、第2層の構造化ログにも出るようになる。
  Phase 2 の判定結果は変わらない（検知の内容に影響しない）が、ログの形は変わる。
  Phase 2 の `assets/` は上書きせず、そのまま残す。

### 3.6 `EscalationEvent` の拡張とハンドオフ種別

ハンドオフ経路は 1 本に保つ（Phase 2 の設計）。層ごとにイベント型を分けず、**種別で区別する**。
通知パターン番号（1〜5）は Slack 側の関心事なので、イベントには持たせない。Phase 4 が対応付ける。

```python
class EscalationEvent(BaseModel):
    layer: Literal["layer1_litellm", "layer2_json_validator", "layer3_opa"]
    kind: Literal["validation_failed", "policy_denied", "policy_unavailable"]  # 追加
    trace_id: str                                     # 追加（3.5）
    reason: str
    errors: list[dict] = []
    action: ProposedAction | None = None              # 追加。パターン3 の「提案されたコマンド」
    violated_rules: list[ViolatedRule] = []           # 追加。パターン3 の「違反ポリシー名」
    raw_output: str | None = None
    original_request: str
    occurred_at: datetime
```

| `kind` | 発生条件 | Phase 4 での対応 |
| --- | --- | --- |
| `validation_failed` | 第2層の `ValidationError`（Phase 2 で実装済み） | 通知パターン2 |
| `policy_denied` | 第3層が `allow: false`（理由つき） | 通知パターン3（承認ボタンあり） |
| `policy_unavailable` | 3.3 の失敗モード 1〜6 | 通知パターン4（ボタンなし・Fail-Closed） |

`action` を載せるのは運用原則1・2 のため。**Phase 4 のステートストアは、このイベントだけで
承認対象を復元できなければならない**（仕様書「実行内容は必ずサーバ側のストアから取得する」）。
足りるかどうかを Phase 3 の時点で確認しておく（未決事項 6）。

### 3.7 実行層（dry-run）

`allow: true` のときだけ到達する。PoC では `event=execution.dry_run` を構造化ログに出すだけで、
実 `kubectl` は発行しない（CLAUDE.md）。アクションの `dry_run` フィールドが `false` でも発行しない。

この層を置く目的は「`allow: true` が実行に**繋がっている**こと」を示すためであり、
実行そのものの検証ではない。`allow: false` のときにこのログが出ていないことが、
Fail-Closed / 拒否の実効性のエビデンスになる（判定基準4）。

### 3.8 CLI と終了コード

`--prompt`（Phase 1）と `--incident`（Phase 2）の呼び出し方は変えない。`--incident` は**第3層まで通す**ようになる。

```bash
python -m agents.main --incident "本番の payment-api の Pod が CrashLoopBackOff"   # 第3層まで
python -m agents.main --incident "..." --no-policy     # 第2層で止める（Phase 2 の経路を再現する）
```

| 終了コード | 意味 | 対応する仕様書の分岐 |
| --- | --- | --- |
| 0 | `allow: true` → 実行層（dry-run）まで到達 | 図1「第3層 allow true」 |
| 1 | 第1層の呼び出し失敗、または第2層の `ValidationError` | 図1「第2層 検証失敗」（Phase 2 と同じ） |
| 2 | 環境変数の設定不足（Phase 1 から変更なし） | — |
| 3 | 第3層が `allow: false` | 図1「第3層 allow false」＝パターン3 |
| 4 | 第3層が評価不能（Fail-Closed） | 図1「第3層 応答なし / Timeout」＝パターン4 |

終了コードを分けるのは、シナリオ実行スクリプトが**分岐を一意に判定できる**ようにするため。
`--repeat` で複数回実行した場合は、最も重いもの（4 > 3 > 1 > 0）を返す。

**拒否されてもプロセスは承認を待たない**（仕様書「App プロセスは待機せず解放」）。
PoC では承認の受け口が無いため、イベントを出して終了する。再開は Phase 5。

### 3.9 障害注入（`agents/` を変更しない）

Phase 1・2 と同じ原則。異常系は**エージェントの外側の設定だけ**で作る。

| シナリオ | 注入方法 | 注入が効いていることの確認（[D-014](../../decisions.md)） |
| --- | --- | --- |
| X-01 接続不能 | `docker compose stop opa` | `docker compose ps` で OPA が停止していること |
| X-02 タイムアウト | `OPA_URL` を fault-injector（`FAULT_MODE=timeout`）に向ける | injector のアクセスログに当該リクエストがあること |
| X-03 HTTP エラー | 同上（`FAULT_MODE=server_error`） | 同上 |
| X-04 undefined | `OPA_URL` のパスを存在しないルールに向ける／空のポリシーディレクトリで OPA を起動 | OPA が 200 と `{}` を返すことを `curl` で確認 |
| X-05 decision の逸脱 | `opa/faults/` の壊れたポリシーを読ませて起動 | `curl` で壊れた decision が返ることを確認 |

`mocks/fault_injector.py` は**改修不要**。既存の `timeout` / `server_error` モードはパス非依存に応答するため、
`OPA_URL` を向けるだけで第3層の障害注入装置として流用できる。
Phase 1・2 のモードには一切手を入れない（再現性を保つ）。

`docker-compose.yml` の変更点:

- OPA の `profiles: ["phase3"]` を**外す**。既定で起動する。
  第3層の不在を「たまたま起動していなかった」で誤魔化さないため（D-014 の趣旨）。
- Decision Log をコンソールに出す（`--set=decision_logs.console=true`）。裏取りの主エビデンス。
- ポリシーディレクトリを変数化する（`${OPA_POLICY_DIR:-./opa}`）。X-04 / X-05 / M-01 の切り替えに使う。
- `--watch` を付ける。M-01（ポリシーの独立変更）で、**エージェントを触らずに判定が変わる**ことを示す。

**OPA の状態リセット**: OPA はポリシー評価に内部状態を持たないため、Phase 1 の
`docker compose restart litellm` に相当する手順は原則不要。ただしポリシー／データを差し替えたときは
再読込（`--watch`）または再起動が要る。**どちらで反映したかをシナリオごとに記録する**。

---

## 4. 検証シナリオ

### 4.1 ポリシー単体（`opa test opa/`。決定的・判定基準の主軸）

アプリケーションを一切起動せずに実行できることが [D-002](../../decisions.md#d-002-ポリシー判定を-pydantic-ではなく-opa-に置く) の主張そのものなので、
**単体テストを先に完成させる**（Phase 2 で単体 → 実 LLM の順にした理由と同じ）。

| ID | 内容 | 期待 |
| --- | --- | --- |
| U-01 | 参照系 × 全環境（3 環境 × 3 操作） | 全件 `allow: true`、`violated_rules` は空 |
| U-02 | `kubectl_delete_pvc` × 全環境 | 全件 deny。`PROHIBIT_PVC_DELETE` を含む |
| U-03 | `kubectl_delete_pod` × production / staging / development | production のみ `PROHIBIT_PRODUCTION_DESTRUCTIVE` で deny |
| U-04 | production × 変更系 × `dry_run` の true / false | `false` のときだけ `REQUIRE_DRY_RUN_IN_PRODUCTION` で deny |
| U-05 | `kubectl_scale` × `replicas` 0 / 1 / 100 × production / staging | production の 0 のみ追加で deny |
| U-06 | 保護 namespace（`kube-system`）× 参照系 / 変更系 | 参照系は allow、変更系は `PROHIBIT_PROTECTED_NAMESPACE` で deny |
| U-07 | `environment` と `namespace` の不一致 | `REQUIRE_NAMESPACE_ENVIRONMENT_MATCH` で deny |
| U-08 | 複数ルールに同時抵触（本番 PVC 削除 + `dry_run: false`） | `violated_rules` が**複数件**返り、順序が安定している |

各ルールについて **allow 側と deny 側の両方**のケースを書く。deny 側しか書かないと、
「常に deny するポリシー」でもテストが通ってしまう。

### 4.2 正常系（エージェント経路）

| ID | 入力 | 期待挙動 |
| --- | --- | --- |
| P-01 | 実 LLM（`--incident` で参照系が提案される状況） | 第1層 → 第2層 → 第3層 → `allow: true` → 実行層（dry-run）。終了コード 0 |
| P-02 | Phase 2 の `valid_rollout_restart.json`（本番 × 変更系 × `dry_run: true`） | `allow: true`。本番でも dry-run なら通ることの確認 |
| P-03 | Phase 2 の `valid_replicas_min.json`（staging × scale to 0） | `allow: true`。`PROHIBIT_PRODUCTION_SCALE_TO_ZERO` が環境で効き分けること |

### 4.3 拒否（エージェント経路。仕様書パターン3 の入口）

| ID | 入力 | 期待挙動 |
| --- | --- | --- |
| B-01 | Phase 2 の `n01_dangerous_delete_pvc.json`（本番 PVC 削除 / `dry_run: false`） | deny。`violated_rules` が複数。**終了コード 3**、実行層のログが出ない |
| B-02 | 本番 × `kubectl_delete_pod` | deny（`PROHIBIT_PRODUCTION_DESTRUCTIVE`） |
| B-03 | 本番 × `kubectl_scale` × `dry_run: false` | deny（`REQUIRE_DRY_RUN_IN_PRODUCTION`） |
| B-04 | `kube-system` × `kubectl_rollout_restart` | deny（`PROHIBIT_PROTECTED_NAMESPACE`） |
| B-05 | 本番 × `kubectl_scale` × `replicas: 0` × `dry_run: true` | deny（`PROHIBIT_PRODUCTION_SCALE_TO_ZERO`）。dry-run でも止まる |

**すべてで `EscalationEvent(kind="policy_denied")` が構造化ログに出力され、
仕様書パターン3 に必要な要素（対象環境・提案アクション・違反ポリシー名・理由・Trace ID）が揃っていること**を確認する。

### 4.4 第3層の障害（Fail-Closed。本フェーズの主戦場）

| ID | 注入 | 期待挙動 |
| --- | --- | --- |
| X-01 | OPA 停止（接続不能） | `PolicyUnavailableError` → `kind="policy_unavailable"` → 終了コード 4。実行層に到達しない |
| X-02 | タイムアウト（`OPA_TIMEOUT_SECONDS` 超過） | 同上。加えて**エージェントが待ち切る時間**を記録する |
| X-03 | HTTP 500 | 同上 |
| X-04 | undefined（ポリシー未ロード / パス誤り） | 同上。**`allow: false` として扱っていないこと**をログで示す |
| X-05 | decision の逸脱（`allow` が文字列 / 違反つき allow / 未知のキー） | 同上。どのフィールドで落ちたかが取れること |

全シナリオで **3.9 の「注入が効いていることの確認」を先に実施し、記録する**。
Phase 2 で「注入が効いておらず全件が静かに正常系になった」失敗を起こしているため（[D-014](../../decisions.md)）、
確認できないシナリオは**結果を採用しない**。

### 4.5 決定性・限界・計測・独立変更

| ID | 分類 | 内容 | 期待 | 判定基準に使うか |
| --- | --- | --- | --- | --- |
| D-01 | 決定性 | 同一アクションを 10 回評価。途中で OPA を再起動する | `allow` と `violated_rules` が完全一致（`decision_id` 以外） | **使う** |
| N-01 | 限界 | `environment: "staging"` と詐称した本番相当のアクション（namespace は `prod`） | `REQUIRE_NAMESPACE_ENVIRONMENT_MATCH` で検知できる。**ただし namespace も併せて詐称されれば素通りする**ことを示す | **使う**（限界の明示） |
| N-02 | 限界 | 必須フィールドを欠いた JSON を **OPA に直接** `curl` で投げる（第2層をバイパス） | ルールが undefined で発火せず **`allow: true` になる**。第3層は構造を保証しない | **使う**（層の順序の必然性） |
| M-01 | 独立変更 | `opa/` のファイルだけを差し替え、**エージェントを再起動せずに** allow → deny を反転させる | 判定が変わる。`agents/` の差分ゼロ | **使う**（D-002 の実証） |
| L-01 | 計測 | 第3層の評価レイテンシを実測（n ≥ 20）し、`OPA_TIMEOUT_SECONDS` を確定する | — | 使わない（計測） |
| C-01 | 比較 | 同じ判定を第2層（Pydantic バリデータ）に書いた場合との比較 | — | 使わない（考察） |

**L-01 の位置づけ**: Phase 2 の最大の教訓は「`timeout` はワークロードごとに実測して決める」だった
（`timeout` を規定するのは出力トークン数＝スキーマの `max_length`）。第3層の評価は
LLM 呼び出しと桁が違う（ミリ秒オーダーのはず）が、**桁が違うことを実測で確認してから**決める。
初期値は仕様書の記載に合わせて 3.0 秒とし、実測後に確定する。短すぎれば健全な OPA を誤って
評価不能にし、長すぎれば人間への引き渡しが遅れる。

**C-01 の位置づけ**: 「このくらいのルールなら Pydantic のバリデータで書けるのでは」という問いへの答え。
失われるのは (1) `opa test` による単体テスト、(2) ポリシーとアプリのデプロイ分離（M-01）、
(3) Decision Log という独立した監査記録、(4) ルールの可読性（レビュー対象が Python になる）。
実測ではなく構造の比較として result.md の考察に書く。

### 4.6 実施順序

U 系（ポリシー単体）→ P 系（正常系）→ B 系（拒否）→ D-01（決定性）→ L-01（レイテンシと `timeout` 確定）
→ X 系（Fail-Closed）→ N-01 / N-02（限界）→ M-01（独立変更）→ C-01（考察材料）。

ポリシー単体を先に固める。エージェント経路から始めると、deny の原因が
ポリシーなのか `input` の組み立てなのか HTTP なのか切り分けられなくなる（Phase 2 の 4.4 と同じ理由）。

---

## 5. 判定基準

以下を**すべて**満たしたとき Phase 3 合格とする。

1. **U-01〜U-08 がすべて `opa test opa/` でパスし、各ルールに allow / deny 両方のケースがある。**
   エージェントを起動せずにポリシーだけを単体テストできること（[D-002](../../decisions.md#d-002-ポリシー判定を-pydantic-ではなく-opa-に置く) の実証）。
2. **B-01〜B-05 のすべてで `allow: false` になり、`violated_rules` に規則 ID と人間が読める理由が
   1 件以上含まれる。** 理由の無い拒否が 1 件でもあれば不合格（設計原則3）。
   通知パターン3 に必要な要素が `EscalationEvent` に揃っていること。
3. **B-01（Phase 2 の N-01 と同一の入力）が第3層で止まる。**
   第2層を通過した危険なアクションが第3層で拒否される＝**層の連結**が成立していること。
4. **X-01〜X-05 のすべてで Fail-Closed する。** 具体的には 4 点すべて:
   (a) `allow: true` にならない、(b) 実行層のログが出力されない、
   (c) `EscalationEvent(kind="policy_unavailable")` が出力される、(d) 終了コードが 4。
   とくに **X-04（undefined）を `allow: false` に潰していない**こと（パターン3 と 4 の区別）。
5. **D-01 で、OPA の再起動を挟んでも 10 回の判定が完全に一致する。**
   第1層（cooldown で結果が変わった）との対比。第3層が内部状態を持たないことの確認。
6. **N-02 で、第2層をバイパスした不完全な入力が `allow: true` になることを示せる。**
   第3層が構造を保証しないこと、層の順序に必然性があることの実証。
   ここで OPA が拒否してしまう場合、Rego 側に構造検証を書いてしまっている（[D-002](../../decisions.md#d-002-ポリシー判定を-pydantic-ではなく-opa-に置く) の逆方向の違反）ため不合格。
7. **M-01 で、`opa/` 配下のファイル変更だけで判定が反転し、`agents/` の差分がゼロである。**
   かつ `agents/` に `production` / `kubectl_delete` 等による条件分岐が存在しないこと（grep で示す）。

**判定基準に使わないもの**: L-01（計測）、C-01（構造比較）、P-01 の LLM 出力内容（非決定的。
経路が繋がることだけを見る）。

---

## 6. 観測方法（エビデンスの取り方）

| 手段 | 内容 | 用途 |
| --- | --- | --- |
| `opa test opa/ -v` の出力 | U 系の結果一覧 | 判定基準1。`assets/` に保存 |
| エージェントの構造化ログ `event=policy.decision` | `trace_id` / `allow` / `violated_rules` / 所要時間 | 判定基準2・3・5 |
| エージェントの構造化ログ `event=policy.unavailable` | 失敗モード（接続不能 / タイムアウト / undefined / 形の逸脱）と詳細 | 判定基準4 |
| エージェントの構造化ログ `event=execution.dry_run` | **出ていないこと**が拒否・Fail-Closed の実効性の証拠 | 判定基準4(b) |
| プロセスの終了コード | `echo $?` | 判定基準2・4 |
| **OPA の Decision Log**（`docker compose logs opa`） | `input`（`trace_id` を含む）/ `result` / `decision_id` / 時刻 | **層の外側からの裏取り**（D-009 / D-014 の第3層版） |
| `curl` で OPA を直接叩いた生 JSON | 判定の生データ。N-02 はこの経路でのみ実施 | 判定基準6 |
| `docker compose ps` / injector のアクセスログ | 注入が効いていることの確認 | X 系すべての前提（D-014） |
| `git diff --stat agents/` と `grep -rn` | ポリシーがアプリに漏れていないこと | 判定基準7 |

### 第3層で「層の外側からの裏取り」が必要な理由

第2層には不要だった（入力も出力も我々が完全に握っており、純関数だから）。第3層では必要になる。

- **`allow: true` は「ポリシーが許可した」のか「評価が起きなかった」のかが、エージェント側からは同じに見える。**
  Decision Log に該当 Trace ID の記録があるかどうかで初めて区別できる。
- Phase 2 の失敗（注入が効かず静かに正常系）と同じ構造の罠が、第3層では
  **OPA が古いポリシーを読んだまま動いている**という形で現れうる。
  M-01 でポリシーを差し替えたのに判定が変わらないとき、それが「ポリシーが間違っている」のか
  「反映されていない」のかは Decision Log を見ないと分からない。

生データは `assets/` に置き、result.md から参照する。**ファイル名は使い回さない**（Phase 1 の失敗）。
LiteLLM のログを貼る場合は秘密情報の除去を忘れない（docs/README.md 運用ルール2）。

---

## 7. 未決事項・確認事項

着手時に確認し、判明した内容は result.md に記録する。設計判断が変わる場合は decisions.md を更新する。

1. **第3層の評価レイテンシと `OPA_TIMEOUT_SECONDS` の確定値。** → L-01 で実測。初期値は 3.0 秒。
2. **OPA が undefined を返すときの正確な形。** 200 + `{}` なのか、404 なのか、パスの誤りとルールの未定義で
   挙動が違うのか。X-04 で実測し、Fail-Closed の判定条件（3.2）が正しく機能するかを確認する。
3. **Decision Log の出力形式と `trace_id` の見え方。** `--set=decision_logs.console=true` で
   `input` 全体が記録されるか、`decision_id` がレスポンスにも含まれるか。含まれない場合、
   エージェントのログと Decision Log の突合キーを `trace_id` 一本にする。
4. **`opa run --watch` がバインドマウント上で効くか。** M-01 の前提。効かない場合は
   コンテナ再起動で代替し、「アプリを再起動していない」ことだけは保つ（ポリシーの独立性の主張は維持できる）。
5. **OPA イメージのバージョンと `import rego.v1` の整合。** `openpolicyagent/opa:latest` を
   固定バージョンに変えるかを含めて確認する（検証の再現性のため固定したい）。
6. **`EscalationEvent` が Phase 4 のステートストアに足りるか。** 仕様書の運用原則2・4 は
   「Trace ID をキーに承認対象のアクションをストアから復元する」ことを求めている。
   `action` + `violated_rules` + `trace_id` で足りるか、承認期限や承認者ロールの判定に
   追加情報（対象環境の危険度など）が要るかを Phase 3 の時点で見ておく。
7. **`opa/data.json` のトップレベルキーがパッケージパスと衝突しないか。** `data.sre.agent` が
   ポリシーのパッケージなので、データは `data.catalog` に置く方針だが、実際の読み込みで
   衝突やマージの問題が出ないかを確認する。
8. **`opa test` でデータファイル（`data.json`）をどう与えるか。** `opa test opa/` がディレクトリ内の
   JSON を自動で読むのか、テスト側でモックする必要があるのかを確認する。
   モックが必要な場合、**テストが実データと乖離する**リスクを result.md に記録する。
9. **第2層の `Operation` enum の線引き**（Phase 2 未決事項6 の最終確認）。
   3.4 の分類がデータ側に置けたことをもって、「語彙は第2層 / 語彙の要素の可否は第3層」の
   線引きが妥当だったかを結論づける。

---

## 8. 実施手順

実装:

- [ ] `opa/policy.rego`（3.4 の 6 ルール。`decision` 文書 1 本で `allow` と `violated_rules` を返す）
- [ ] `opa/data.json`（操作の分類・保護 namespace・環境と namespace の対応）
- [ ] `opa/policy_test.rego`（U-01〜U-08。各ルールに allow / deny 両方）
- [ ] `opa/faults/broken_decision.rego`（X-05 用。正本と同じパッケージで decision の形を壊す）
- [ ] `docker-compose.yml`（OPA の `profiles` を外す / Decision Log をコンソールへ /
      `OPA_POLICY_DIR` の変数化 / `--watch` / イメージのバージョン固定）
- [ ] `agents/schemas.py`（`ViolatedRule` / `PolicyDecision` / `OpaResponse` / `EscalationEvent` の拡張）
- [ ] `agents/policy.py`（`evaluate_action()` と `PolicyUnavailableError`。第3層の唯一の入口）
- [ ] `agents/main.py`（Trace ID の発番と伝播 / 第3層の呼び出し / 実行層の dry-run ログ /
      終了コード 3・4 / `--no-policy`）
- [ ] `tests/fixtures/policy/`（B 系・P 系の入力。Phase 2 の fixture で足りるものは**再利用する**）
- [ ] `tests/test_layer3_policy.py`（decision のパースと Fail-Closed の単体検証。
      OPA への HTTP はモックし、**ポリシーの内容は検証しない**（それは `opa test` の仕事））
- [ ] `.env.example`（`OPA_URL` を decision のフルパスに更新 / `OPA_TIMEOUT_SECONDS` / `OPA_POLICY_DIR`）

実行:

1. `opa test opa/ -v` で U-01〜U-08 を通す。出力を `assets/` に保存。
2. P-01〜P-03 をエージェント経路で実行し、`allow: true` → 実行層（dry-run）まで繋がることを確認。
3. B-01〜B-05 を実行し、`EscalationEvent(kind="policy_denied")` の中身が
   仕様書パターン3 に必要な要素を満たすことを確認。
4. D-01（10 回 + OPA 再起動）で決定性を記録。
5. L-01 でレイテンシを実測（n ≥ 20）し、`OPA_TIMEOUT_SECONDS` を確定して根拠を残す。
6. X-01〜X-05 を実行。**各シナリオの前に「注入が効いていること」を確認して記録する**（D-014）。
7. N-01（ラベル詐称の限界）と N-02（第2層バイパスで allow になること）を実行。
   N-02 は `curl` で OPA を直接叩く。
8. M-01（ポリシー差し替えだけで判定を反転）を実行し、`agents/` の差分ゼロを示す。
9. C-01 の考察材料を整理する。
10. result.md に結果・考察・実運用への提言（OPA の可用性対策、評価リトライの是非、
    ポリシーの配布と bundle 化、Decision Log の保全、input の真正性をどう担保するか）を書く。
    decisions.md に D-015〜D-017 の結果を反映し、ルート README のロードマップ表と TODO.md を更新する。
