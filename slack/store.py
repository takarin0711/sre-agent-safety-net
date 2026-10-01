"""承認待ちアクションのステートストア（Handoff 仕様書 運用原則4 / design.md 3.5）。

SQLite を使う。コンテナを増やさず、標準ライブラリだけで
排他・永続・TTL・冪等・追跡・監査を同一ファイルに置けるため。
**「分散」ロックではない**という限界は隠さず、提言に書く。

排他は「ロックを取ってから更新する」のではなく、**状態遷移そのものを条件付き更新で行う**
（``UPDATE ... WHERE status = 'pending'`` の更新件数が 1 のときだけ先に進む）。
ロックの取得と遷移が 1 回の操作で完結するため、ロックの解放漏れや
TTL 満了中の実行といった事故が原理的に起きない。

このモジュールは Slack にも agents にも依存しない（``EscalationEvent`` の JSON を
文字列として預かるだけ）。ストアだけを単体で検証できるようにするため。
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

DEFAULT_STORE_PATH = "var/handoff.sqlite3"
DEFAULT_TTL_SECONDS = 15 * 60  # 承認期限（運用原則4）。承認者の稼働時間帯で調整すべき値
DEFAULT_TOCTOU_SECONDS = 5 * 60  # 提案から実行までの許容時間（付帯要件 TOCTOU）


class HandoffStatus(StrEnum):
    """ステートの語彙（design.md 3.5.1）。

    **Phase 4 で到達しないものも含めて定義する。** 後から足すと既存レコードの意味が変わる。
    とくに ``EXECUTION_FAILED``（確定的な失敗・再試行可）と ``UNKNOWN``（実行有無が不明・
    再試行不可）を分けておくことが重要で、タイムアウトした実行を一律に「失敗」として
    再試行可能にすると、非冪等な操作で二重実行を招く。
    """

    PENDING = "pending"
    APPROVED = "approved"
    EXECUTED = "executed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    STALE = "stale"
    NOTIFY_FAILED = "notify_failed"
    EXECUTION_FAILED = "execution_failed"
    UNKNOWN = "unknown"


# 終端ステート。ここに達したら監査基盤へ移送してよい（PoC では移送しない）。
TERMINAL_STATUSES = frozenset(
    {
        HandoffStatus.EXECUTED,
        HandoffStatus.REJECTED,
        HandoffStatus.EXPIRED,
        HandoffStatus.STALE,
        HandoffStatus.EXECUTION_FAILED,
        HandoffStatus.UNKNOWN,
    }
)


class StateConflict(RuntimeError):
    """期待したステートではなかった（二重押下・期限切れ・処理済み）。

    これは異常ではなく、**排他が効いたこと**を表す。Slack の再送でも同じ経路を通る（D-024）。
    """

    def __init__(self, trace_id: str, expected: HandoffStatus, actual: str | None) -> None:
        super().__init__(f"{trace_id}: expected={expected.value} actual={actual}")
        self.trace_id = trace_id
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True)
class PendingAction:
    """承認待ちレコード 1 件。"""

    trace_id: str
    status: str
    kind: str
    event_json: str
    channel: str | None
    message_ts: str | None
    created_at: datetime
    expires_at: datetime
    approved_by: str | None
    approved_at: datetime | None
    executed_at: datetime | None
    execution_result: str | None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_actions (
  trace_id         TEXT PRIMARY KEY,
  status           TEXT NOT NULL,
  kind             TEXT NOT NULL,
  event_json       TEXT NOT NULL,
  channel          TEXT,
  message_ts       TEXT,
  created_at       TEXT NOT NULL,
  expires_at       TEXT NOT NULL,
  approved_by      TEXT,
  approved_at      TEXT,
  executed_at      TEXT,
  execution_result TEXT
);
CREATE INDEX IF NOT EXISTS idx_pending_status_expires
  ON pending_actions (status, expires_at);

CREATE TABLE IF NOT EXISTS audit_log (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  trace_id      TEXT NOT NULL,
  occurred_at   TEXT NOT NULL,
  actor         TEXT,
  action        TEXT NOT NULL,
  override_kind TEXT,
  detail        TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_trace ON audit_log (trace_id);
"""


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def now() -> datetime:
    return datetime.now(UTC)


