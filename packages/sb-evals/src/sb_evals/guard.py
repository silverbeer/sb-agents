"""Run work that follows a real agent run, so it can never hurt that run."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger("sb_evals")


async def guarded[T](
    work: Callable[[], Awaitable[T]], *, timeout_s: float, fallback: T, label: str
) -> T:
    """`work()`, or `fallback` if it raises or runs past `timeout_s`. Never raises.

    Set `timeout_s` inside whatever deadline the host runs under, so a slow judge can't
    get the host killed. Task cancellation still propagates: it isn't a failure of `work`.
    """
    try:
        async with asyncio.timeout(timeout_s):
            return await work()
    except Exception:
        log.exception("%s failed", label)
        return fallback
