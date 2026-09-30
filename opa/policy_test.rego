# 第3層ポリシーの単体検証（design.md 4.1 の U-01〜U-08）。
#
# エージェントを一切起動せずに実行できることが D-002 の主張そのものである。
#
#   opa test opa/ -v
#   docker run --rm -v "$PWD/opa:/policies:ro" openpolicyagent/opa:1.21.0 test /policies -v
#
# 各ルールについて **allow 側と deny 側の両方**を書く。deny 側しか書かないと
# 「常に deny するポリシー」でもテストが通ってしまう。

package sre.agent_test

import rego.v1

import data.sre.agent

# 参照系・development・namespace 一致・dry_run true の、どのルールにも該当しない基準アクション。
base_action := {
	"action_id": "act-test",
	"environment": "development",
	"operation": "kubectl_get",
	"target": {"kind": "pod", "name": "payment-api-0", "namespace": "dev"},
	"replicas": null,
	"reason": "単体テスト用のアクション",
	"dry_run": true,
}

with_fields(overrides) := object.union(base_action, overrides)

target_in(namespace) := {"kind": "pod", "name": "payment-api-0", "namespace": namespace}

# エージェントが送るのと同じエンベロープで評価する（D-016）。
decision_for(a) := d if {
	d := agent.decision with input as {"action": a, "trace_id": "test-trace"}
}

rules_of(d) := {v.rule | some v in d.violated_rules}

# ── U-01 参照系は全環境で許可される ───────────────────────

test_u01_read_only_allowed_in_all_environments if {
	every env, namespaces in data.catalog.environment_namespaces {
		every op in data.catalog.read_only_operations {
			d := decision_for(with_fields({
				"environment": env,
				"operation": op,
				"target": target_in(namespaces[0]),
			}))
			d.allow == true
			count(d.violated_rules) == 0
		}
	}
}

# ── U-02 PVC 削除は全環境で拒否される ─────────────────────

test_u02_pvc_delete_denied_in_all_environments if {
	every env, namespaces in data.catalog.environment_namespaces {
		d := decision_for(with_fields({
			"environment": env,
			"operation": "kubectl_delete_pvc",
			"target": {"kind": "pvc", "name": "payment-db-data", "namespace": namespaces[0]},
		}))
		d.allow == false
		"PROHIBIT_PVC_DELETE" in rules_of(d)
	}
}

# allow 側: PVC 以外の削除には PROHIBIT_PVC_DELETE が付かない
test_u02_pod_delete_is_not_flagged_as_pvc_delete if {
	d := decision_for(with_fields({
		"environment": "staging",
		"operation": "kubectl_delete_pod",
		"target": target_in("staging"),
	}))
	d.allow == true
	not "PROHIBIT_PVC_DELETE" in rules_of(d)
}

# ── U-03 本番の破壊的操作だけが拒否される ─────────────────

test_u03_pod_delete_denied_in_production if {
	d := decision_for(with_fields({
		"environment": "production",
		"operation": "kubectl_delete_pod",
		"target": target_in("prod"),
	}))
	d.allow == false
	rules_of(d) == {"PROHIBIT_PRODUCTION_DESTRUCTIVE"}
}

test_u03_pod_delete_allowed_outside_production if {
	every env in ["staging", "development"] {
		d := decision_for(with_fields({
			"environment": env,
			"operation": "kubectl_delete_pod",
			"target": target_in(data.catalog.environment_namespaces[env][0]),
		}))
		d.allow == true
	}
}

# ── U-04 本番の変更系は dry_run: true のときだけ許可される ──

test_u04_production_mutating_denied_without_dry_run if {
	every op in data.catalog.mutating_operations {
		d := decision_for(with_fields({
			"environment": "production",
			"operation": op,
			"target": {"kind": "deployment", "name": "payment-api", "namespace": "prod"},
			"replicas": 3,
			"dry_run": false,
		}))
		d.allow == false
		"REQUIRE_DRY_RUN_IN_PRODUCTION" in rules_of(d)
	}
}

test_u04_production_mutating_allowed_with_dry_run if {
	every op in data.catalog.mutating_operations {
		d := decision_for(with_fields({
			"environment": "production",
			"operation": op,
			"target": {"kind": "deployment", "name": "payment-api", "namespace": "prod"},
			"replicas": 3,
			"dry_run": true,
		}))
		d.allow == true
		count(d.violated_rules) == 0
	}
}

# ── U-05 本番の replicas: 0 だけが拒否される ───────────────

