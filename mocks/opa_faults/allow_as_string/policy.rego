# X-05: allow が真偽値ではなく文字列。Python 側が `if decision["allow"]:` のような
# 素朴な読み方をしていると、"false" という文字列が真と評価されて実行に進んでしまう。
package sre.agent

import rego.v1

decision := {"allow": "false", "violated_rules": []}
