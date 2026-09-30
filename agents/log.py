"""構造化ログ出力。

各層の判定結果（フェイルオーバー発火・ValidationError・allow の値と理由）を
後から追えるよう、1 行 1 JSON で標準出力に書く。検証のエビデンスそのものになるため、
フォーマットは Phase をまたいで変えない。
"""

from __future__ import annotations

import json

from pydantic_core import ValidationError


def log(**fields: object) -> None:
    """構造化ログを 1 行 1 JSON で標準出力に書く（検証のエビデンスになる）。"""
    print(json.dumps(fields, ensure_ascii=False, default=str), flush=True)


def json_safe_errors(exc: ValidationError) -> list[dict]:
    """``ValidationError.errors()`` を JSON 化可能な形で取り出す。

    ``errors()`` の ``loc`` はタプル、``ctx`` には任意のオブジェクトが入りうるため、
    Pydantic 自身のシリアライザ（``.json()``）を経由させる。
    第2層（LLM 出力の検証）と第3層（OPA 応答の検証）の両方で使う。
    """
    return json.loads(exc.json(include_url=False))
