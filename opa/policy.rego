# 第3層: 権限判定（design.md 3.4）
#
# 責務は「その操作を実行してよいか」だけである。構造が仕様どおりかは第2層の責務であり、
# ここには書かない（docs/decisions.md D-002）。したがって本ファイルに
# 「environment が必須である」といった構造検証を足してはならない。
#
# 設計上の要点:
#
# - **default allow := false を書かない。** ポリシーが読み込まれていなければ
#   data.sre.agent.decision は undefined になり、OPA は {} を返す。これをエージェント側が
#   「評価不能」として Fail-Closed する（D-015 / D-017）。default を置くと
#   「評価して拒否した」と「ポリシーが存在しない」が同じ allow: false に潰れ、
#   Handoff 仕様書の通知パターン3 と パターン4 を区別できなくなる。
# - **allow を直接書かず、違反が無いことで allow を決める。** 参照系はどのルールにも
#   該当しないため自動的に allow になる。許可条件を書き足す運用にすると、
#   ルールの追加漏れが「許可」側に倒れる。
# - **input.trace_id を参照しない**（D-016）。参照した瞬間、Trace ID の値で判定が変わりうる
#   ことになり、ポリシーの決定性が崩れる。判定材料は input.action だけである。
# - 操作の分類と保護対象は data.catalog（opa/data.json）に外出しする。
#   「ルール（判定の形）」と「データ（対象の一覧）」を分けるため。

package sre.agent

import rego.v1

# エージェントが受け取る唯一の決定文書。allow と理由を同時に返す（設計原則3）。
decision := {
	"allow": count(violated_rules) == 0,
	"violated_rules": violated_rules,
}

# 順序を固定する。D-01（決定性）で毎回同一の JSON になることを見るため。
violated_rules := sort([v | some v in violations])

action := input.action

# ── 参照系かどうか。複数のルールで使う ─────────────────────
is_read_only if action.operation in data.catalog.read_only_operations

# ── PROHIBIT_PVC_DELETE ───────────────────────────────
# PVC の削除はデータ消失を伴い不可逆。環境に依らず人間の承認を要する。
violations contains {
	"rule": "PROHIBIT_PVC_DELETE",
	"message": sprintf(
		"PVC の削除は不可逆なため自動実行を許可しません（対象: %s/%s、環境: %s）",
		[action.target.namespace, action.target.name, action.environment],
	),
} if {
	action.operation == "kubectl_delete_pvc"
}

# ── PROHIBIT_PRODUCTION_DESTRUCTIVE ───────────────────
# 本番環境の破壊的操作。PROHIBIT_PVC_DELETE と意図的に重ねている
# （理由が複数返ることの検証。U-08）。
violations contains {
	"rule": "PROHIBIT_PRODUCTION_DESTRUCTIVE",
	"message": sprintf(
		"本番環境に対する破壊的操作 %s は自動実行を許可しません（対象: %s/%s）",
		[action.operation, action.target.namespace, action.target.name],
	),
} if {
	action.environment == "production"
	action.operation in data.catalog.destructive_operations
}

# ── PROHIBIT_PROTECTED_NAMESPACE ──────────────────────
# クラスタ基盤の namespace への変更・破壊操作。環境に依らない。
violations contains {
	"rule": "PROHIBIT_PROTECTED_NAMESPACE",
	"message": sprintf(
		"保護対象の namespace %s への操作 %s は自動実行を許可しません（参照系のみ許可）",
		[action.target.namespace, action.operation],
	),
} if {
	action.target.namespace in data.catalog.protected_namespaces
	not is_read_only
}

# ── REQUIRE_DRY_RUN_IN_PRODUCTION ─────────────────────
# 本番環境の変更系は dry-run 経由に限る。
violations contains {
	"rule": "REQUIRE_DRY_RUN_IN_PRODUCTION",
	"message": sprintf(
		"本番環境の変更操作 %s は dry_run: true でのみ許可します（指定値: %v）",
		[action.operation, action.dry_run],
	),
} if {
	action.environment == "production"
	action.operation in data.catalog.mutating_operations
	action.dry_run == false
}

# ── PROHIBIT_PRODUCTION_SCALE_TO_ZERO ─────────────────
# 本番のレプリカ 0 は実質的なサービス停止。フィールド間の相互依存による判定であり、
# スキーマ検証でも構造化出力の強制でも表現できない領域（D-011）。
violations contains {
	"rule": "PROHIBIT_PRODUCTION_SCALE_TO_ZERO",
	"message": sprintf(
		"本番環境の %s を replicas: 0 にする操作は実質的な停止であり許可しません",
		[action.target.name],
	),
} if {
	action.environment == "production"
	action.operation == "kubectl_scale"
	action.replicas == 0
}

# ── REQUIRE_NAMESPACE_ENVIRONMENT_MATCH ───────────────
# 申告された environment と実際の namespace の食い違いを検知する（N-01）。
#
# 参照系は対象外とする。design.md 3.4 の表は参照系を除外していなかったが、
# 除外しないと U-06（保護 namespace × 参照系は allow）および
# 「参照系はどのルールにも該当しない」という本ポリシーの構造と矛盾する。
# 参照操作は環境ラベルが食い違っていても破壊力を持たないため、実装では除外した。
# この差分は result.md に記録する。
violations contains {
	"rule": "REQUIRE_NAMESPACE_ENVIRONMENT_MATCH",
	"message": sprintf(
		"environment=%s に対して namespace=%s は対応していません（許可: %v）",
		[action.environment, action.target.namespace, allowed],
	),
} if {
	not is_read_only
	allowed := data.catalog.environment_namespaces[action.environment]
	not action.target.namespace in allowed
}
