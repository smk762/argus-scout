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
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from argus_scout.approvals import StandingRule
from argus_scout.events import StatusEvent, parse_dt
from argus_scout.recommendations import Recommendation, Status
from argus_scout.store.base import InboxEntry, Report, Transition

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

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
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
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

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
        )

    def upsert_event(self, event: StatusEvent, *, now: datetime | None = None) -> bool:
        """Insert or refresh by ``(source, event_id)``; True when it was new.

        A refresh updates the content and ``last_seen`` and keeps ``first_seen``.
        The ack and announce bookkeeping are kept while the fingerprint is
        unchanged; a changed fingerprint (e.g. a severity change under the same
        id) is new news, so it reopens the event and clears its announce state.
        """
        seen = _ts(_now(now))
        data = json.dumps(event.to_dict())
        fp = event.fingerprint
        with self._tx() as conn:
            row = conn.execute(
                "SELECT fingerprint FROM events WHERE source = ? AND event_id = ?", (event.source, event.event_id)
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
            if row["fingerprint"] != fp:
                reset = ", acked_at = NULL, acked_by = NULL, last_announced_at = NULL, broadcast_count = 0"
            conn.execute(
                f"""UPDATE events SET fingerprint = ?, severity = ?, generated_at = ?, expires_at = ?,
                        last_seen = ?, seen_count = seen_count + 1, data = ?{reset}
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

    def open_events(self, *, now: datetime | None = None, include_expired: bool = False) -> list[StatusEvent]:
        """Unacked events, oldest first; expired ones are left out unless asked for."""
        sql = "SELECT * FROM events WHERE acked_at IS NULL"
        params: list[Any] = []
        if not include_expired:
            sql += " AND (expires_at IS NULL OR expires_at > ?)"
            params.append(_ts(_now(now)))
        return [self._event_from_row(r) for r in self._query(sql + " ORDER BY first_seen, rowid", params)]

    def events_by_fingerprint(self, fingerprint: str, *, open_only: bool = True) -> list[StatusEvent]:
        """Every event with this content identity (see :attr:`StatusEvent.fingerprint`), oldest first."""
        sql = "SELECT * FROM events WHERE fingerprint = ?"
        if open_only:
            sql += " AND acked_at IS NULL"
        return [self._event_from_row(r) for r in self._query(sql + " ORDER BY first_seen, rowid", (fingerprint,))]

    def open_fingerprints(self) -> dict[str, int]:
        """Open events counted per fingerprint: the cheap duplicate check."""
        rows = self._query("SELECT fingerprint, COUNT(*) AS n FROM events WHERE acked_at IS NULL GROUP BY fingerprint")
        return {r["fingerprint"]: r["n"] for r in rows}

    # -- recommendations ---------------------------------------------------------
    def save_recommendation(self, rec: Recommendation, *, by: str | None = None, reason: str | None = None) -> None:
        """Insert or update ``rec``. A new row or a status change appends a transition.

        ``by`` names who made the change (a standing approval defaults to
        ``rule:<id>``); ``reason`` defaults to the rejection reason on a reject.
        """
        with self._tx() as conn:
            row = conn.execute("SELECT status FROM recommendations WHERE id = ?", (rec.id,)).fetchone()
            prior = Status(row["status"]) if row else None
            conn.execute(
                """INSERT INTO recommendations (id, status, analyzer, created_at, updated_at, data)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (id) DO UPDATE SET status = excluded.status, analyzer = excluded.analyzer,
                       updated_at = excluded.updated_at, data = excluded.data""",
                (
                    rec.id,
                    rec.status.value,
                    rec.analyzer,
                    _ts(rec.created_at),
                    _ts(rec.updated_at),
                    json.dumps(rec.to_dict()),
                ),
            )
            if prior == rec.status:
                return
            approved = rec.status == Status.APPROVED
            source = rec.approval_source if approved else None
            rule_id = rec.approved_by_rule if approved else None
            if by is None and source == "standing" and rule_id:
                by = f"rule:{rule_id}"
            if reason is None:
                reason = rec.rejection_reason if rec.status == Status.REJECTED else ""
            at = (rec.approved_at if approved else None) or rec.updated_at
            conn.execute(
                """INSERT INTO recommendation_transitions
                       (rec_id, from_status, to_status, at, by, source, rule_id, reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (rec.id, prior.value if prior else None, rec.status.value, _ts(at), by, source, rule_id, reason),
            )

    def get_recommendation(self, rec_id: str) -> Recommendation | None:
        rows = self._query("SELECT data FROM recommendations WHERE id = ?", (rec_id,))
        return Recommendation.from_dict(json.loads(rows[0]["data"])) if rows else None

    def list_recommendations(
        self, status: Status | Iterable[Status] | None = None, *, limit: int | None = None
    ) -> list[Recommendation]:
        """Recommendations, oldest first, optionally filtered to one or more statuses."""
        sql = "SELECT data FROM recommendations"
        params: list[Any] = []
        if status is not None:
            statuses = [status] if isinstance(status, str) else list(status)
            sql += f" WHERE status IN ({', '.join('?' * len(statuses))})"
            params.extend(Status(s).value for s in statuses)
        sql += " ORDER BY created_at, id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [Recommendation.from_dict(json.loads(r["data"])) for r in self._query(sql, params)]

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

    # -- retention -------------------------------------------------------------------
    def prune(self, older_than: datetime) -> dict[str, int]:
        """Delete events acked (or expired) before ``older_than`` and reports generated before it.

        Open events, recommendations and their audit trail are never pruned.
        """
        cutoff = _ts(older_than)
        with self._tx() as conn:
            events = conn.execute(
                """DELETE FROM events WHERE (acked_at IS NOT NULL AND acked_at < ?)
                       OR (expires_at IS NOT NULL AND expires_at < ?)""",
                (cutoff, cutoff),
            ).rowcount
            reports = conn.execute("DELETE FROM reports WHERE generated_at < ?", (cutoff,)).rowcount
        if events or reports:
            log.info("store.pruned", events=events, reports=reports, older_than=cutoff)
        return {"events": events, "reports": reports}


__all__ = ["MIGRATIONS", "SCHEMA_VERSION", "SqliteStore", "migrate"]
