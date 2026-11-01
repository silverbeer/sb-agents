"""httpx client for agent-to-agent calls that always carries the current trace.

The httpx instrumentation injects `traceparent`, but only on real network
transports. Injecting in a request hook as well makes propagation explicit and
transport-independent (it also works over `httpx.ASGITransport` in tests).
When both run, the instrumentation's header wins and points at its client span,
which is the more precise parent.
"""

from __future__ import annotations

from typing import Any

import httpx
from opentelemetry import propagate


async def _inject_trace(request: httpx.Request) -> None:
    propagate.inject(request.headers)


def traced_http_client(**kwargs: Any) -> httpx.AsyncClient:
    hooks = kwargs.pop("event_hooks", {}) or {}
    hooks.setdefault("request", []).append(_inject_trace)
    return httpx.AsyncClient(event_hooks=hooks, **kwargs)
