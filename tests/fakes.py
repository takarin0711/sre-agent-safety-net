"""テスト用のフェイク。**実ワークスペースにも実 Slack API にも触れない。**

Phase 4 の判定基準1 は「ワークスペース無しで決定的に検証できること」なので、
送出先をここで閉じる。WebClient を継承しているのは、Bolt に渡したときに
型として受け付けられることを保証するため。
"""

from __future__ import annotations

import json
import time
import urllib.parse

from slack_bolt.request import BoltRequest
from slack_sdk import WebClient
from slack_sdk.signature import SignatureVerifier
from slack_sdk.web import SlackResponse

from slack.blocks import ACTION_ID_APPROVE


class FakeSlack(WebClient):
    """呼び出しを記録するだけの WebClient。ネットワークには出ない。"""

    def __init__(self) -> None:
        super().__init__(token="xoxb-fake-token")
        self.calls: list[tuple[str, dict]] = []

    def _response(self, data: dict) -> SlackResponse:
        """Slack SDK と同じ形で返す。Bolt は ``.headers`` まで参照する。"""
        return SlackResponse(
            client=self,
            http_verb="POST",
            api_url="https://slack.com/api/fake",
            req_args={},
            data=data,
            headers={"x-oauth-scopes": "chat:write"},
            status_code=200,
        )

    def auth_test(self, **kwargs):  # type: ignore[override]
        """Bolt の認可ミドルウェアが毎リクエストで呼ぶ。

        これを塞がないと、**テストが実 Slack API に出ていく**（実際に出ていた）。
        token_verification_enabled=False はアプリ起動時の検証を切るだけで、
        リクエストごとの auth.test は別経路である。
        """
        self.calls.append(("auth_test", kwargs))
        return self._response(
            {
                "ok": True,
                "url": "https://example.slack.com/",
                "team": "poc",
                "user": "safety-net-bot",
                "team_id": "T-TEST",
                "user_id": "U-BOT",
                "bot_id": "B-TEST",
            }
        )

    def conversations_open(self, **kwargs):  # type: ignore[override]
        """ユーザ ID から DM のチャンネル ID を開く（検証用チャンネルを作らない経路）。"""
        self.calls.append(("conversations_open", kwargs))
        return self._response({"ok": True, "channel": {"id": "D-TEST"}})

    def chat_postMessage(self, **kwargs):  # type: ignore[override]
        self.calls.append(("chat_postMessage", kwargs))
        return self._response(
            {"ok": True, "channel": kwargs.get("channel", "C-TEST"), "ts": "1700000000.000100"}
        )

    def chat_update(self, **kwargs):  # type: ignore[override]
        self.calls.append(("chat_update", kwargs))
        return self._response({"ok": True})

    def chat_postEphemeral(self, **kwargs):  # type: ignore[override]
        self.calls.append(("chat_postEphemeral", kwargs))
        return self._response({"ok": True})

    def methods(self) -> list[str]:
        """呼ばれた API の一覧。auth_test は Bolt の内部処理なので除く。"""
        return [name for name, _ in self.calls if name != "auth_test"]

    @property
    def api_calls(self) -> list[tuple[str, dict]]:
        return [call for call in self.calls if call[0] != "auth_test"]

    def last(self, method: str) -> dict:
        for name, kwargs in reversed(self.calls):
            if name == method:
                return kwargs
        raise AssertionError(f"{method} は呼ばれていない（呼ばれたのは {self.methods()}）")


def block_actions_payload(
    trace_id: str, *, user_id: str = "U-APPROVER", action_id: str = ACTION_ID_APPROVE
) -> dict:
    """Slack が送ってくる block_actions のペイロード（必要な部分だけ）。

    **value に入っているのは Trace ID だけ**である（運用原則2 / D-020）。
    """
    return {
        "type": "block_actions",
        "user": {"id": user_id},
        "channel": {"id": "C-TEST"},
        "message": {"ts": "1700000000.000100"},
        "actions": [{"action_id": action_id, "value": trace_id, "type": "button"}],
    }


def signed_request(
    payload: dict,
    *,
    secret: str,
    timestamp: int | None = None,
    tamper_with: dict | None = None,
    retry_num: int | None = None,
) -> BoltRequest:
    """Slack から来たのと同じ形のリクエストを組み立てる。

    ``tamper_with`` を渡すと、**署名を計算したあとに本文を差し替える**（G-03）。
    これが改変ペイロードの再現であり、署名検証が無ければ通ってしまうもの。
    """
    ts = str(timestamp if timestamp is not None else int(time.time()))
    body = "payload=" + urllib.parse.quote(json.dumps(payload))
    signature = SignatureVerifier(secret).generate_signature(timestamp=ts, body=body)
    if tamper_with is not None:
        body = "payload=" + urllib.parse.quote(json.dumps(tamper_with))
    headers = {
        "content-type": ["application/x-www-form-urlencoded"],
        "x-slack-request-timestamp": [ts],
        "x-slack-signature": [signature],
    }
    if retry_num is not None:
        headers["x-slack-retry-num"] = [str(retry_num)]
        headers["x-slack-retry-reason"] = ["http_timeout"]
    return BoltRequest(body=body, headers=headers)


