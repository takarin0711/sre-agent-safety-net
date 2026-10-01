"""秘匿情報のマスキング（Handoff 仕様書 運用原則1）。

LLM が生成する ``reason`` や、第1層から返ってきた ``raw_output`` には、
接続文字列・トークン・クレデンシャルが混入しうる。Slack は社内とはいえ
チャンネル参加者全員が読める場所なので、**送出前に必ずここを通す**
（design.md 3.8）。

純関数として実装する。Slack にもストアにも触らない。理由は 2 つ:

- pytest で決定的に固められる（S-06）
- **マスキングをストアに適用しない**ことを構造的に保証できる。
  監査の正本は原文であるべきで、隠すのは送出先だけである（D-020 の影響欄）
"""

from __future__ import annotations

import re

# マスク対象。検知できるものだけを並べる。網羅は目的ではない（PoC の非目的）。
# 「何をマスクしているか」が後から読めることを優先し、1 パターン 1 目的にする。
_RULES: list[tuple[re.Pattern[str], str]] = [
    # AWS のアクセスキー ID（AKIA / ASIA で始まる 20 文字）
    (re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])"), "***AWS_ACCESS_KEY_ID***"),
    # Slack のトークン（Bot / User / App / Refresh / Legacy）
    (re.compile(r"xox[baprse]-[A-Za-z0-9-]{10,}"), "***SLACK_TOKEN***"),
    # Authorization: Bearer <token>
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-+/=]{8,}"), "Bearer ***"),
    # key=value 形式のシークレット（AWS のシークレットキー・セッショントークン・汎用の password）
    (
        re.compile(
            r"(?i)\b(aws_secret_access_key|aws_session_token|secret_access_key|session_token"
            r"|password|passwd|api[_-]?key|signing[_-]?secret)\b(\s*[=:]\s*)(\"?)([^\s\"',;]{4,})",
        ),
        r"\1\2\3***",
    ),
    # URL に埋め込まれた認証情報（scheme://user:password@host）
    # パスワードに @ が含まれうるため、ホストの直前（最後の @）まで貪欲に取る
    (re.compile(r"\b([a-z][a-z0-9+.\-]*://)([^/\s:@]*):([^\s/]+)@"), r"\1\2:***@"),
]


def mask(text: str) -> str:
    """文字列を秘匿情報マスキング済みにする。入力が壊れていても例外を出さない。"""
    masked = text
    for pattern, replacement in _RULES:
        masked = pattern.sub(replacement, masked)
    return masked


def mask_value(value: object) -> object:
    """dict / list / str を再帰的にマスクする。

    Block Kit は入れ子の dict なので、組み上げた後に全体を通せるようにしておく。
    文字列以外（bool / int / None）はそのまま返す。
    """
    if isinstance(value, str):
        return mask(value)
    if isinstance(value, dict):
        return {k: mask_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_value(v) for v in value]
    return value