test_u05_production_scale_to_zero_denied if {
	d := decision_for(with_fields({
		"environment": "production",
		"operation": "kubectl_scale",
		"target": {"kind": "deployment", "name": "payment-api", "namespace": "prod"},
		"replicas": 0,
	}))
	d.allow == false
	rules_of(d) == {"PROHIBIT_PRODUCTION_SCALE_TO_ZERO"}
}

test_u05_production_scale_to_nonzero_allowed if {
	every n in [1, 100] {
		d := decision_for(with_fields({
			"environment": "production",
			"operation": "kubectl_scale",
			"target": {"kind": "deployment", "name": "payment-api", "namespace": "prod"},
			"replicas": n,
		}))
		d.allow == true
	}
}

test_u05_staging_scale_to_zero_allowed if {
	d := decision_for(with_fields({
		"environment": "staging",
		"operation": "kubectl_scale",
		"target": {"kind": "deployment", "name": "batch-worker", "namespace": "staging"},
		"replicas": 0,
	}))
	d.allow == true
}

# ── U-06 保護 namespace は参照系のみ許可される ─────────────

test_u06_protected_namespace_read_only_allowed if {
	every ns in data.catalog.protected_namespaces {
		d := decision_for(with_fields({
			"environment": "production",
			"operation": "kubectl_logs",
			"target": target_in(ns),
		}))
		d.allow == true
		count(d.violated_rules) == 0
	}
}

test_u06_protected_namespace_mutating_denied if {
	d := decision_for(with_fields({
		"environment": "production",
		"operation": "kubectl_rollout_restart",
		"target": {"kind": "deployment", "name": "coredns", "namespace": "kube-system"},
	}))
	d.allow == false
	"PROHIBIT_PROTECTED_NAMESPACE" in rules_of(d)
}

# ── U-07 environment と namespace の食い違いを検知する ─────

test_u07_namespace_environment_mismatch_denied if {
	d := decision_for(with_fields({
		"environment": "staging",
		"operation": "kubectl_rollout_restart",
		"target": {"kind": "deployment", "name": "payment-api", "namespace": "prod"},
	}))
	d.allow == false
	rules_of(d) == {"REQUIRE_NAMESPACE_ENVIRONMENT_MATCH"}
}

test_u07_namespace_environment_match_allowed if {
	d := decision_for(with_fields({
		"environment": "staging",
		"operation": "kubectl_rollout_restart",
		"target": {"kind": "deployment", "name": "payment-api", "namespace": "staging"},
	}))
	d.allow == true
}

# ── U-08 複数ルールへの同時抵触 ───────────────────────────

# Phase 2 の N-01 と同じ入力（本番 PVC 削除 / dry_run: false）。
# 第2層を通過したアクションがここで止まることの、ポリシー単体での確認。
test_u08_multiple_violations_reported if {
	d := decision_for(with_fields({
		"environment": "production",
		"operation": "kubectl_delete_pvc",
		"target": {"kind": "pvc", "name": "payment-db-data", "namespace": "prod"},
		"dry_run": false,
	}))
	d.allow == false
	rules_of(d) == {"PROHIBIT_PVC_DELETE", "PROHIBIT_PRODUCTION_DESTRUCTIVE"}
}

# 拒否には必ず理由が付く（設計原則3）。空文字列の message を許さない。
test_u08_every_violation_has_a_message if {
	d := decision_for(with_fields({
		"environment": "production",
		"operation": "kubectl_delete_pvc",
		"target": {"kind": "pvc", "name": "payment-db-data", "namespace": "kube-system"},
		"dry_run": false,
	}))
	count(d.violated_rules) >= 2
	every v in d.violated_rules {
		count(v.rule) > 0
		count(v.message) > 0
	}
}

# violated_rules の順序が安定している（D-01 決定性の前提）。
test_u08_violated_rules_are_sorted if {
	d := decision_for(with_fields({
		"environment": "production",
		"operation": "kubectl_delete_pvc",
		"target": {"kind": "pvc", "name": "payment-db-data", "namespace": "kube-system"},
		"dry_run": false,
	}))
	d.violated_rules == sort(d.violated_rules)
}

# ── 責務境界: 第3層は構造を保証しない（design.md 1. 証明すること 5 / N-02）──

# 第2層をバイパスした不完全な入力は、ルールが undefined で発火せず allow になる。
# ここで deny になる場合、Rego 側に構造検証を書いてしまっている（D-002 の逆方向の違反）。
test_n02_incomplete_input_is_allowed_by_design if {
	d := agent.decision with input as {"action": {"operation": "kubectl_delete_pvc"}, "trace_id": "t"}
	d.allow == true
}
