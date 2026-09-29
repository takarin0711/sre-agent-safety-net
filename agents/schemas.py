"""層をまたぐデータ構造の定義（第2層 JSON Validator のスキーマ）。

第2層は「構造が仕様どおりであること」だけを保証する。
「その操作を実行してよいか」は第3層 (OPA) の責務であり、ここには書かない
（docs/decisions.md D-002）。したがって本ファイルに
「本番では delete を禁止する」といった判定を足してはならない。

設計上の要点（docs/phases/phase2-pydantic/design.md 3.1〜3.3）:

- 全モデルに ``strict=True`` を付ける。Pydantic v2 の既定 (lax) は ``"3"`` → ``3``、
  ``"yes"`` → ``True`` を黙って強制変換するため、外すと型不一致 (V-05) が
  検証を通過してしまい、第2層が何を保証しているのか言えなくなる。
- 全モデルに ``extra="forbid"`` を付ける。スキーマに無いキーを黙って捨てると、
  第3層が見ないフィールドに指示が入る余地が残る。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

# 全モデル共通。strict / extra のどちらを外しても判定基準1 が崩れるため、
# 個別に書かずここに集約して逸脱を起こしにくくする。
LAYER2_MODEL_CONFIG = ConfigDict(extra="forbid", strict=True)


class Environment(StrEnum):
    """操作対象の環境。"""

    PRODUCTION = "production"
    STAGING = "staging"
    DEVELOPMENT = "development"


class Operation(StrEnum):
    """エージェントが提案しうる操作の語彙（allow-list）。

    ここに無い操作は構造検証の時点で落ちる。自由文字列にすると
    第3層が deny-list を持つことになって漏れる（design.md 未決事項6）。
    """

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
    """操作対象のリソース。"""

    model_config = LAYER2_MODEL_CONFIG

    kind: Literal["pod", "deployment", "statefulset", "pvc", "node"]
    # RFC 1123 のサブセット。Kubernetes のリソース名として成立しない文字列を弾く。
    name: str = Field(min_length=1, max_length=253, pattern=r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
    namespace: str = Field(min_length=1, max_length=63)


class ProposedAction(BaseModel):
    """エージェントが実行しようとする 1 アクション。第2層 → 第3層のインターフェース。"""

    model_config = LAYER2_MODEL_CONFIG

    action_id: str
    environment: Environment
    operation: Operation
    target: TargetResource
    replicas: int | None = Field(default=None, ge=0, le=100)
    reason: str = Field(min_length=1, max_length=1000)  # 人間が読む根拠（設計原則3）
    dry_run: bool = True

    @model_validator(mode="after")
    def _replicas_required_for_scale(self) -> ProposedAction:
        """「scale なのに目標値が無い」は構造の不完全性であり、構造検証の責務。

        フィールド間の相互依存はスキーマ強制（response_format / tool use）では
        原理的に守れない領域であり、第2層が必要である根拠のひとつになる（D-011）。
        """
        if self.operation is Operation.SCALE and self.replicas is None:
            raise PydanticCustomError(
                "replicas_required_for_scale",
                "operation={operation} のとき replicas は必須です",
                {"operation": self.operation.value, "field": "replicas"},
            )
        return self


class EscalationEvent(BaseModel):
    """層の失敗を人間に引き渡すためのイベント（architecture.md 失敗時の共通フロー 2）。

    「どの層で」「何が」「元のリクエストは何だったか」を含む。
    Phase 4 で Block Kit に載せる際に不足があれば、そのとき追加する（未決事項3）。
    """

    model_config = LAYER2_MODEL_CONFIG

    layer: Literal["layer1_litellm", "layer2_json_validator", "layer3_opa"]
    reason: str  # 人間が読んで判断できる文言
    errors: list[dict] = Field(default_factory=list)  # ValidationError の内容（JSON 化可能な形）
    raw_output: str | None = None  # LLM の生出力（何が来たのかを人間が見る）
    original_request: str  # 元のインシデント記述
    occurred_at: datetime
