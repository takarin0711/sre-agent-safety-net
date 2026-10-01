"""実行層。PoC では構造化ログを出すだけで、実 kubectl は発行しない。

``agents/main.py`` から切り出した。承認経路（``slack/handler.py``）からも
**同じ関数**を呼ぶためである（design.md 3.1）。エージェント本体が実行するときと、
人間が承認して実行するときで「実行」の意味が変わってはいけない。

**ログの形式は Phase 3 から 1 バイトも変えない。** Phase 3 の assets と比較できなくなると、
過去の判定が再現できなくなる。承認経路に固有の情報は handler 側が別の行として出す。
"""

from __future__ import annotations

from agents.log import log
from agents.schemas import ProposedAction


def execute_dry_run(action: ProposedAction, trace_id: str) -> None:
    """実行層。PoC では構造化ログを出すだけで、実 kubectl は発行しない。

    アクションの dry_run が false でも発行しない（CLAUDE.md）。この層を置く目的は
    「allow: true が実行に繋がっていること」を示すことであり、実行そのものの検証ではない。
    したがって allow: false / 評価不能のときにこのログが**出ていないこと**が、
    拒否と Fail-Closed の実効性のエビデンスになる（判定基準4）。
    """
    log(
        event="execution.dry_run",
        trace_id=trace_id,
        action=action.model_dump(mode="json"),
        note="PoC のため実際のコマンドは発行しない",
    )