def unsigned_request(payload: dict) -> BoltRequest:
    """署名ヘッダが無いリクエスト（G-02）。"""
    body = "payload=" + urllib.parse.quote(json.dumps(payload))
    return BoltRequest(body=body, headers={"content-type": ["application/x-www-form-urlencoded"]})


# ── Phase 5: 実行層のフェイク ──────────────────────────────
#
# **実クラスタにも kind にも触れない。** 判定基準1（単体はクラスタ無しで決定的に回ること）
# を Phase 4 と同じ形で守る。実機の確認は K 系より後の R / Z / X 系で行う。


class _Obj:
    """属性アクセスだけできる軽量なスタブ（API オブジェクトの代用）。"""

    def __init__(self, **kwargs: object) -> None:
        self.__dict__.update(kwargs)


class FakeK8sError(Exception):
    """FakeApis に仕込む例外のマーカー（テストが意図した経路を通ったことの確認用）。"""


class _FakeApi:
    """呼び出しを記録し、仕込まれた例外を投げるだけの API クライアント。"""

    def __init__(self, recorder: list, raises: Exception | None = None) -> None:
        self._recorder = recorder
        self._raises = raises

    def _record(self, method: str, args: tuple, kwargs: dict) -> None:
        self._recorder.append((method, args, kwargs))
        if self._raises is not None:
            raise self._raises

    # 参照系
    def read_namespaced_pod(self, name, namespace, **kw):
        self._record("read_namespaced_pod", (name, namespace), kw)
        return _Obj(status=_Obj(phase="Running"), metadata=_Obj(uid="uid-fake"))

    def read_namespaced_persistent_volume_claim(self, name, namespace, **kw):
        self._record("read_namespaced_persistent_volume_claim", (name, namespace), kw)
        return _Obj(status=_Obj(phase="Bound"))

    def read_node(self, name, **kw):
        self._record("read_node", (name,), kw)
        return _Obj(metadata=_Obj(name=name))

    def read_namespaced_pod_log(self, name, namespace, **kw):
        self._record("read_namespaced_pod_log", (name, namespace), kw)
        return "line-1\nline-2\n"

    def list_namespaced_event(self, namespace, **kw):
        self._record("list_namespaced_event", (namespace,), kw)
        return _Obj(items=[])

    def read_namespaced_deployment(self, name, namespace, **kw):
        self._record("read_namespaced_deployment", (name, namespace), kw)
        return _Obj(metadata=_Obj(generation=1), spec=_Obj(replicas=1))

    def read_namespaced_stateful_set(self, name, namespace, **kw):
        self._record("read_namespaced_stateful_set", (name, namespace), kw)
        return _Obj(metadata=_Obj(generation=1), spec=_Obj(replicas=1))

    # 変更系
    def patch_namespaced_deployment(self, name, namespace, body, **kw):
        self._record("patch_namespaced_deployment", (name, namespace, body), kw)
        return _Obj(metadata=_Obj(generation=2))

    def patch_namespaced_stateful_set(self, name, namespace, body, **kw):
        self._record("patch_namespaced_stateful_set", (name, namespace, body), kw)
        return _Obj(metadata=_Obj(generation=2))

    def patch_namespaced_deployment_scale(self, name, namespace, body, **kw):
        self._record("patch_namespaced_deployment_scale", (name, namespace, body), kw)
        return _Obj(spec=_Obj(replicas=body["spec"]["replicas"]))

    def patch_namespaced_stateful_set_scale(self, name, namespace, body, **kw):
        self._record("patch_namespaced_stateful_set_scale", (name, namespace, body), kw)
        return _Obj(spec=_Obj(replicas=body["spec"]["replicas"]))

    # 破壊系
    def delete_namespaced_pod(self, name, namespace, **kw):
        self._record("delete_namespaced_pod", (name, namespace), kw)

    def delete_namespaced_persistent_volume_claim(self, name, namespace, **kw):
        self._record("delete_namespaced_persistent_volume_claim", (name, namespace), kw)


class FakeApis:
    """``agents.k8s.Apis`` と同じ形。``raises`` を渡すと全呼び出しがその例外を投げる。"""

    def __init__(self, raises: Exception | None = None) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.core = _FakeApi(self.calls, raises)
        self.apps = _FakeApi(self.calls, raises)

    @property
    def methods(self) -> list[str]:
        return [name for name, _, _ in self.calls]

    def last(self) -> tuple[str, tuple, dict]:
        if not self.calls:
            raise AssertionError("API は 1 度も呼ばれていない")
        return self.calls[-1]
