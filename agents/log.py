"""構造化ログ出力。

各層の判定結果（フェイルオーバー発火・ValidationError・allow の値と理由）を
後から追えるよう、1 行 1 JSON で標準出力に書く。検証のエビデンスそのものになるため、
フォーマットは Phase をまたいで変えない。
"""

from __future__ import annotations

import json


def log(**fields: object) -> None:
    """構造化ログを 1 行 1 JSON で標準出力に書く（検証のエビデンスになる）。"""
    print(json.dumps(fields, ensure_ascii=False, default=str), flush=True)
