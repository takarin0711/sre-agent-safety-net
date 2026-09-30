# OPA の異常系ポリシー（X-05 / X-04 用）

第3層の**層としての異常**を注入するためのポリシー群。`agents/` を一切変更せずに、
OPA が返す decision の形だけを壊す（design.md 3.9）。正本の `opa/` とは別ディレクトリに置く。
同じ `opa/` の下に置くと `decision` の定義が衝突して OPA が起動しなくなるため。

```bash
# X-05: allow が真偽値でない
OPA_POLICY_DIR=./mocks/opa_faults/allow_as_string docker compose up -d opa

# X-05: allow: true なのに違反が列挙されている（自己矛盾）
OPA_POLICY_DIR=./mocks/opa_faults/contradictory docker compose up -d opa

# X-05: decision に未知のキーが増えている
OPA_POLICY_DIR=./mocks/opa_faults/unknown_key docker compose up -d opa

# X-04: ポリシーが 1 つも読み込まれていない（undefined → OPA は 200 と {} を返す）
OPA_POLICY_DIR=./mocks/opa_faults/no_policy docker compose up -d opa
```

いずれも `allow` に倒れてはならない。エージェントは Fail-Closed し、
`EscalationEvent(kind="policy_unavailable")` を出して終了コード 4 で終わる（判定基準4）。
