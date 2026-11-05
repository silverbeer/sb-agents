"""Answers for the signed-in MT user (SB-1529): a delegated token, per request.

MT's backend mints a short-lived token for the user (capped to read/my/self in mt-mcp)
and sends it with the chat message; sb-support forwards it to specialists that declare
`IDENTITY_EXTENSION` in their agent card, in the A2A request's `params.metadata`:

    {"urn:sb-agents:a2a:mt-identity:v1": {"token": "<delegated token>"}}

Here it is taken out of the request before ADK sees the metadata: ADK copies request
metadata into `RunConfig.custom_metadata`, and from there onto every event, the
session and the A2A replies. Instead the token lives in a ContextVar for the one
request (the converter and the runner run in the same task), and only reaches mt-mcp
as an `Authorization` header. It is never logged, stored, or put in a span.

No token → phase 1: the public API account (`MT_AGENT_MCP_TOKEN`, client `sb-agents`,
read only), and `get_my_team` is hidden.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from google.adk.a2a.converters.request_converter import (
    A2A_METADATA_KEY,
    AgentRunRequest,
    convert_a2a_request_to_agent_run_request,
)

IDENTITY_EXTENSION = "urn:sb-agents:a2a:mt-identity:v1"
USER_CLIENT = "sb-agents-user"  # for a delegated user token: capped to read/my/self
USER_TOOLS = ("get_my_team",)  # shown to the model only with a user token
MAX_TOKEN = 4096

USER_TOKEN: ContextVar[str | None] = ContextVar("mt_agent_user_token", default=None)


def take_token(metadata: dict[str, Any]) -> str | None:
    """Remove the identity entry from request metadata; return its token if well-formed."""
    entry = metadata.pop(IDENTITY_EXTENSION, None)
    token = entry.get("token") if isinstance(entry, dict) else None
    if isinstance(token, str) and 0 < len(token) <= MAX_TOKEN:
        return token
    return None


def request_converter(request: Any, part_converter: Any) -> AgentRunRequest:
    """ADK's converter, minus the identity entry, which goes to `USER_TOKEN` instead."""
    run_request = convert_a2a_request_to_agent_run_request(request, part_converter)
    token = None
    config = run_request.run_config
    metadata = (config.custom_metadata or {}).get(A2A_METADATA_KEY) if config else None
    if isinstance(metadata, dict):
        token = take_token(metadata)
        if not metadata:
            config.custom_metadata.pop(A2A_METADATA_KEY, None)
    USER_TOKEN.set(token)  # set (to None too): a request never inherits another's token
    return run_request


def mcp_headers(_context: Any = None) -> dict[str, str]:
    """ADK `header_provider`: with a user token, replace the toolset's static public
    headers (same names, so these win in ADK's merge); without one, add nothing."""
    token = USER_TOKEN.get()
    if token:
        return {"Authorization": f"Bearer {token}", "X-MT-Client": USER_CLIENT}
    return {}


def signed_in() -> bool:
    return USER_TOKEN.get() is not None
