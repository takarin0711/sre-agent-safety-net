"""エスカレーション層（Slack Handoff）。

送出（notifier）はエージェントと同一プロセスのライブラリ、受信（handler）は独立プロセス。
この非対称は docs/decisions.md D-019 に記録している。

**注意**: このディレクトリは ``slack_sdk`` が互換用に同梱している ``slack`` パッケージを
シャドーイングする（``litellm/`` と同じ構造）。``slack_bolt`` / ``slack_sdk`` は
どちらも ``slack`` を import しないため実害は無いが、レガシーな
``from slack import WebClient`` は本リポジトリのルートからは解決できない。
"""