class Store:
    """Trace ID をキーにした承認ステートストア。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or os.environ.get("SLACK_STORE_PATH") or DEFAULT_STORE_PATH)
        if self.path.parent != Path(""):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # isolation_level=None で自動コミットを切り、BEGIN IMMEDIATE を自分で打つ。
        # timeout は他プロセス／他スレッドのロック待ち（T-01 の同時押下で効く）。
        conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    # ── 書き込み ──────────────────────────────────────────

    def create_pending(
        self, *, trace_id: str, kind: str, event_json: str, ttl_seconds: int | None = None
    ) -> PendingAction:
        """承認待ちを作る。**Slack へ送る前に呼ぶ**（design.md 3.5 の順序）。

        逆順にすると「通知はあるがストアに無い」＝押しても何も起きない状態が作れる。
        """
        ttl = ttl_seconds if ttl_seconds is not None else _ttl_from_env()
        created = now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO pending_actions "
                "(trace_id, status, kind, event_json, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    trace_id,
                    HandoffStatus.PENDING.value,
                    kind,
                    event_json,
                    _iso(created),
                    _iso(created + timedelta(seconds=ttl)),
                ),
            )
            conn.execute("COMMIT")
        self.audit(trace_id, action="created", detail=f"ttl={ttl}s")
        row = self.get(trace_id)
        if row is None:  # pragma: no cover - INSERT 直後に消えることはない
            raise RuntimeError(f"{trace_id}: 作成直後のレコードが見つかりません")
        return row

    def attach_message(self, trace_id: str, *, channel: str, message_ts: str) -> None:
        """送出に成功した Slack メッセージを結びつける（chat.update の宛先）。"""
        self._update(trace_id, "SET channel = ?, message_ts = ?", (channel, message_ts))

    def mark_notify_failed(self, trace_id: str, detail: str) -> None:
        """**通知の送出**に失敗した。実行の失敗とは別のステートである（3.5.1）。"""
        self._update(trace_id, "SET status = ?", (HandoffStatus.NOTIFY_FAILED.value,))
        self.audit(trace_id, action="notify_failed", detail=detail)

    def approve(self, trace_id: str, *, actor: str) -> PendingAction:
        """pending -> approved の条件付き更新。失敗は StateConflict。

        **承認期限の判定も同じ 1 文に含める。** 満了ジョブが掃くのを待ってから弾くと、
        「期限は過ぎているがまだ pending」の窓で承認が通ってしまう。
        期限切れは満了ジョブの都合であって、承認可否の条件ではない。
        """
        return self._transition(
            trace_id,
            expected=HandoffStatus.PENDING,
            new=HandoffStatus.APPROVED,
            extra="approved_by = ?, approved_at = ?",
            params=(actor, _iso(now())),
            guard_expiry=True,
        )

    def reject(self, trace_id: str, *, actor: str) -> PendingAction:
        """pending -> rejected。却下も必ずステートを閉じる（UI 更新だけでは pending が残る）。"""
        return self._transition(
            trace_id,
            expected=HandoffStatus.PENDING,
            new=HandoffStatus.REJECTED,
            extra="approved_by = ?, approved_at = ?",
            params=(actor, _iso(now())),
        )

    def expire(self, trace_id: str) -> PendingAction:
        """pending -> expired（満了ジョブから呼ぶ）。"""
        return self._transition(trace_id, expected=HandoffStatus.PENDING, new=HandoffStatus.EXPIRED)

    def mark_stale(self, trace_id: str, detail: str, *, actor: str | None = None) -> PendingAction:
        """approved -> stale。TOCTOU 閾値を超えたため実行しない。"""
        row = self._transition(
            trace_id,
            expected=HandoffStatus.APPROVED,
            new=HandoffStatus.STALE,
            extra="execution_result = ?",
            params=(detail,),
        )
        self.audit(trace_id, action="stale", actor=actor, detail=detail)
        return row

    def mark_executed(self, trace_id: str, result: str) -> PendingAction:
        """approved -> executed。**成功時にのみ** executed_at を立てる（冪等性の要）。"""
        return self._transition(
            trace_id,
            expected=HandoffStatus.APPROVED,
            new=HandoffStatus.EXECUTED,
            extra="executed_at = ?, execution_result = ?",
            params=(_iso(now()), result),
        )

    def mark_execution_failed(self, trace_id: str, detail: str) -> PendingAction:
        """approved -> execution_failed。**実行を試みて確定的に失敗した**場合のみ。

        実行されたかどうかが分からない場合はここではなく UNKNOWN に倒す（3.5.1）。
        """
        return self._transition(
            trace_id,
            expected=HandoffStatus.APPROVED,
            new=HandoffStatus.EXECUTION_FAILED,
            extra="executed_at = ?, execution_result = ?",
            params=(_iso(now()), detail),
        )

    def audit(
        self,
        trace_id: str,
        *,
        action: str,
        actor: str | None = None,
        override_kind: str | None = None,
        detail: str | None = None,
    ) -> None:
        """監査記録（追記のみ）。

        ``policy_override``（OPA の deny を人間が承認した事実）は
        **ここで記録しなければどこにも残らない**（OPA の Decision Log には出ない）。
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO audit_log (trace_id, occurred_at, actor, action, override_kind, detail) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (trace_id, _iso(now()), actor, action, override_kind, detail),
            )
            conn.execute("COMMIT")

    # ── 読み出し ──────────────────────────────────────────

    def get(self, trace_id: str) -> PendingAction | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pending_actions WHERE trace_id = ?", (trace_id,)
            ).fetchone()
        return None if row is None else _to_action(row)

    def due_for_expiry(self, at: datetime | None = None) -> list[PendingAction]:
        """承認期限を過ぎた pending を列挙する（満了ジョブが走査する）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pending_actions WHERE status = ? AND expires_at <= ? "
                "ORDER BY expires_at",
                (HandoffStatus.PENDING.value, _iso(at or now())),
            ).fetchall()
        return [_to_action(row) for row in rows]

    def audit_entries(self, trace_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE trace_id = ? ORDER BY id", (trace_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    # ── 内部 ─────────────────────────────────────────────

    def _transition(
        self,
        trace_id: str,
        *,
        expected: HandoffStatus,
        new: HandoffStatus,
        extra: str = "",
        params: tuple = (),
        guard_expiry: bool = False,
    ) -> PendingAction:
        """条件付きアトミック更新。**更新件数が 1 のときだけ成功とする。**

        二重押下・Slack の再送・期限切れ後の押下は、すべてここで弾かれる。
        """
        assignments = "status = ?" + (f", {extra}" if extra else "")
        at = now()
        condition = "trace_id = ? AND status = ?"
        condition_params: tuple = (trace_id, expected.value)
        if guard_expiry:
            condition += " AND expires_at > ?"
            condition_params = (*condition_params, _iso(at))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                f"UPDATE pending_actions SET {assignments} WHERE {condition}",
                (new.value, *params, *condition_params),
            )
            updated = cursor.rowcount
            if updated != 1:
                current = conn.execute(
                    "SELECT status, expires_at FROM pending_actions WHERE trace_id = ?", (trace_id,)
                ).fetchone()
                conn.execute("ROLLBACK")
                raise StateConflict(trace_id, expected, _actual_of(current, at))
            row = conn.execute(
                "SELECT * FROM pending_actions WHERE trace_id = ?", (trace_id,)
            ).fetchone()
            conn.execute("COMMIT")
        return _to_action(row)

    def _update(self, trace_id: str, assignments: str, params: tuple) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                f"UPDATE pending_actions {assignments} WHERE trace_id = ?", (*params, trace_id)
            )
            conn.execute("COMMIT")


def _actual_of(row: sqlite3.Row | None, at: datetime) -> str | None:
    """条件に合わなかった理由を人間が読める形にする。

    「pending のままだが期限切れ」を "pending" とだけ返すと、なぜ弾かれたのかが分からない。
    """
    if row is None:
        return None
    if row["status"] == HandoffStatus.PENDING.value and datetime.fromisoformat(row["expires_at"]) <= at:
        return "pending(expired)"
    return str(row["status"])


def _to_action(row: sqlite3.Row) -> PendingAction:
    return PendingAction(
        trace_id=row["trace_id"],
        status=row["status"],
        kind=row["kind"],
        event_json=row["event_json"],
        channel=row["channel"],
        message_ts=row["message_ts"],
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        approved_by=row["approved_by"],
        approved_at=_parse(row["approved_at"]),
        executed_at=_parse(row["executed_at"]),
        execution_result=row["execution_result"],
    )


def is_stale(action: PendingAction, *, at: datetime | None = None, threshold: int | None = None) -> bool:
    """TOCTOU の判定。**「承認 -> 実行」ではなく「提案 -> 実行」で測る**（design.md 3.5）。

    承認ボタンの押下から実行層までは同一ハンドラ内の数ミリ秒であり、そこを測っても
    何も検知できない。変化しうるのは、インシデントが起きてからクラスタが辿った時間である。
    """
    limit = threshold if threshold is not None else _toctou_from_env()
    return (at or now()) - action.created_at > timedelta(seconds=limit)


def actionable_until(action: PendingAction, *, threshold: int | None = None) -> datetime:
    """**実際に押して実行できる期限**。承認期限と TOCTOU 閾値の早いほう。

    2 つは別の関心事（前者は承認者の都合、後者は世界が変わったかどうか）だが、
    どちらも提案時刻を起点に測るため、**実効的な窓は短いほうで決まる**。
    15 分の承認期限を表示しながら 5 分で stale に倒れるのは、受け取った人を欺く。
    Handoff 仕様書の既定値（TTL 15 分 / STALE 5 分）はこの矛盾を内包している。
    """
    limit = threshold if threshold is not None else _toctou_from_env()
    return min(action.expires_at, action.created_at + timedelta(seconds=limit))


def _ttl_from_env() -> int:
    raw = os.environ.get("SLACK_APPROVAL_TTL_SECONDS")
    # 設定ミスを黙って既定値に落とさない（例外は握り潰さないのが本 PoC の規律）。
    return DEFAULT_TTL_SECONDS if not raw else int(raw)


def _toctou_from_env() -> int:
    raw = os.environ.get("SLACK_TOCTOU_SECONDS")
    return DEFAULT_TOCTOU_SECONDS if not raw else int(raw)
