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
    # Phase 5 で追加。**既存の値の意味は変えない**（phase5 design.md 3.5）。
    # これが無いと executor が実行中に落ちたとき approved のまま残り、再起動で**再実行される**。
    EXECUTING = "executing"
    EXECUTED = "executed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    STALE = "stale"
    NOTIFY_FAILED = "notify_failed"
    EXECUTION_FAILED = "execution_failed"
    # Phase 5 で追加。実行を**試みたあと**に人間が中止したもの。
    # REJECTED（実行を試みていない状態での却下）と混ぜない。実行の有無は監査上の別事実である。
    ABANDONED = "abandoned"
    UNKNOWN = "unknown"


# 終端ステート。ここに達したら監査基盤へ移送してよい（PoC では移送しない）。
TERMINAL_STATUSES = frozenset(
    {
        HandoffStatus.EXECUTED,
        HandoffStatus.REJECTED,
        HandoffStatus.EXPIRED,
        HandoffStatus.STALE,
        HandoffStatus.ABANDONED,
        HandoffStatus.UNKNOWN,
    }
)

# 実行を試みた結果の失敗のうち、**人間が再試行を選べる**もの。
# UNKNOWN はここに入らない。実行有無が分からないものに再試行ボタンを出すのは、
# 評価不能なものに承認ボタンを出すのと同じ誤りである（D-028）。
RETRYABLE_STATUSES = frozenset({HandoffStatus.EXECUTION_FAILED})

