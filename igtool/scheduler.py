"""Post queue stored in SQLite and a loop that publishes due posts."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("igtool.scheduler")

STATUS_PENDING = "pending"
STATUS_PUBLISHING = "publishing"
STATUS_PUBLISHED = "published"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scheduled_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    media_urls TEXT NOT NULL,
    caption TEXT,
    options TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    container_id TEXT,
    media_id TEXT,
    created_at TEXT NOT NULL,
    published_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_scheduled_posts_due ON scheduled_posts (status, scheduled_at);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("scheduled_at must be timezone-aware")
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def from_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class ScheduledPost:
    scheduled_at: datetime
    kind: str
    media_urls: List[str]
    caption: Optional[str] = None
    options: Dict[str, Any] = field(default_factory=dict)
    id: Optional[int] = None
    status: str = STATUS_PENDING
    attempts: int = 0
    last_error: Optional[str] = None
    container_id: Optional[str] = None
    media_id: Optional[str] = None
    created_at: Optional[datetime] = None
    published_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ScheduledPost":
        return cls(
            id=row["id"],
            scheduled_at=from_iso(row["scheduled_at"]),
            kind=row["kind"],
            media_urls=json.loads(row["media_urls"]),
            caption=row["caption"],
            options=json.loads(row["options"] or "{}"),
            status=row["status"],
            attempts=row["attempts"],
            last_error=row["last_error"],
            container_id=row["container_id"],
            media_id=row["media_id"],
            created_at=from_iso(row["created_at"]) if row["created_at"] else None,
            published_at=from_iso(row["published_at"]) if row["published_at"] else None,
        )


class ScheduleStore:
    def __init__(self, path: str = "igtool.db"):
        self.path = path
        self.conn = sqlite3.connect(path, detect_types=0)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def add(self, post: ScheduledPost) -> int:
        cur = self.conn.execute(
            "INSERT INTO scheduled_posts (scheduled_at, kind, media_urls, caption, options, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                to_iso(post.scheduled_at),
                post.kind,
                json.dumps(post.media_urls),
                post.caption,
                json.dumps(post.options or {}),
                STATUS_PENDING,
                to_iso(utcnow()),
            ),
        )
        self.conn.commit()
        post.id = cur.lastrowid
        return post.id

    def get(self, post_id: int) -> Optional[ScheduledPost]:
        row = self.conn.execute("SELECT * FROM scheduled_posts WHERE id = ?", (post_id,)).fetchone()
        return ScheduledPost.from_row(row) if row else None

    def list(self, status: Optional[str] = None) -> List[ScheduledPost]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM scheduled_posts WHERE status = ? ORDER BY scheduled_at", (status,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM scheduled_posts ORDER BY scheduled_at").fetchall()
        return [ScheduledPost.from_row(r) for r in rows]

    def due(self, now: Optional[datetime] = None) -> List[ScheduledPost]:
        now = now or utcnow()
        rows = self.conn.execute(
            "SELECT * FROM scheduled_posts WHERE status = ? AND scheduled_at <= ? ORDER BY scheduled_at",
            (STATUS_PENDING, to_iso(now)),
        ).fetchall()
        return [ScheduledPost.from_row(r) for r in rows]

    def cancel(self, post_id: int) -> bool:
        cur = self.conn.execute(
            "UPDATE scheduled_posts SET status = ? WHERE id = ? AND status = ?",
            (STATUS_CANCELLED, post_id, STATUS_PENDING),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def update(self, post_id: int, **fields: Any) -> None:
        if not fields:
            return
        for key in ("published_at",):
            if isinstance(fields.get(key), datetime):
                fields[key] = to_iso(fields[key])
        assignments = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(
            f"UPDATE scheduled_posts SET {assignments} WHERE id = ?", (*fields.values(), post_id)
        )
        self.conn.commit()


class Scheduler:
    """Publishes due posts. ``publisher`` needs ``publish_post(kind, urls, caption, options)``."""

    def __init__(
        self,
        store: ScheduleStore,
        publisher: Any,
        max_attempts: int = 3,
        now: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.store = store
        self.publisher = publisher
        self.max_attempts = max_attempts
        self._now = now
        self._sleep = sleep

    def run_once(self) -> List[ScheduledPost]:
        """Publish everything that is due. Returns the posts that were attempted."""
        handled = []
        for post in self.store.due(self._now()):
            self.store.update(post.id, status=STATUS_PUBLISHING, attempts=post.attempts + 1)
            try:
                result = self.publisher.publish_post(post.kind, post.media_urls, post.caption, post.options)
            except Exception as exc:  # noqa: BLE001 - we record and carry on
                attempts = post.attempts + 1
                status = STATUS_FAILED if attempts >= self.max_attempts else STATUS_PENDING
                log.error("post %s failed (attempt %d/%d): %s", post.id, attempts, self.max_attempts, exc)
                self.store.update(post.id, status=status, last_error=str(exc)[:1000])
            else:
                log.info("post %s published as media %s", post.id, result.get("media_id"))
                self.store.update(
                    post.id,
                    status=STATUS_PUBLISHED,
                    container_id=result.get("container_id"),
                    media_id=result.get("media_id"),
                    published_at=self._now(),
                    last_error=None,
                )
            handled.append(self.store.get(post.id))
        return handled

    def run_forever(self, interval: float = 60.0) -> None:
        log.info("scheduler running, checking every %.0fs", interval)
        while True:
            try:
                self.run_once()
            except Exception:  # noqa: BLE001
                log.exception("scheduler tick failed")
            self._sleep(interval)
