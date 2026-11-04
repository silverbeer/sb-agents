"""Bounded in-memory ADK sessions: one per A2A conversation (SB-1525).

ADK's A2A executor maps the request's `contextId` to an ADK session id (and, for an
unauthenticated caller, the user id to `A2A_USER_<contextId>`), creating the session on
the first message and reusing it after. So a caller that sends the same `contextId`
gets a multi-turn conversation for free; a caller that sends none gets a fresh
server-generated context, and a new session, every time.

`InMemorySessionService` never forgets either kind, so the process grew by one session
per question. This one keeps at most `max_sessions`, drops any idle for longer than
`max_idle_s`, and is lost on restart (fine: a follow-up after a restart just starts
over).
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from google.adk.sessions import InMemorySessionService, Session

MAX_SESSIONS = 500
MAX_IDLE_S = 30 * 60

Key = tuple[str, str, str]  # app_name, user_id, session_id


class BoundedSessionService(InMemorySessionService):
    def __init__(
        self,
        max_sessions: int = MAX_SESSIONS,
        max_idle_s: float = MAX_IDLE_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        self.max_sessions, self.max_idle_s, self.clock = max_sessions, max_idle_s, clock
        self._seen: OrderedDict[Key, float] = OrderedDict()  # oldest access first

    def __len__(self) -> int:
        return len(self._seen)

    async def create_session(self, *, app_name: str, user_id: str, **kwargs: Any) -> Session:
        self._evict()
        session = await super().create_session(app_name=app_name, user_id=user_id, **kwargs)
        self._touch((app_name, user_id, session.id))
        while len(self._seen) > self.max_sessions:
            self._drop(next(iter(self._seen)))
        return session

    async def get_session(
        self, *, app_name: str, user_id: str, session_id: str, **kwargs: Any
    ) -> Session | None:
        self._evict()
        session = await super().get_session(
            app_name=app_name, user_id=user_id, session_id=session_id, **kwargs
        )
        if session is not None:
            self._touch((app_name, user_id, session_id))
        return session

    async def delete_session(self, *, app_name: str, user_id: str, session_id: str) -> None:
        self._drop((app_name, user_id, session_id))

    def _touch(self, key: Key) -> None:
        self._seen[key] = self.clock()
        self._seen.move_to_end(key)

    def _evict(self) -> None:
        cutoff = self.clock() - self.max_idle_s
        while self._seen and next(iter(self._seen.values())) < cutoff:
            self._drop(next(iter(self._seen)))

    def _drop(self, key: Key) -> None:
        self._seen.pop(key, None)
        app_name, user_id, session_id = key
        self._delete_session_impl(app_name=app_name, user_id=user_id, session_id=session_id)
        # A2A users are per conversation (`A2A_USER_<contextId>`): drop the empty shells.
        users = self.sessions.get(app_name, {})
        if user_id in users and not users[user_id]:
            del users[user_id]
            self.user_state.get(app_name, {}).pop(user_id, None)
