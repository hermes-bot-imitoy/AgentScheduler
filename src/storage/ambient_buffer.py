"""AmbientBuffer — 潜意识暂存区 (subconscious event buffer).

Lives between Layer 1/2 filtering and the agent's conscious workflow.
Events that don't warrant immediate LLM attention are parked here
and summarized during shift-end."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.types import Event, FilterDecision, Priority

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS ambient_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    TEXT    NOT NULL,
    event_id    TEXT    NOT NULL,
    source      TEXT,
    event_type  TEXT,
    priority    INTEGER,
    payload     TEXT,
    salience    REAL,
    decision    TEXT,
    blocked_reason TEXT,
    timestamp   TEXT    NOT NULL,
    flushed     INTEGER DEFAULT 0   -- 0=pending, 1=flushed (read by shift-end)
);
CREATE INDEX IF NOT EXISTS idx_ambient_agent_flushed
    ON ambient_events(agent_id, flushed);
CREATE INDEX IF NOT EXISTS idx_ambient_agent_ts
    ON ambient_events(agent_id, timestamp);
"""


class AmbientBuffer:
    """Thread-safe SQLite-backed subconscious buffer.

    Usage:
        buf = AmbientBuffer(":memory:")   # or path to disk db
        buf.append("agent-1", event)
        events = buf.get_and_clear("agent-1")   # consumes pending
    """

    def __init__(self, db_path: str = ":memory:"):
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(DB_SCHEMA)
        self._conn.commit()

    # ── Public API ──────────────────────────────────────────

    def append(self, agent_id: str, event: Event) -> int:
        """Park an event in the ambient buffer (0-Token cost). Returns row id."""
        cursor = self._conn.execute(
            """INSERT INTO ambient_events
               (agent_id, event_id, source, event_type, priority,
                payload, salience, decision, blocked_reason, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                agent_id,
                event.id,
                event.source,
                event.event_type,
                int(event.priority),
                json.dumps(event.payload, ensure_ascii=False),
                event.salience_score,
                event.filter_decision.value,
                event.blocked_reason,
                event.timestamp.isoformat(),
            ),
        )
        self._conn.commit()
        return cursor.lastrowid or 0

    def get_and_clear(self, agent_id: str) -> list[Event]:
        """Atomically fetch all pending events for an agent and mark them flushed.

        Returns a list of reconstructed Event objects (or empty list).
        """
        cursor = self._conn.execute(
            "SELECT * FROM ambient_events WHERE agent_id = ? AND flushed = 0 ORDER BY timestamp",
            (agent_id,),
        )
        rows = cursor.fetchall()

        events: list[Event] = []
        for row in rows:
            events.append(
                Event(
                    id=row["event_id"],
                    source=row["source"] or "",
                    event_type=row["event_type"] or "",
                    priority=Priority(row["priority"]),
                    payload=json.loads(row["payload"] or "{}"),
                    timestamp=datetime.fromisoformat(row["timestamp"]),
                    salience_score=row["salience"] or 0.0,
                    filter_decision=FilterDecision(row["decision"] or "BLOCKED"),
                    blocked_reason=row["blocked_reason"] or "",
                )
            )

        # Mark as flushed
        if rows:
            self._conn.execute(
                "UPDATE ambient_events SET flushed = 1 WHERE agent_id = ? AND flushed = 0",
                (agent_id,),
            )
            self._conn.commit()

        return events

    def count_pending(self, agent_id: str) -> int:
        """How many unflushed events are parked for this agent?"""
        row = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM ambient_events WHERE agent_id = ? AND flushed = 0",
            (agent_id,),
        ).fetchone()
        return row["cnt"] if row else 0

    def close(self) -> None:
        self._conn.close()
