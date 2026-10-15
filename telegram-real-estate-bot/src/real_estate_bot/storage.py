"""SQLite persistence for listing history and notification status.

Statuses: pending -> sent | failed | expired ; baseline = seen but intentionally never notified.
All state changes happen inside transactions, so a failed notification can never be recorded as sent.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Listing

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    key           TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    source_id     TEXT NOT NULL,
    url           TEXT NOT NULL,
    title         TEXT,
    price_ils     INTEGER,
    payload       TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('pending','sent','failed','baseline','expired')),
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    published_at  TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    sent_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_listings_status ON listings(status);
CREATE INDEX IF NOT EXISTS idx_listings_last_seen ON listings(last_seen_at);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Storage:
    def __init__(self, path: str | Path, clock: Callable[[], datetime] = _utcnow):
        self._clock = clock
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def _now(self) -> str:
        return self._clock().isoformat(timespec="seconds")

    # -- writes ---------------------------------------------------------------------------------

    def register(self, listing: Listing, status: str = "pending") -> bool:
        """Record a matching listing. Returns True only if it has never been seen before."""
        now = self._now()
        payload = json.dumps(listing.to_dict(), ensure_ascii=False)
        published = listing.published_at.isoformat() if listing.published_at else None
        with self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO listings (key, source, source_id, url, title, price_ils, payload, status,"
                " published_at, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (listing.key, listing.source, listing.source_id, listing.url, listing.title, listing.price_ils,
                 payload, status, published, now, now),
            )
            if cur.rowcount == 1:
                return True
            # Already known: refresh what may legitimately change (URL, data) without touching the status.
            self._conn.execute(
                "UPDATE listings SET last_seen_at=?, url=?, title=?, price_ils=?, payload=? WHERE key=?",
                (now, listing.url, listing.title, listing.price_ils, payload, listing.key),
            )
            if status == "baseline":
                self._conn.execute("UPDATE listings SET status='baseline' WHERE key=? AND status='pending'", (listing.key,))
            return False

    def mark_sent(self, key: str, note: str | None = None) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE listings SET status='sent', sent_at=?, attempts=attempts+1, last_error=? WHERE key=?",
                (self._now(), note, key),
            )

    def record_failure(self, key: str, error: str, max_attempts: int) -> str:
        """Count a failed send. After max_attempts the listing is parked as 'failed'. Returns new status."""
        with self._conn:
            self._conn.execute(
                "UPDATE listings SET attempts=attempts+1, last_error=? WHERE key=?", (error[:500], key)
            )
            row = self._conn.execute("SELECT attempts FROM listings WHERE key=?", (key,)).fetchone()
            status = "failed" if row and row["attempts"] >= max_attempts else "pending"
            self._conn.execute("UPDATE listings SET status=? WHERE key=?", (status, key))
        return status

    def expire_pending(self, max_age_days: int) -> int:
        cutoff = (self._clock() - timedelta(days=max_age_days)).isoformat(timespec="seconds")
        with self._conn:
            cur = self._conn.execute(
                "UPDATE listings SET status='expired' WHERE status='pending' AND first_seen_at < ?", (cutoff,)
            )
        return cur.rowcount

    def purge(self, retention_days: int) -> int:
        """Delete history for listings not seen on the source for retention_days (pending ones are kept)."""
        cutoff = (self._clock() - timedelta(days=retention_days)).isoformat(timespec="seconds")
        with self._conn:
            cur = self._conn.execute("DELETE FROM listings WHERE last_seen_at < ? AND status != 'pending'", (cutoff,))
        return cur.rowcount

    def set_meta(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                               (key, value))

    # -- reads ----------------------------------------------------------------------------------

    def pending(self, limit: int | None = None) -> list[Listing]:
        sql = "SELECT payload FROM listings WHERE status='pending' ORDER BY COALESCE(published_at, first_seen_at) DESC, key"
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        return [Listing.from_dict(json.loads(r["payload"])) for r in self._conn.execute(sql, params)]

    def pending_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM listings WHERE status='pending'").fetchone()[0]

    def status_of(self, key: str) -> str | None:
        row = self._conn.execute("SELECT status FROM listings WHERE key=?", (key,)).fetchone()
        return row["status"] if row else None

    def attempts_of(self, key: str) -> int:
        row = self._conn.execute("SELECT attempts FROM listings WHERE key=?", (key,)).fetchone()
        return row["attempts"] if row else 0

    def counts(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT status, COUNT(*) AS n FROM listings GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None
