"""Per-user preferences sb-support keeps between conversations (SB-1564).

Today one: the user's "my team", in their own words ("IFA Homegrown U15"). Slack
users have no MT identity (SB-1529), so this is how "how's my team doing?" gets
answered without a follow-up question. Public data only: the team text is put into
the question sb-support sends a specialist, never used as a credential.

Keyed by (channel, user_id): a Slack user id means nothing in another channel.
SQLite at `SB_SUPPORT_DB` (a PVC on k3s); in memory when unset, e.g. in tests.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import UTC, datetime

MAX_TEAM_CHARS = 120

SCHEMA = """
CREATE TABLE IF NOT EXISTS my_team (
    channel TEXT NOT NULL,
    user_id TEXT NOT NULL,
    team TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (channel, user_id)
)
"""


class PreferenceStore:
    def __init__(self, path: str | None = None) -> None:
        if path is None:
            path = os.environ.get("SB_SUPPORT_DB") or ":memory:"
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        with self._lock, self._db:
            self._db.execute(SCHEMA)

    def my_team(self, channel: str, user_id: str | None) -> str | None:
        if not user_id:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT team FROM my_team WHERE channel = ? AND user_id = ?", (channel, user_id)
            ).fetchone()
        return row[0] if row else None

    def set_my_team(self, channel: str, user_id: str, team: str) -> str:
        """Save and return the cleaned team text. Raises ValueError when it's empty."""
        team = " ".join(team.split())[:MAX_TEAM_CHARS]
        if not team:
            raise ValueError("empty team")
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO my_team (channel, user_id, team, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (channel, user_id) DO UPDATE SET team = excluded.team, "
                "updated_at = excluded.updated_at",
                (channel, user_id, team, now),
            )
        return team

    def forget_my_team(self, channel: str, user_id: str) -> bool:
        with self._lock, self._db:
            cursor = self._db.execute(
                "DELETE FROM my_team WHERE channel = ? AND user_id = ?", (channel, user_id)
            )
        return cursor.rowcount > 0
