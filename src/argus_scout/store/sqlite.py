"""SQLite store (stdlib ``sqlite3``, no ORM).

Schema changes are ordered SQL scripts in :data:`MIGRATIONS`; the database's
``PRAGMA user_version`` records how many have run, and :class:`SqliteStore`
applies the rest on open, each in its own transaction. Never edit a shipped
migration: append a new one.

Threading: one connection per store, opened with ``check_same_thread=False``
and serialised by a re-entrant lock. Every public method takes the lock for
its whole read or read-modify-write, so a FastAPI app and a scheduler thread
can share one :class:`SqliteStore`. The workload is tiny, so a single writer
costs nothing, and it keeps ``":memory:"`` (one database per connection)
working in tests. WAL mode and a busy timeout cover a second process (e.g. a
CLI reading the same file).

Datetimes are stored as aware UTC ISO-8601 text with fixed microsecond
precision, so they sort and compare correctly as strings, and come back as
aware UTC datetimes.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from argus_scout.approvals import StandingRule
from argus_scout.events import StatusEvent, parse_dt
from argus_scout.recommendations import TRANSITIONS, Recommendation, Status
from argus_scout.severity import Severity
from argus_scout.store.base import (
    UNCHECKED,
    DeliveryRecord,
    IllegalTransition,
    InboxEntry,
    JobRun,
    PendingDelivery,
    Report,
    StaleRecommendation,
    Transition,
    Unchecked,
)

log = structlog.get_logger()

MIGRATIONS: tuple[str, ...] = (
    # 1: initial schema
    """
    CREATE TABLE events (
        source            TEXT NOT NULL,
        event_id          TEXT NOT NULL,
        fingerprint       TEXT NOT NULL,
        severity          TEXT NOT NULL,
        generated_at      TEXT NOT NULL,
        expires_at        TEXT,
        first_seen        TEXT NOT NULL,
        last_seen         TEXT NOT NULL,
        seen_count        INTEGER NOT NULL DEFAULT 1,
        acked_at          TEXT,
        acked_by          TEXT,
        last_announced_at TEXT,
        broadcast_count   INTEGER NOT NULL DEFAULT 0,
        data              TEXT NOT NULL,
        PRIMARY KEY (source, event_id)
    );
    CREATE INDEX events_fingerprint ON events (fingerprint);
    CREATE INDEX events_open ON events (acked_at, first_seen);

    CREATE TABLE recommendations (
        id         TEXT PRIMARY KEY,
        status     TEXT NOT NULL,
        analyzer   TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        data       TEXT NOT NULL
    );
    CREATE INDEX recommendations_status ON recommendations (status, created_at);

    CREATE TABLE recommendation_transitions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        rec_id      TEXT NOT NULL REFERENCES recommendations (id) ON DELETE CASCADE,
        from_status TEXT,
        to_status   TEXT NOT NULL,
        at          TEXT NOT NULL,
        by          TEXT,
        source      TEXT,
        rule_id     TEXT,
        reason      TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX recommendation_transitions_rec ON recommendation_transitions (rec_id, id);

    CREATE TABLE standing_rules (
        rule_id    TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        expires_at TEXT,
        data       TEXT NOT NULL
    );

    CREATE TABLE reports (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        kind         TEXT NOT NULL,
        generated_at TEXT NOT NULL,
        text         TEXT NOT NULL,
        meta         TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX reports_kind ON reports (kind, generated_at);
    CREATE INDEX reports_generated ON reports (generated_at);
    """,
    # 2: scheduler bookkeeping, one row per job
    """
    CREATE TABLE job_runs (
        name            TEXT PRIMARY KEY,
        last_run_at     TEXT,
        last_success_at TEXT,
        last_error_at   TEXT,
        last_error      TEXT,
        run_count       INTEGER NOT NULL DEFAULT 0,
        error_count     INTEGER NOT NULL DEFAULT 0
    );
    """,
    # 3: recommendation dedup key (rule + distinguishing labels). Older rows have no key.
    """
    ALTER TABLE recommendations ADD COLUMN key TEXT NOT NULL DEFAULT '';
    UPDATE recommendations SET key = COALESCE(json_extract(data, '$.key'), '');
    CREATE INDEX recommendations_key ON recommendations (key, status, updated_at);
    """,
    # 4: signal rules' pending ``for`` state, so a restart doesn't reset how long a condition has held
    """
    CREATE TABLE signal_pending (
        rule  TEXT NOT NULL,
        key   TEXT NOT NULL,
        since TEXT NOT NULL,
        PRIMARY KEY (rule, key)
    );
    """,
    # 5: delivery records, one row per attempt at a message on a channel. At most one successful row per
    # (delivery_key, channel): that is what makes re-delivery idempotent, across restarts too.
    """
    CREATE TABLE deliveries (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        delivery_key TEXT NOT NULL,
        channel      TEXT NOT NULL,
        report_id    INTEGER,
        event_source TEXT,
        event_id     TEXT,
        at           TEXT NOT NULL,
        ok           INTEGER NOT NULL,
        error        TEXT
    );
    CREATE UNIQUE INDEX deliveries_sent ON deliveries (delivery_key, channel) WHERE ok = 1;
    CREATE INDEX deliveries_key ON deliveries (delivery_key, id);
    CREATE INDEX deliveries_at ON deliveries (at);
    """,
    # 6: a system-side "cleared" for status events (a polled condition that disappeared), distinct from a
    # person's ack: resolved_at is set by scout, acked_at/acked_by only by a person.
    """
    ALTER TABLE events ADD COLUMN resolved_at TEXT;
    """,
    # 7: daily usage counts (budget caps: llm_calls, tts, speak, any kind), one row per local day and kind. Each
    # charge is one atomic upsert, so the server and a CLI run sharing this file can't lose a count or exceed a cap
    # together (was usage.json).
    """
    CREATE TABLE IF NOT EXISTS usage_counts (
        day  TEXT NOT NULL,
        kind TEXT NOT NULL,
        used INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (day, kind)
    );
    """,
    # 8: failed deliveries the channel refused permanently (a 4xx), which the redelivery sweep never retries: one
    # row per such attempt. A side table, not a column, so replaying the migration is harmless.
    """
    CREATE TABLE IF NOT EXISTS delivery_refusals (
        delivery_id INTEGER PRIMARY KEY REFERENCES deliveries (id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS deliveries_report ON deliveries (report_id, channel);
    """,
    # 9: each recommendation's revision, bumped on every save, so a person's decision (and the engine's
    # load-then-save) is refused if the row's content changed since it was read, not only its status. A side
    # table, not a column, so replaying the migration is harmless; a row with no revision yet reads as 0.
    """
    CREATE TABLE IF NOT EXISTS recommendation_revisions (
        rec_id   TEXT PRIMARY KEY REFERENCES recommendations (id) ON DELETE CASCADE,
        revision INTEGER NOT NULL
    );
    """,
)

_REC_SELECT = (
    "SELECT r.data, r.status, COALESCE(v.revision, 0) AS revision"
    " FROM recommendations r LEFT JOIN recommendation_revisions v ON v.rec_id = r.id"
)

SCHEMA_VERSION = len(MIGRATIONS)


def _ts(value: datetime | None) -> str | None:
    dt = parse_dt(value)
    return dt.astimezone(UTC).isoformat(timespec="microseconds") if dt else None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value).astimezone(UTC) if value else None


def _now(value: datetime | None = None) -> datetime:
    return parse_dt(value) or datetime.now(UTC)


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations; returns the resulting schema version. Idempotent."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > SCHEMA_VERSION:
        raise RuntimeError(f"database schema v{current} is newer than this argus-scout (v{SCHEMA_VERSION})")
    for version in range(current + 1, SCHEMA_VERSION + 1):
        try:
            conn.executescript(f"BEGIN;\n{MIGRATIONS[version - 1]}\nPRAGMA user_version = {version};\nCOMMIT;")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        log.info("store.migrated", version=version)
    return SCHEMA_VERSION


class SqliteStore:
    """The default :class:`~argus_scout.store.base.Store`. ``path`` may be ``":memory:"``."""

    @classmethod
    def copy_of(cls, path: Path | str) -> SqliteStore:
        """An in-memory copy of the database at ``path`` (read-only open; empty if it doesn't exist).

        Writes to the copy never reach the file: a dry run sees real history without changing it.
        """
        store = cls(":memory:")
        if str(path) != ":memory:" and Path(path).is_file():
            src = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
            try:
                with store._lock:
                    src.backup(store._conn)
                    migrate(store._conn)
            finally:
                src.close()
        return store

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._tx_depth = 0  # >0 while this thread (holding the lock) is inside a transaction
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute("PRAGMA journal_mode = WAL")
        with self._lock:
            migrate(self._conn)

    # -- plumbing ------------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> SqliteStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        with self._lock:
            return self._conn.execute("PRAGMA user_version").fetchone()[0]

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._tx_depth:  # inside transaction(): join it, and let the outermost commit or roll back
                self._tx_depth += 1
                try:
                    yield self._conn
                finally:
                    self._tx_depth -= 1
                return
            self._conn.execute("BEGIN IMMEDIATE")
            self._tx_depth = 1
            try:
                yield self._conn
                self._conn.execute("COMMIT")  # inside the try: a failed commit is rolled back, never left open
            except BaseException:
                # SQLite may already have rolled back on its own (SQLITE_FULL, SQLITE_IOERR, an interrupt); a second
                # ROLLBACK would raise "no transaction is active" in place of the error that caused it.
                if self._conn.in_transaction:
                    try:
                        self._conn.execute("ROLLBACK")
                    except sqlite3.Error as exc:
                        log.warning("store.rollback_failed", error=str(exc))
                raise
            finally:
                self._tx_depth = 0

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """One ``BEGIN IMMEDIATE`` transaction around every store call made inside it on this thread: all of them
        commit together, or (on any exception) none do. Other threads wait for it."""
        with self._tx():
            yield

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    # -- status events ---------------------------------------------------------
    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> StatusEvent:
        event = StatusEvent.from_dict(json.loads(row["data"]))
        event.event_id = row["event_id"]
        event.acked_at = _dt(row["acked_at"])
        event.last_broadcast_at = _dt(row["last_announced_at"])
        event.broadcast_count = row["broadcast_count"]
        event.ingested_at = _dt(row["first_seen"]) or event.ingested_at
        return event

    @classmethod
    def _entry_from_row(cls, row: sqlite3.Row) -> InboxEntry:
        return InboxEntry(
            event=cls._event_from_row(row),
            fingerprint=row["fingerprint"],
            first_seen=_dt(row["first_seen"]),  # type: ignore[arg-type]
            last_seen=_dt(row["last_seen"]),  # type: ignore[arg-type]
            seen_count=row["seen_count"],
            acked_by=row["acked_by"],
            resolved_at=_dt(row["resolved_at"]),
        )

    def upsert_event(self, event: StatusEvent, *, now: datetime | None = None) -> bool:
        """Insert or refresh by ``(source, event_id)``; True when it was new.

        A refresh updates the content and ``last_seen`` and keeps ``first_seen``.
        The ack and announce bookkeeping are kept while the fingerprint is
        unchanged; a changed fingerprint (e.g. a severity change under the same
        id) is new news, so it reopens the event and clears its announce state.
        So does seeing a *resolved* event again: the condition cleared and came back.
        """
        seen = _ts(_now(now))
        data = json.dumps(event.to_dict())
        fp = event.fingerprint
        with self._tx() as conn:
            row = conn.execute(
                "SELECT fingerprint, resolved_at FROM events WHERE source = ? AND event_id = ?",
                (event.source, event.event_id),
            ).fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO events (source, event_id, fingerprint, severity, generated_at, expires_at,
                           first_seen, last_seen, seen_count, acked_at, acked_by, last_announced_at,
                           broadcast_count, data)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, NULL, ?, ?, ?)""",
                    (
                        event.source,
                        event.event_id,
                        fp,
                        event.severity.value,
                        _ts(event.generated_at),
                        _ts(event.expires_at),
                        seen,
                        seen,
                        _ts(event.acked_at),
                        _ts(event.last_broadcast_at),
                        event.broadcast_count,
                        data,
                    ),
                )
                return True
            reset = ""
            if row["fingerprint"] != fp or row["resolved_at"] is not None:
                reset = ", acked_at = NULL, acked_by = NULL, last_announced_at = NULL, broadcast_count = 0"
            conn.execute(
                f"""UPDATE events SET fingerprint = ?, severity = ?, generated_at = ?, expires_at = ?,
                        last_seen = ?, seen_count = seen_count + 1, data = ?, resolved_at = NULL{reset}
                    WHERE source = ? AND event_id = ?""",
                (
                    fp,
                    event.severity.value,
                    _ts(event.generated_at),
                    _ts(event.expires_at),
                    seen,
                    data,
                    event.source,
                    event.event_id,
                ),
            )
            return False

    def get_event(self, source: str, event_id: str) -> StatusEvent | None:
        entry = self.get_entry(source, event_id)
        return entry.event if entry else None

    def get_entry(self, source: str, event_id: str) -> InboxEntry | None:
        rows = self._query("SELECT * FROM events WHERE source = ? AND event_id = ?", (source, event_id))
        return self._entry_from_row(rows[0]) if rows else None

    def ack_event(self, source: str, event_id: str, by: str, *, at: datetime | None = None) -> bool:
        """Acknowledge an open event; False if it is unknown or already acked."""
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE events SET acked_at = ?, acked_by = ? WHERE source = ? AND event_id = ? AND acked_at IS NULL",
                (_ts(_now(at)), by, source, event_id),
            )
            return cur.rowcount > 0

    def mark_announced(self, source: str, event_id: str, *, at: datetime | None = None) -> bool:
        """Record a broadcast: sets ``last_broadcast_at`` and bumps ``broadcast_count``."""
        with self._tx() as conn:
            cur = conn.execute(
                """UPDATE events SET last_announced_at = ?, broadcast_count = broadcast_count + 1
                   WHERE source = ? AND event_id = ?""",
                (_ts(_now(at)), source, event_id),
            )
            return cur.rowcount > 0

    def resolve_missing(self, source: str, keep: Iterable[str], *, at: datetime | None = None) -> list[str]:
        """Mark every unresolved event of ``source`` whose id is not in ``keep`` resolved (cleared at the source).

        A system-side close: ``acked_at``/``acked_by`` are left alone, so a person's ack audit stays theirs.
        Seeing the event again reopens it (see :meth:`upsert_event`). Returns the ids it resolved."""
        keep = list(dict.fromkeys(keep))
        marks = ", ".join("?" * len(keep))
        where = "source = ? AND resolved_at IS NULL" + (f" AND event_id NOT IN ({marks})" if keep else "")
        with self._tx() as conn:
            ids = [r["event_id"] for r in conn.execute(f"SELECT event_id FROM events WHERE {where}", [source, *keep])]
            if ids:
                conn.execute(f"UPDATE events SET resolved_at = ? WHERE {where}", [_ts(_now(at)), source, *keep])
        return ids

    def open_events(self, *, now: datetime | None = None, include_expired: bool = False) -> list[StatusEvent]:
        """Unacked, unresolved events, oldest first; expired ones are left out unless asked for."""
        sql = "SELECT * FROM events WHERE acked_at IS NULL AND resolved_at IS NULL"
        params: list[Any] = []
        if not include_expired:
            sql += " AND (expires_at IS NULL OR expires_at > ?)"
            params.append(_ts(_now(now)))
        return [self._event_from_row(r) for r in self._query(sql + " ORDER BY first_seen, rowid", params)]

    def events_by_fingerprint(self, fingerprint: str, *, open_only: bool = True) -> list[StatusEvent]:
        """Every event with this content identity (see :attr:`StatusEvent.fingerprint`), oldest first."""
        sql = "SELECT * FROM events WHERE fingerprint = ?"
        if open_only:
            sql += " AND acked_at IS NULL AND resolved_at IS NULL"
        return [self._event_from_row(r) for r in self._query(sql + " ORDER BY first_seen, rowid", (fingerprint,))]

    def open_fingerprints(self) -> dict[str, int]:
        """Open events counted per fingerprint: the cheap duplicate check."""
        rows = self._query(
            "SELECT fingerprint, COUNT(*) AS n FROM events WHERE acked_at IS NULL AND resolved_at IS NULL"
            " GROUP BY fingerprint"
        )
        return {r["fingerprint"]: r["n"] for r in rows}

    # -- recommendations ---------------------------------------------------------
    @staticmethod
    def _rec_from_row(row: sqlite3.Row) -> Recommendation:
        rec = Recommendation.from_dict(json.loads(row["data"]))
        rec.revision = row["revision"]
        return rec

    def save_recommendation(
        self,
        rec: Recommendation,
        *,
        by: str | None = None,
        reason: str | None = None,
        expected_status: Status | None | Unchecked = UNCHECKED,
        expected_revision: int | Unchecked = UNCHECKED,
    ) -> None:
        """Insert or update ``rec``. A new row or a status change appends a transition. Every save bumps the
        row's revision, and sets ``rec.revision`` to it.

        ``by`` names who made the change (a standing approval defaults to
        ``rule:<id>``); ``reason`` defaults to the rejection reason on a reject.

        The checks and the write share one ``BEGIN IMMEDIATE`` transaction, so they can't interleave with
        another writer: a stored status other than ``expected_status``, or a stored revision other than
        ``expected_revision`` (or no row, when one is given), raises :class:`StaleRecommendation`; a status
        change the lifecycle forbids raises :class:`IllegalTransition`. Either way nothing is written.
        """
        with self._tx() as conn:
            revision = self._write_recommendation(conn, rec, by, reason, expected_status, expected_revision)
        rec.revision = revision

    def update_recommendation(
        self,
        rec_id: str,
        change: Callable[[Recommendation], None],
        *,
        expected_revision: int,
        by: str | None = None,
        reason: str | None = None,
    ) -> Recommendation | None:
        """Apply ``change`` to the **stored** row and save it, in one transaction; None if there is no such row.

        The row is read inside the transaction, so the change lands on the current content, never on a copy
        read earlier. A stored revision other than ``expected_revision`` raises :class:`StaleRecommendation`
        before ``change`` runs. Anything ``change`` raises rolls the transaction back and propagates.
        """
        with self._tx() as conn:
            row = conn.execute(f"{_REC_SELECT} WHERE r.id = ?", (rec_id,)).fetchone()
            if row is None:
                return None
            rec = self._rec_from_row(row)
            if rec.revision != expected_revision:
                raise StaleRecommendation(
                    f"recommendation {rec_id} is at revision {rec.revision}, not {expected_revision}"
                )
            loaded = rec.status
            change(rec)
            revision = self._write_recommendation(conn, rec, by, reason, loaded, expected_revision)
        rec.revision = revision
        return rec

    @staticmethod
    def _write_recommendation(
        conn: sqlite3.Connection,
        rec: Recommendation,
        by: str | None,
        reason: str | None,
        expected_status: Status | None | Unchecked,
        expected_revision: int | Unchecked,
    ) -> int:
        """The checks and writes of a save, inside the caller's transaction. Returns the new revision."""
        row = conn.execute(
            "SELECT r.status, COALESCE(v.revision, 0) AS revision FROM recommendations r"
            " LEFT JOIN recommendation_revisions v ON v.rec_id = r.id WHERE r.id = ?",
            (rec.id,),
        ).fetchone()
        prior = Status(row["status"]) if row else None
        stored_revision = row["revision"] if row else None
        if not isinstance(expected_status, Unchecked) and prior != expected_status:
            stored = prior.value if prior else "absent"
            expected = expected_status.value if expected_status else "absent"
            raise StaleRecommendation(f"recommendation {rec.id} is {stored}, not {expected} as loaded")
        if not isinstance(expected_revision, Unchecked) and stored_revision != expected_revision:
            stored = f"at revision {stored_revision}" if row else "absent"
            raise StaleRecommendation(
                f"recommendation {rec.id} is {stored}, not revision {expected_revision} as loaded"
            )
        if prior is not None and prior != rec.status and rec.status not in TRANSITIONS[prior]:
            raise IllegalTransition(f"recommendation {rec.id}: {prior.value} -> {rec.status.value} is not allowed")
        revision = (stored_revision or 0) + 1
        conn.execute(
            """INSERT INTO recommendations (id, status, analyzer, key, created_at, updated_at, data)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (id) DO UPDATE SET status = excluded.status, analyzer = excluded.analyzer,
                   key = excluded.key, updated_at = excluded.updated_at, data = excluded.data""",
            (
                rec.id,
                rec.status.value,
                rec.analyzer,
                rec.key,
                _ts(rec.created_at),
                _ts(rec.updated_at),
                json.dumps(rec.to_dict() | {"revision": revision}),
            ),
        )
        conn.execute(
            """INSERT INTO recommendation_revisions (rec_id, revision) VALUES (?, ?)
               ON CONFLICT (rec_id) DO UPDATE SET revision = excluded.revision""",
            (rec.id, revision),
        )
        if prior == rec.status:
            return revision
        approved = rec.status == Status.APPROVED
        source = rec.approval_source if approved else None
        rule_id = rec.approved_by_rule if approved else None
        if by is None and source == "standing" and rule_id:
            by = f"rule:{rule_id}"
        if reason is None:
            reasons = {Status.REJECTED: rec.rejection_reason, Status.RETIRED: rec.retired_reason}
            reason = reasons.get(rec.status, "")
        at = (rec.approved_at if approved else None) or rec.updated_at
        conn.execute(
            """INSERT INTO recommendation_transitions
                   (rec_id, from_status, to_status, at, by, source, rule_id, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (rec.id, prior.value if prior else None, rec.status.value, _ts(at), by, source, rule_id, reason),
        )
        return revision

    def get_recommendation(self, rec_id: str) -> Recommendation | None:
        rows = self._query(f"{_REC_SELECT} WHERE r.id = ?", (rec_id,))
        return self._rec_from_row(rows[0]) if rows else None

    def list_recommendations(
        self, status: Status | Iterable[Status] | None = None, *, limit: int | None = None
    ) -> list[Recommendation]:
        """Recommendations, oldest first, optionally filtered to one or more statuses."""
        sql = _REC_SELECT
        params: list[Any] = []
        if status is not None:
            statuses = [status] if isinstance(status, str) else list(status)
            sql += f" WHERE r.status IN ({', '.join('?' * len(statuses))})"
            params.extend(Status(s).value for s in statuses)
        sql += " ORDER BY r.created_at, r.id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [self._rec_from_row(r) for r in self._query(sql, params)]

    def recommendations_by_key(self, key: str, status: Status | Iterable[Status] | None = None) -> list[Recommendation]:
        """Recommendations with this dedup key, most recently updated first."""
        sql = f"{_REC_SELECT} WHERE r.key = ?"
        params: list[Any] = [key]
        if status is not None:
            statuses = [status] if isinstance(status, str) else list(status)
            sql += f" AND r.status IN ({', '.join('?' * len(statuses))})"
            params.extend(Status(s).value for s in statuses)
        sql += " ORDER BY r.updated_at DESC, r.id"
        return [self._rec_from_row(r) for r in self._query(sql, params)]

    def recent_rejections(self, limit: int = 10) -> list[Recommendation]:
        """Rejected recommendations, most recently rejected first (with their reasons)."""
        rows = self._query(
            f"{_REC_SELECT} WHERE r.status = ? ORDER BY r.updated_at DESC, r.id LIMIT ?",
            (Status.REJECTED.value, int(limit)),
        )
        return [self._rec_from_row(r) for r in rows]

    def recommendation_history(self, rec_id: str) -> list[Transition]:
        rows = self._query("SELECT * FROM recommendation_transitions WHERE rec_id = ? ORDER BY id", (rec_id,))
        return [
            Transition(
                rec_id=r["rec_id"],
                from_status=Status(r["from_status"]) if r["from_status"] else None,
                to_status=Status(r["to_status"]),
                at=_dt(r["at"]),  # type: ignore[arg-type]
                by=r["by"],
                source=r["source"],
                rule_id=r["rule_id"],
                reason=r["reason"],
            )
            for r in rows
        ]

    # -- standing approvals (runtime rules) ---------------------------------------
    def load_runtime_rules(self) -> list[StandingRule]:
        rows = self._query("SELECT data FROM standing_rules ORDER BY created_at, rule_id")
        return [StandingRule.from_record(json.loads(r["data"])) for r in rows]

    def save_runtime_rule(self, rule: StandingRule) -> None:
        """Persist a runtime rule. File rules live in the YAML file and are refused."""
        if rule.origin != "api":
            raise ValueError("only runtime (origin 'api') rules are stored; file rules live in the approvals file")
        data = rule.to_dict()
        data.pop("expired", None)
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO standing_rules (rule_id, created_at, expires_at, data) VALUES (?, ?, ?, ?)
                   ON CONFLICT (rule_id) DO UPDATE SET created_at = excluded.created_at,
                       expires_at = excluded.expires_at, data = excluded.data""",
                (rule.rule_id, _ts(rule.created_at), _ts(rule.expires_at), json.dumps(data)),
            )

    def delete_runtime_rule(self, rule_id: str) -> bool:
        with self._tx() as conn:
            return conn.execute("DELETE FROM standing_rules WHERE rule_id = ?", (rule_id,)).rowcount > 0

    # -- signal rules' pending state ---------------------------------------------------
    def load_signal_pending(self) -> dict[str, dict[str, datetime]]:
        """Since when each (rule, instance key) condition has held, for rules still pending."""
        pending: dict[str, dict[str, datetime]] = {}
        for r in self._query("SELECT rule, key, since FROM signal_pending"):
            pending.setdefault(r["rule"], {})[r["key"]] = _dt(r["since"])  # type: ignore[assignment]
        return pending

    def save_signal_pending(self, rule: str, pending: dict[str, datetime]) -> None:
        """Replace one rule's pending state."""
        with self._tx() as conn:
            conn.execute("DELETE FROM signal_pending WHERE rule = ?", (rule,))
            conn.executemany(
                "INSERT INTO signal_pending (rule, key, since) VALUES (?, ?, ?)",
                [(rule, key, _ts(since)) for key, since in pending.items()],
            )

    def drop_signal_pending(self, keep: Iterable[str]) -> int:
        """Forget the pending state of every rule not in ``keep`` (rules removed from the file)."""
        keep = list(keep)
        sql = "DELETE FROM signal_pending"
        if keep:
            sql += f" WHERE rule NOT IN ({', '.join('?' * len(keep))})"
        with self._tx() as conn:
            return conn.execute(sql, keep).rowcount

    # -- reports -------------------------------------------------------------------
    @staticmethod
    def _report_from_row(row: sqlite3.Row) -> Report:
        return Report(
            id=row["id"],
            kind=row["kind"],
            generated_at=_dt(row["generated_at"]),  # type: ignore[arg-type]
            text=row["text"],
            meta=json.loads(row["meta"]),
        )

    def add_report(self, kind: str, generated_at: datetime, text: str, meta: dict[str, Any] | None = None) -> Report:
        meta = dict(meta or {})
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT INTO reports (kind, generated_at, text, meta) VALUES (?, ?, ?, ?)",
                (kind, _ts(generated_at), text, json.dumps(meta, default=str)),
            )
            report_id = cur.lastrowid
        return Report(
            id=report_id or 0, kind=kind, generated_at=_now(generated_at).astimezone(UTC), text=text, meta=meta
        )

    def latest_report(self, kind: str | None = None) -> Report | None:
        reports = self.list_reports(1, kind=kind)
        return reports[0] if reports else None

    def list_reports(self, limit: int = 50, *, kind: str | None = None) -> list[Report]:
        """Reports, newest first."""
        sql = "SELECT * FROM reports"
        params: list[Any] = []
        if kind is not None:
            sql += " WHERE kind = ?"
            params.append(kind)
        sql += " ORDER BY generated_at DESC, id DESC LIMIT ?"
        params.append(int(limit))
        return [self._report_from_row(r) for r in self._query(sql, params)]

    def get_report(self, report_id: int) -> Report | None:
        rows = self._query("SELECT * FROM reports WHERE id = ?", [int(report_id)])
        return self._report_from_row(rows[0]) if rows else None

    # -- delivery records -------------------------------------------------------------
    @staticmethod
    def _delivery_from_row(row: sqlite3.Row) -> DeliveryRecord:
        return DeliveryRecord(
            delivery_key=row["delivery_key"],
            channel=row["channel"],
            at=_dt(row["at"]),  # type: ignore[arg-type]
            ok=bool(row["ok"]),
            error=row["error"],
            report_id=row["report_id"],
            event_source=row["event_source"],
            event_id=row["event_id"],
            permanent=bool(row["permanent"]),
        )

    def record_delivery(
        self,
        delivery_key: str,
        channel: str,
        *,
        ok: bool,
        error: str | None = None,
        at: datetime | None = None,
        report_id: int | None = None,
        event_source: str | None = None,
        event_id: str | None = None,
        permanent: bool = False,
    ) -> bool:
        """Record one delivery attempt. Returns False when ``ok`` and the message was already recorded as
        sent on this channel (nothing is written then: a message is sent at most once per channel).
        ``permanent`` marks a failure the channel refused for good, which is never retried."""
        with self._tx() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO deliveries
                       (delivery_key, channel, report_id, event_source, event_id, at, ok, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (delivery_key, channel, report_id, event_source, event_id, _ts(_now(at)), int(ok), error),
            )
            if cur.rowcount > 0 and permanent and not ok:
                conn.execute("INSERT INTO delivery_refusals (delivery_id) VALUES (?)", (cur.lastrowid,))
            return cur.rowcount > 0

    def undelivered_reports(self, since: datetime, *, max_failures: int) -> list[PendingDelivery]:
        """Report deliveries worth retrying: a report written at or after ``since`` and a channel it was tried on
        with no success, no permanent refusal and fewer than ``max_failures`` failed attempts. Oldest first.
        A channel the report was never tried on (filtered, held back, added later) is not listed."""
        rows = self._query(
            """SELECT r.id, r.kind, r.generated_at, r.text, r.meta,
                      d.channel, COUNT(*) AS failures, MAX(d.at) AS last_at
                 FROM deliveries d JOIN reports r ON r.id = d.report_id
                 LEFT JOIN delivery_refusals x ON x.delivery_id = d.id
                WHERE r.generated_at >= ? AND d.delivery_key = 'report:' || r.id
                GROUP BY r.id, d.channel
               HAVING SUM(d.ok) = 0 AND COUNT(x.delivery_id) = 0 AND COUNT(*) < ?
                ORDER BY r.generated_at, r.id, d.channel""",
            (_ts(since), int(max_failures)),
        )
        return [
            PendingDelivery(
                report=self._report_from_row(r),
                channel=r["channel"],
                failures=r["failures"],
                last_at=_dt(r["last_at"]),  # type: ignore[arg-type]
            )
            for r in rows
        ]

    def delivered_channels(self, delivery_key: str) -> set[str]:
        """The channels this message was successfully delivered on."""
        rows = self._query("SELECT channel FROM deliveries WHERE delivery_key = ? AND ok = 1", (delivery_key,))
        return {r["channel"] for r in rows}

    def delivery_counts(self) -> dict[tuple[str, bool], int]:
        """Delivery attempts per ``(channel, ok)``, for /metrics."""
        rows = self._query("SELECT channel, ok, COUNT(*) AS n FROM deliveries GROUP BY channel, ok")
        return {(r["channel"], bool(r["ok"])): r["n"] for r in rows}

    def list_deliveries(self, delivery_key: str | None = None, *, limit: int = 100) -> list[DeliveryRecord]:
        """Delivery attempts, newest first, optionally for one message."""
        sql = (
            "SELECT d.*, x.delivery_id IS NOT NULL AS permanent FROM deliveries d"
            " LEFT JOIN delivery_refusals x ON x.delivery_id = d.id"
        )
        params: list[Any] = []
        if delivery_key is not None:
            sql += " WHERE d.delivery_key = ?"
            params.append(delivery_key)
        sql += " ORDER BY d.at DESC, d.id DESC LIMIT ?"
        params.append(int(limit))
        return [self._delivery_from_row(r) for r in self._query(sql, params)]

    # -- scheduler bookkeeping -------------------------------------------------------
    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> JobRun:
        return JobRun(
            name=row["name"],
            last_run_at=_dt(row["last_run_at"]),
            last_success_at=_dt(row["last_success_at"]),
            last_error_at=_dt(row["last_error_at"]),
            last_error=row["last_error"],
            run_count=row["run_count"],
            error_count=row["error_count"],
        )

    def get_job_run(self, name: str) -> JobRun | None:
        rows = self._query("SELECT * FROM job_runs WHERE name = ?", (name,))
        return self._job_from_row(rows[0]) if rows else None

    def list_job_runs(self) -> list[JobRun]:
        return [self._job_from_row(r) for r in self._query("SELECT * FROM job_runs ORDER BY name")]

    def record_job_run(
        self, name: str, *, started_at: datetime, finished_at: datetime, error: str | None = None
    ) -> JobRun:
        """Count one run of ``name``. A success moves ``last_run_at`` (what the schedule keys on) to
        ``started_at``; a failure leaves it alone, so the run is still due and is retried."""
        ok = error is None
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO job_runs (name, last_run_at, last_success_at, last_error_at, last_error,
                       run_count, error_count)
                   VALUES (?, ?, ?, ?, ?, 1, ?)
                   ON CONFLICT (name) DO UPDATE SET
                       last_run_at = COALESCE(excluded.last_run_at, last_run_at),
                       last_success_at = COALESCE(excluded.last_success_at, last_success_at),
                       last_error_at = COALESCE(excluded.last_error_at, last_error_at),
                       last_error = COALESCE(excluded.last_error, last_error),
                       run_count = run_count + 1,
                       error_count = error_count + excluded.error_count""",
                (
                    name,
                    _ts(started_at) if ok else None,
                    _ts(finished_at) if ok else None,
                    None if ok else _ts(finished_at),
                    error,
                    0 if ok else 1,
                ),
            )
            row = conn.execute("SELECT * FROM job_runs WHERE name = ?", (name,)).fetchone()
        return self._job_from_row(row)

    # -- daily usage counts (DailyBudget's UsageCounter) ---------------------------------------
    def usage_counts(self, day: str) -> dict[str, int]:
        return {r["kind"]: r["used"] for r in self._query("SELECT kind, used FROM usage_counts WHERE day = ?", (day,))}

    def consume_usage(self, day: str, kind: str, limit: int, *, allow_over: bool = False) -> tuple[bool, int]:
        """Charge one ``kind`` on ``day`` unless it is at ``limit``; returns (charged, count after).

        The limit check is the upsert's own ``WHERE``, so check and increment are one statement inside one
        ``BEGIN IMMEDIATE`` transaction: another connection (another process) can't slip in between them.
        """
        unlimited = allow_over or limit <= 0
        with self._tx() as conn:
            row = conn.execute(
                """INSERT INTO usage_counts (day, kind, used) VALUES (?, ?, 1)
                   ON CONFLICT (day, kind) DO UPDATE SET used = used + 1 WHERE ? OR used < ?
                   RETURNING used""",
                (day, kind, unlimited, limit),
            ).fetchone()
            if row is not None:
                return True, row["used"]
            used = conn.execute("SELECT used FROM usage_counts WHERE day = ? AND kind = ?", (day, kind)).fetchone()
        return False, used["used"] if used else 0

    def merge_usage(self, day: str, counts: Mapping[str, int]) -> None:
        with self._tx() as conn:
            conn.executemany(
                """INSERT INTO usage_counts (day, kind, used) VALUES (?, ?, ?)
                   ON CONFLICT (day, kind) DO UPDATE SET used = MAX(used, excluded.used)""",
                [(day, str(kind), max(0, int(n))) for kind, n in counts.items()],
            )

    # -- counts (read at scrape time by /metrics) ----------------------------------------
    def recommendation_counts(self) -> dict[Status, int]:
        """Recommendations per status; every status is present, zero when empty."""
        counts = dict.fromkeys(Status, 0)
        for r in self._query("SELECT status, COUNT(*) AS n FROM recommendations GROUP BY status"):
            counts[Status(r["status"])] = r["n"]
        return counts

    def approval_counts(self) -> dict[str, int]:
        """Approvals recorded in the audit trail, per source (``manual`` / ``standing``)."""
        counts = {"manual": 0, "standing": 0}
        rows = self._query(
            "SELECT COALESCE(source, 'manual') AS source, COUNT(*) AS n FROM recommendation_transitions"
            " WHERE to_status = ? GROUP BY COALESCE(source, 'manual')",
            (Status.APPROVED.value,),
        )
        for r in rows:
            counts[r["source"]] = r["n"]
        return counts

    def open_event_counts(self, *, now: datetime | None = None) -> dict[str, int]:
        """Open (unacked, unresolved, unexpired) events per severity; every severity is present."""
        counts = {sev.value: 0 for sev in Severity}
        rows = self._query(
            "SELECT severity, COUNT(*) AS n FROM events WHERE acked_at IS NULL AND resolved_at IS NULL"
            " AND (expires_at IS NULL OR expires_at > ?) GROUP BY severity",
            (_ts(_now(now)),),
        )
        for r in rows:
            counts[r["severity"]] = r["n"]
        return counts

    # -- retention -------------------------------------------------------------------
    def prune(self, older_than: datetime) -> dict[str, int]:
        """Delete events acked, resolved or expired before ``older_than``, reports and delivery records older
        than it, and usage counts of earlier days.

        Open events, recommendations and their audit trail are never pruned, nor is an event its source still
        reports (``last_seen`` since ``older_than``): pruning it would bring it back unacked, as news.
        """
        cutoff = _ts(older_than)
        with self._tx() as conn:
            events = conn.execute(
                """DELETE FROM events WHERE last_seen < ?
                       AND ((acked_at IS NOT NULL AND acked_at < ?)
                            OR (resolved_at IS NOT NULL AND resolved_at < ?)
                            OR (expires_at IS NOT NULL AND expires_at < ?))""",
                (cutoff, cutoff, cutoff, cutoff),
            ).rowcount
            reports = conn.execute("DELETE FROM reports WHERE generated_at < ?", (cutoff,)).rowcount
            deliveries = conn.execute("DELETE FROM deliveries WHERE at < ?", (cutoff,)).rowcount
            usage = conn.execute("DELETE FROM usage_counts WHERE day < ?", (older_than.date().isoformat(),)).rowcount
        if events or reports or deliveries or usage:
            log.info(
                "store.pruned", events=events, reports=reports, deliveries=deliveries, usage=usage, older_than=cutoff
            )
        return {"events": events, "reports": reports, "deliveries": deliveries, "usage": usage}


__all__ = ["MIGRATIONS", "SCHEMA_VERSION", "SqliteStore", "migrate"]
