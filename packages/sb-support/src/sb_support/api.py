"""MT app adapter (SB-1500): an HTTP front door to the support core for MissingTable.

    SB_SUPPORT_API_TOKEN=... uv run sb-support-api      # http://127.0.0.1:8103

MT's backend owns the user, their auth and the conversation record (`ai_messages`);
it calls this with its service token and the MT conversation id, and stores the reply.

    POST /v1/chat   Authorization: Bearer $SB_SUPPORT_API_TOKEN
        {"conversation_id": "<MT conversation uuid>", "user_id": "<MT user id>", "text": "..."}
    →   {"text", "outcome", "escalation", "routed_to", "tables"}

    GET /healthz    no auth; specialists discovered at startup

Same core as Slack: channel "mt-app", so the RunRecord's caller is "mt-app" and memory
is keyed `mt-app:<conversation id>`. An incoming `traceparent` is continued, so MT
backend → core → specialist → mt-mcp is one trace.
"""

from __future__ import annotations

import hmac
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any

from sb_models import ModelConfigError, resolve
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from sb_obs import instrument_asgi, setup_telemetry
from sb_support.agent import build_agent
from sb_support.core import InboundMessage, Reply, SupportCore
from sb_support.specialists import discover

log = logging.getLogger("sb_support.api")

CHANNEL = "mt-app"
DEFAULT_HOST, DEFAULT_PORT = "127.0.0.1", 8103
MAX_TEXT = 2000  # MT's own limit on a chat message
MAX_ID = 128


def reply_body(reply: Reply) -> dict[str, Any]:
    return {
        "text": reply.text,
        "outcome": reply.outcome,
        "escalation": reply.escalation,
        "routed_to": reply.routed_to,
        "tables": [asdict(t) for t in reply.tables],
    }


def _bad(detail: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


def parse_message(body: Any) -> InboundMessage | str:
    """The request body → an InboundMessage, or what's wrong with it."""
    if not isinstance(body, dict):
        return "body must be a JSON object"
    conversation_id, user_id, text = (
        body.get("conversation_id"),
        body.get("user_id"),
        body.get("text"),
    )
    if not isinstance(conversation_id, str) or not 0 < len(conversation_id) <= MAX_ID:
        return "conversation_id is required"
    if user_id is not None and (not isinstance(user_id, str) or not 0 < len(user_id) <= MAX_ID):
        return "user_id must be a non-empty string"
    if not isinstance(text, str) or not text.strip():
        return "text is required"
    if len(text) > MAX_TEXT:
        return f"text is over {MAX_TEXT} characters"
    return InboundMessage(CHANNEL, conversation_id, text.strip(), user_id=user_id)


def build_app(core: SupportCore | None = None, token: str | None = None) -> Starlette:
    """`core=None` discovers the specialists at startup. `token` defaults to the env."""
    token = token if token is not None else os.environ.get("SB_SUPPORT_API_TOKEN", "")
    if not token:
        raise ValueError("SB_SUPPORT_API_TOKEN is required")
    expected = f"Bearer {token}".encode()
    state: dict[str, SupportCore] = {}

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        if core is not None:
            state["core"] = core
        else:
            specialists = await discover()
            log.info("specialists: %s", [s.name for s in specialists] or "none")
            state["core"] = SupportCore(build_agent(specialists=specialists))
        yield

    async def chat(request: Request) -> JSONResponse:
        given = request.headers.get("authorization", "").encode()
        if not hmac.compare_digest(given, expected):
            return _bad("unauthorized", 401)
        try:
            body = await request.json()
        except ValueError:
            return _bad("body must be JSON")
        msg = parse_message(body)
        if isinstance(msg, str):
            return _bad(msg)
        reply = await state["core"].handle(msg)  # never raises: errors are an outcome
        return JSONResponse(reply_body(reply))

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok" if "core" in state else "starting"})

    return Starlette(
        routes=[Route("/v1/chat", chat, methods=["POST"]), Route("/healthz", healthz)],
        lifespan=lifespan,
    )


def main() -> None:
    import uvicorn

    setup_telemetry("sb-support-api")
    if not os.environ.get("SB_SUPPORT_API_TOKEN"):
        raise SystemExit("missing env: SB_SUPPORT_API_TOKEN")
    try:
        resolve("sb-support")
    except ModelConfigError as exc:
        raise SystemExit(str(exc)) from exc
    host = os.environ.get("SB_SUPPORT_API_HOST", DEFAULT_HOST)
    port = int(os.environ.get("SB_SUPPORT_API_PORT", DEFAULT_PORT))
    uvicorn.run(instrument_asgi(build_app()), host=host, port=port, log_config=None)
