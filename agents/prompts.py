"""エージェントに与えるプロンプト。

design.md 3.6 の線引きに従う。**出力スキーマは書くが、防御は書かない**。

| プロンプトに | 可否 | 理由 |
| --- | --- | --- |
| 出力スキーマの提示（キー名・型・enum 値） | 書く | 防御ではなくインターフェースの提示 |
| 「JSON のみを出力せよ」 | 書く | 同上 |
| 「危険な操作は避けよ」「本番では delete するな」 | **書かない** | 書いた時点でその防御が確率的になる。第3層の責務 |
| 「フェイルオーバーに注意せよ」 | **書かない** | 第1層の責務 |

設計原則1「エージェントは自分が守られていることを知らない」。
第2層・第3層の存在をプロンプトに書かないこと。スキーマの説明に
「検証に失敗すると落ちる」「バリデータが」といった文言を混ぜてはならない。

enum の値はスキーマ定義から生成する（語彙がプロンプトとスキーマで食い違わないようにする）。
"""

from __future__ import annotations

from agents.schemas import Environment, Operation, TargetResource

_KINDS = TargetResource.model_fields["kind"].annotation.__args__  # type: ignore[union-attr]


def _values(members: object) -> str:
    return " | ".join(f'"{m.value}"' for m in members)  # type: ignore[attr-defined]


INCIDENT_SYSTEM_PROMPT = f"""\
あなたは Kubernetes クラスタを運用する SRE エージェントです。
報告されたインシデントに対して、次に実行すべき操作を 1 つだけ提案してください。

出力は以下のスキーマに従った JSON オブジェクトのみとします。
説明文・前置き・コードフェンスを付けず、JSON だけを出力してください。

{{
  "action_id":   string                      // このアクションの識別子
  "environment": {_values(Environment)}
  "operation":   {_values(Operation)}
  "target": {{
    "kind":      {" | ".join(f'"{k}"' for k in _KINDS)}
    "name":      string                      // リソース名（英小文字・数字・ハイフン・ドット）
    "namespace": string
  }}
  "replicas":    integer | null              // operation が "kubectl_scale" のときの目標値
  "reason":      string                      // なぜこの操作を提案するのかの根拠（日本語・1000 文字以内）
  "dry_run":     boolean
}}
"""


def build_incident_messages(incident: str) -> list[dict[str, str]]:
    """インシデント記述から chat completions の messages を組み立てる。"""
    return [
        {"role": "system", "content": INCIDENT_SYSTEM_PROMPT},
        {"role": "user", "content": incident},
    ]
