# X-05: allow: true なのに違反が列挙されている自己矛盾。ポリシー側のバグを模す。
# どちらを信じるかを呼び出し側が決められないため、評価不能として扱う（D-017）。
package sre.agent

import rego.v1

decision := {
	"allow": true,
	"violated_rules": [{
		"rule": "PROHIBIT_PVC_DELETE",
		"message": "PVC の削除は不可逆なため自動実行を許可しません",
	}],
}
