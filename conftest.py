"""リポジトリルートを sys.path に載せ、テストを外部へ出さないための conftest。

pytest は conftest.py のあるディレクトリを sys.path に追加するので、
tests/ から `agents` / `slack` パッケージを import できる。

加えて Phase 4 から、**すべてのテストでエスカレーション層を隔離する**。
開発者のシェルに SLACK_BOT_TOKEN が export されていると、Phase 2・3 のテストが
（第2層・第3層の失敗を検知するたびに）実ワークスペースへ投稿してしまうため。
テストが副作用で外部サービスを叩く構造は、PoC の検証を汚染する。
"""

from __future__ import annotations

import pytest

from agents import escalation

# 送出先・認可・閾値。テストごとに既定へ戻す（環境変数の漏れを遮断する）。
_SLACK_ENV = (
    "SLACK_BOT_TOKEN",
    "SLACK_SIGNING_SECRET",
    "SLACK_CHANNEL_ID",
    "SLACK_API_BASE_URL",
    "SLACK_APPROVER_USER_IDS",
    "SLACK_APPROVAL_TTL_SECONDS",
    "SLACK_TOCTOU_SECONDS",
    "SLACK_TIMEOUT_SECONDS",
    # Phase 5。**テストが実クラスタに触れないようにする。** 開発者のシェルに
    # K8S_KUBECONFIG が残っていると、単体テストが kind へ実際に API を発行しうる。
    "K8S_KUBECONFIG",
    "K8S_CONTEXT",
    "EXECUTION_TIMEOUT_SECONDS",
    "EXECUTION_MAX_RETRIES",
    "EXECUTOR_POLL_SECONDS",
)


@pytest.fixture(autouse=True)
def isolate_escalation(tmp_path, monkeypatch):
    """Slack への送出を無効化し、ストアをテストごとの一時ファイルに閉じる。"""
    for key in _SLACK_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SLACK_STORE_PATH", str(tmp_path / "handoff.sqlite3"))
    escalation.reset()
    yield
    escalation.reset()
