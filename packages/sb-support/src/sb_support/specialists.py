"""Specialist agents reached over A2A, discovered from their agent cards.

`SB_SUPPORT_SPECIALISTS` is a comma-separated list of A2A base URLs. At startup each
card (`/.well-known/agent-card.json`) is fetched, and its name, description and skill
examples become one sb-support tool. Adding a specialist is a URL, not code.

A specialist's result artifact carries a text part (what a Slack reply wants) and,
from PydanticAI agents, a data part with the structured output. `confidence` is read
from the data part when present; sb-support escalates on "low".
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from fasta2a.client import A2AClient
from fasta2a.schema import Message, Part

from sb_obs import traced_http_client

log = logging.getLogger("sb_support.specialists")

DEFAULT_URLS = "http://127.0.0.1:8101"  # docs-agent
CARD_PATH = "/.well-known/agent-card.json"


class SpecialistError(RuntimeError):
    pass


@dataclass
class SpecialistAnswer:
    text: str
    confidence: str | None = None
    data: dict[str, Any] | None = None


@dataclass
class SpecialistClient:
    """fasta2a A2A client. Uses `message/stream`: fasta2a's server has no blocking send."""

    url: str
    http_client: httpx.AsyncClient | None = None
    timeout_s: float = 90.0

    async def ask(self, question: str) -> SpecialistAnswer:
        url = self.url.rstrip("/")
        http = self.http_client or traced_http_client(timeout=self.timeout_s)
        client = A2AClient(url, http_client=http)
        message = Message(role="user", parts=[Part(text=question)], message_id=uuid.uuid4().hex)
        text: str | None = None
        data: dict[str, Any] | None = None
        try:
            async for response in client.stream_message(message):
                if "error" in response:
                    raise SpecialistError(response["error"].get("message", "A2A error"))
                result = response["result"]
                if update := result.get("artifact_update"):
                    for part in update["artifact"]["parts"]:
                        if "text" in part and text is None:
                            text = part["text"]
                        elif "data" in part:
                            data = part["data"].get("result", part["data"])
                if status := result.get("status_update"):
                    if status["status"]["state"] in ("failed", "rejected", "canceled"):
                        raise SpecialistError(f"task {status['status']['state']}")
        except httpx.HTTPError as exc:
            raise SpecialistError(f"unreachable at {url}: {exc}") from exc
        finally:
            if self.http_client is None:
                await http.aclose()
        if text is None:
            raise SpecialistError("returned no answer")
        confidence = data.get("confidence") if isinstance(data, dict) else None
        return SpecialistAnswer(text=text, confidence=confidence, data=data)


@dataclass
class Specialist:
    name: str  # from the agent card, e.g. "docs-agent"
    description: str
    client: SpecialistClient
    examples: list[str] = field(default_factory=list)

    @property
    def tool_name(self) -> str:
        return "ask_" + re.sub(r"[^a-z0-9]+", "_", self.name.lower()).strip("_")

    @property
    def tool_description(self) -> str:
        text = f"Ask the {self.name} specialist over A2A. {self.description}"
        if self.examples:
            text += " Example questions: " + "; ".join(self.examples[:3])
        return text


def specialist_from_card(
    card: dict[str, Any], url: str, http: httpx.AsyncClient | None
) -> Specialist:
    skills = card.get("skills") or []
    examples = [e for s in skills for e in (s.get("examples") or [])]
    return Specialist(
        name=card["name"],
        description=card.get("description", ""),
        client=SpecialistClient(url, http),
        examples=examples,
    )


async def discover(
    urls: list[str] | None = None, http: httpx.AsyncClient | None = None
) -> list[Specialist]:
    """Fetch each specialist's agent card. Unreachable ones are logged and skipped."""
    if urls is None:
        urls = [
            u.strip() for u in os.environ.get("SB_SUPPORT_SPECIALISTS", DEFAULT_URLS).split(",")
        ]
    found: list[Specialist] = []
    client = http or traced_http_client(timeout=10)
    try:
        for url in filter(None, urls):
            try:
                response = await client.get(url.rstrip("/") + CARD_PATH)
                response.raise_for_status()
                found.append(specialist_from_card(response.json(), url, http))
                log.info("specialist discovered", extra={"specialist": found[-1].name, "url": url})
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                log.warning("specialist unavailable at %s: %s", url, exc)
    finally:
        if http is None:
            await client.aclose()
    return found
