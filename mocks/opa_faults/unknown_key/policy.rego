# X-05: decision に未知のキーが増えている。将来の「条件つき許可」のような拡張を
# 黙って無視すると見落とすため、extra="forbid" で検知する（D-017）。
package sre.agent

import rego.v1

decision := {
	"allow": false,
	"violated_rules": [{"rule": "PROHIBIT_PVC_DELETE", "message": "理由"}],
	"requires_approval_from": "sre-oncall",
}
