"""Bounded ADK sessions (SB-1525): reuse per context, evict by count and idle time."""

from mt_agent.sessions import BoundedSessionService


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def new(svc: BoundedSessionService, sid: str):
    return await svc.create_session(app_name="a", user_id=f"A2A_USER_{sid}", session_id=sid)


async def get(svc: BoundedSessionService, sid: str):
    return await svc.get_session(app_name="a", user_id=f"A2A_USER_{sid}", session_id=sid)


async def test_oldest_session_goes_first_when_full() -> None:
    svc = BoundedSessionService(max_sessions=2)
    await new(svc, "s1")
    await new(svc, "s2")
    await get(svc, "s1")  # s1 is now the most recent
    await new(svc, "s3")
    assert len(svc) == 2
    assert await get(svc, "s2") is None
    assert await get(svc, "s1") is not None
    assert "A2A_USER_s2" not in svc.sessions["a"]  # no empty per-context user shells


async def test_idle_sessions_expire() -> None:
    clock = Clock()
    svc = BoundedSessionService(max_idle_s=60, clock=clock)
    await new(svc, "old")
    clock.now = 30
    await new(svc, "fresh")
    clock.now = 75
    assert await get(svc, "old") is None
    assert await get(svc, "fresh") is not None
    assert len(svc) == 1


async def test_delete_session_forgets_it() -> None:
    svc = BoundedSessionService()
    await new(svc, "s1")
    await svc.delete_session(app_name="a", user_id="A2A_USER_s1", session_id="s1")
    assert len(svc) == 0 and await get(svc, "s1") is None
