"""V 系 fixture ごとの ValidationError.errors() を JSON で書き出す（エビデンス生成用）。

判定基準1 は「落ちたこと」ではなく「**なぜ**落ちたかが特定できること」なので、
pytest の pass/fail だけでは足りない。errors() の生の内容を残す。

D-01（決定性）では本スクリプトを別プロセスで 10 回実行し、出力のハッシュを比較する。

    python -m tests.dump_validation_errors <出力先.json>

errors() の JSON は引数のファイルへ、validate_action() 自身の構造化ログは標準出力へ出る
（エスカレーションイベントもエビデンスなので、標準出力も別ファイルに残すとよい）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic_core import ValidationError

from agents.validation import validate_action
from tests.test_layer2_validation import INVALID_CASES


def main(out_path: str) -> int:
    dumped: list[dict] = []
    for scenario, content, _expected in INVALID_CASES:
        try:
            validate_action(content, original_request="エビデンス生成")
        except ValidationError as exc:
            dumped.append({"scenario": scenario, "errors": json.loads(exc.json(include_url=False))})
        else:  # pragma: no cover - ここに来たら判定基準1 が崩れている
            dumped.append({"scenario": scenario, "errors": None, "note": "検証を通過してしまった"})

    Path(out_path).write_text(json.dumps(dumped, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"usage: python -m {__spec__.name} <出力先.json>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
