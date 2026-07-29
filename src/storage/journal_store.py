"""JournalStore — 记事本/日记持久化模块.

Stores structured end-of-shift journal entries keyed by agent_id + date.
Uses JSON-file backend for simplicity and human readability."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from src.core.types import Journal


class JournalStore:
    """Flat-file journal storage.

    Directory layout:
        {data_dir}/
          {agent_id}/
            2026-07-29.json
            2026-07-28.json
    """

    def __init__(self, data_dir: str = "./data/journals"):
        self._data_dir = Path(data_dir)

    # ── Public API ──────────────────────────────────────────

    def save(self, journal: Journal) -> str:
        """Persist a journal entry. Returns the file path."""
        agent_dir = self._data_dir / journal.agent_id
        agent_dir.mkdir(parents=True, exist_ok=True)

        filepath = agent_dir / f"{journal.date}.json"
        payload = {
            "agent_id": journal.agent_id,
            "date": journal.date,
            "created_at": journal.created_at.isoformat(),
            "summary": journal.summary,
            "key_decisions": journal.key_decisions,
            "pending_tasks": journal.pending_tasks,
            "ambient_highlights": journal.ambient_highlights,
            "raw_log": journal.raw_log[:3000],  # truncate for storage
        }
        filepath.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return str(filepath)

    def load(self, agent_id: str, target_date: str) -> Optional[Journal]:
        """Load a journal by agent + date. Returns None if not found."""
        filepath = self._data_dir / agent_id / f"{target_date}.json"
        if not filepath.exists():
            return None

        data = json.loads(filepath.read_text(encoding="utf-8"))
        return Journal(
            agent_id=data["agent_id"],
            date=data["date"],
            created_at=datetime.fromisoformat(data["created_at"]),
            summary=data.get("summary", ""),
            key_decisions=data.get("key_decisions", []),
            pending_tasks=data.get("pending_tasks", []),
            ambient_highlights=data.get("ambient_highlights", []),
            raw_log=data.get("raw_log", ""),
        )

    def load_latest(self, agent_id: str, before_date: Optional[str] = None) -> Optional[Journal]:
        """Load the most recent journal for an agent (optionally before a given date).

        Args:
            agent_id: The agent to look up.
            before_date: ISO date string; if provided, finds the newest journal
                         strictly before this date. If None, returns the absolute latest.
        """
        agent_dir = self._data_dir / agent_id
        if not agent_dir.exists():
            return None

        files = sorted(agent_dir.glob("*.json"), reverse=True)
        for fp in files:
            file_date = fp.stem  # e.g. "2026-07-29"
            if before_date is None or file_date < before_date:
                return self.load(agent_id, file_date)
        return None

    def list_dates(self, agent_id: str) -> list[str]:
        """Return all journal dates for an agent, newest first."""
        agent_dir = self._data_dir / agent_id
        if not agent_dir.exists():
            return []
        return sorted(
            (fp.stem for fp in agent_dir.glob("*.json")),
            reverse=True,
        )