DEFAULT_MAX_RETRIES = 3


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
    # 実行を**開始した**時刻。executed_at（成功時のみ立てる）と分ける。
    # Phase 4 は execution_failed にも executed_at が立っていた（phase4 考察11）。
    execution_started_at: datetime | None
    executed_at: datetime | None
    execution_result: str | None
    retry_count: int


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
  execution_started_at TEXT,
  executed_at      TEXT,
  execution_result TEXT,
  retry_count      INTEGER NOT NULL DEFAULT 0
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
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Phase 4 に作られた既存ファイルへ Phase 5 の列を足す。

        **黙って作り直さない。** 監査記録は運用ステートより寿命が長い（phase4 design.md 3.5.1）ので、
        スキーマが変わったからといって捨ててよいものではない。
        """
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(pending_actions)")}
        for column, ddl in (
            ("execution_started_at", "TEXT"),
            ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in existing:
                conn.execute(f"ALTER TABLE pending_actions ADD COLUMN {column} {ddl}")

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
        return self.create_entry(trace_id=trace_id, kind=kind, event_json=event_json, ttl_seconds=ttl_seconds)

    def create_entry(
        self,
        *,
        trace_id: str,
        kind: str,
        event_json: str,
        status: HandoffStatus = HandoffStatus.PENDING,
        ttl_seconds: int | None = None,
    ) -> PendingAction:
        """エントリを 1 件作る。

        Phase 4 は ``pending`` しか作らなかった（承認待ちだけがストアに入る）。
        Phase 5 では**自律経路の実行失敗**もエントリを持つ（phase5 design.md 3.7）。
        不変条件は「ストアにエントリがあるのは**人間の操作を待つもの**だけ」に一般化される。
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
                    status.value,
                    kind,
                    event_json,
                    _iso(created),
                    _iso(created + timedelta(seconds=ttl)),
                ),
            )
            conn.execute("COMMIT")
        self.audit(trace_id, action="created", detail=f"ttl={ttl}s status={status.value}")
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

    def claim_for_execution(self, trace_id: str) -> PendingAction:
        """approved -> executing。**実行に取りかかる前に**占有する（D-027）。

        これが実行側の排他である。複数の executor が同時に拾っても、
        更新件数が 1 になるのは 1 プロセスだけ。
        ``execution_started_at`` をここで立てる（実行の**開始**時刻であり、完了時刻ではない）。
        """
        return self._transition(
            trace_id,
            expected=HandoffStatus.APPROVED,
            new=HandoffStatus.EXECUTING,
            extra="execution_started_at = ?",
            params=(_iso(now()),),
        )

    def mark_executed(self, trace_id: str, result: str) -> PendingAction:
        """executing -> executed。**成功時にのみ** executed_at を立てる（冪等性の要）。"""
        return self._transition(
            trace_id,
            expected=HandoffStatus.EXECUTING,
            new=HandoffStatus.EXECUTED,
            extra="executed_at = ?, execution_result = ?",
            params=(_iso(now()), result),
        )

    def mark_execution_failed(self, trace_id: str, detail: str) -> PendingAction:
        """executing -> execution_failed。**実行されていないことが確定している**失敗のみ。

        ``executed_at`` は立てない（実行していないため）。試行時刻は
        ``execution_started_at`` にある。実行されたかどうかが分からない場合は
        ここではなく ``mark_unknown()`` に倒す（D-028）。
        """
        return self._transition(
            trace_id,
            expected=HandoffStatus.EXECUTING,
            new=HandoffStatus.EXECUTION_FAILED,
            extra="execution_result = ?",
            params=(detail,),
        )

    def mark_unknown(self, trace_id: str, detail: str) -> PendingAction:
        """executing -> unknown。**実行されたかどうかが分からない**（D-028）。

        ``executed_at`` を立てない。立てると「実行した」と読めてしまい、
        分類した意味が消える。**終端であり、自動でも手動でも再試行させない。**
        """
        return self._transition(
            trace_id,
            expected=HandoffStatus.EXECUTING,
            new=HandoffStatus.UNKNOWN,
            extra="execution_result = ?",
            params=(detail,),
        )

    def retry(self, trace_id: str, *, actor: str, max_retries: int | None = None) -> PendingAction:
        """execution_failed -> approved。人間が押したときだけ呼ぶ（自動再試行はしない）。

        **実行内容は復元し直す。** ここで変えるのは ``status`` と ``retry_count`` だけであり、
        ``event_json`` には触れない（運用原則4「再試行の同一性」）。
        """
        limit = max_retries if max_retries is not None else _max_retries_from_env()
        row = self.get(trace_id)
        if row is not None and row.retry_count >= limit:
            raise StateConflict(trace_id, HandoffStatus.EXECUTION_FAILED, f"retry_exhausted({row.retry_count})")
        updated = self._transition(
            trace_id,
            expected=HandoffStatus.EXECUTION_FAILED,
            new=HandoffStatus.APPROVED,
            extra="retry_count = retry_count + 1, approved_by = ?, approved_at = ?, execution_started_at = NULL",
            params=(actor, _iso(now())),
        )
        self.audit(trace_id, action="retry", actor=actor, detail=f"retry_count={updated.retry_count}")
        return updated

    def abandon(self, trace_id: str, *, actor: str) -> PendingAction:
        """execution_failed -> abandoned。**実行を試みたあとの**中止であり、却下とは別の事実。"""
        row = self._transition(
            trace_id,
            expected=HandoffStatus.EXECUTION_FAILED,
            new=HandoffStatus.ABANDONED,
        )
        self.audit(trace_id, action="abandon", actor=actor)
        return row

    def recover_executing(self, detail: str) -> list[PendingAction]:
        """``executing`` のまま残っているものを ``unknown`` に倒す（executor の起動時に呼ぶ）。

        「実行を開始した記録はあるが結果が無い」＝定義どおり実行有無が不明である。
        **approved に戻して再実行してはならない。** それが二重実行の典型的な作り方になる。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT trace_id FROM pending_actions WHERE status = ?",
                (HandoffStatus.EXECUTING.value,),
            ).fetchall()
        recovered = []
        for row in rows:
            trace_id = row["trace_id"]
            recovered.append(self.mark_unknown(trace_id, detail))
            self.audit(trace_id, action="recovered_unknown", detail=detail)
        return recovered

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

    def due_for_execution(self, limit: int = 20) -> list[PendingAction]:
        """実行待ち（``approved``）を古い順に列挙する。executor が走査する。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pending_actions WHERE status = ? ORDER BY approved_at LIMIT ?",
                (HandoffStatus.APPROVED.value, limit),
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
        execution_started_at=_parse(row["execution_started_at"]),
        executed_at=_parse(row["executed_at"]),
        execution_result=row["execution_result"],
        retry_count=row["retry_count"] or 0,
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


def _max_retries_from_env() -> int:
    raw = os.environ.get("EXECUTION_MAX_RETRIES")
    return DEFAULT_MAX_RETRIES if not raw else int(raw)


def can_retry(action: PendingAction, *, max_retries: int | None = None) -> bool:
    """**再試行ボタンを出してよいか。** 通知層はこの関数だけを見る（分岐を増やさない）。"""
    limit = max_retries if max_retries is not None else _max_retries_from_env()
    return HandoffStatus(action.status) in RETRYABLE_STATUSES and action.retry_count < limit


def _toctou_from_env() -> int:
    raw = os.environ.get("SLACK_TOCTOU_SECONDS")
    return DEFAULT_TOCTOU_SECONDS if not raw else int(raw)
