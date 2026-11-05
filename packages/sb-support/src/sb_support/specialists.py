"""Specialist agents reached over A2A, discovered from their agent cards.

`SB_SUPPORT_SPECIALISTS` is a comma-separated list of A2A base URLs. At startup each
card (`/.well-known/agent-card.json`) is fetched, and its name, description and skill
examples become one sb-support tool. Adding a specialist is a URL, not code.

A specialist's result artifact carries a text part (what a Slack reply wants) and a
data part `{confidence, sources, ...}`. `confidence` is read from the data part when
present; sb-support escalates on "low".

The client speaks A2A 1.0 JSON-RPC (`SendStreamingMessage`, `A2A-Version: 1.0`), which
both servers in use answer the same way: fasta2a (docs-agent) and a2a-sdk (ADK's
`to_a2a`, mt-agent). The older `message/stream` name gets a different shape from each:
a2a-sdk treats a request without the version header as A2A 0.3 and answers with
`kind`-tagged events; fasta2a with 1.0 objects but 0.3 enums. See docs/notes/mt-agent.md.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from sb_obs import traced_http_client

log = logging.getLogger("sb_support.specialists")

DEFAULT_URLS = "http://127.0.0.1:8101,http://127.0.0.1:8102"  # docs-agent, mt-agent
CARD_PATH = "/.well-known/agent-card.json"
A2A_VERSION = "1.0"
# A specialist that declares this in its card's `capabilities.extensions` keeps one
# conversation per contextId (SB-1525); it gets a stable contextId per conversation.
# Others get none: fasta2a (docs-agent) would replay the whole history, tool results
# included, for a reused context. Same URI as mt_agent.a2a_server.
CONVERSATION_EXTENSION = "urn:sb-agents:a2a:conversation:v1"
# A specialist that declares this answers as the signed-in MT user (SB-1529): it gets the
# user's delegated token in params.metadata. Nobody else ever sees the token. Same URI as
# mt_agent.identity.
IDENTITY_EXTENSION = "urn:sb-agents:a2a:mt-identity:v1"
FAILED_STATES = ("TASK_STATE_FAILED", "TASK_STATE_REJECTED", "TASK_STATE_CANCELED")


class SpecialistError(RuntimeError):
    pass


@dataclass
class SpecialistAnswer:
    text: str
    confidence: str | None = None
    data: dict[str, Any] | None = None


@dataclass
class SpecialistClient:
    """A2A 1.0 client: `SendStreamingMessage`, read until the result artifact arrives.

    Streaming, not `SendMessage`: fasta2a's server has no blocking send (it returns
    `submitted`). Events are `{"result": {"task"|"message"|"statusUpdate"|"artifactUpdate"}}`.
    """

    url: str
    http_client: httpx.AsyncClient | None = None
    timeout_s: float = 90.0

    async def ask(
        self,
        question: str,
        context_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SpecialistAnswer:
        """`context_id`: reuse for follow-ups in one conversation; None = a new context.
        `metadata`: the request's params.metadata (may hold a credential: never log it)."""
        url = self.url.rstrip("/")
        http = self.http_client or traced_http_client(timeout=self.timeout_s)
        message: dict[str, Any] = {
            "messageId": uuid.uuid4().hex,
            "role": "ROLE_USER",
            "parts": [{"text": question}],
        }
        if context_id:
            message["contextId"] = context_id
        request = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": "SendStreamingMessage",
            "params": {"message": message},
        }
        if metadata:
            request["params"]["metadata"] = metadata
        headers = {"A2A-Version": A2A_VERSION, "Accept": "text/event-stream"}
        answer = _Answer()
        try:
            async with http.stream("POST", url + "/", json=request, headers=headers) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise SpecialistError(f"HTTP {response.status_code}: {response.text[:200]}")
                if "text/event-stream" not in response.headers.get("content-type", ""):
                    await response.aread()  # a JSON-RPC error comes back as plain JSON
                    answer.read(response.json())
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        answer.read(json.loads(line[5:]))
        except httpx.HTTPError as exc:
            raise SpecialistError(f"unreachable at {url}: {exc}") from exc
        except ValueError as exc:
            raise SpecialistError(f"unreadable A2A response: {exc}") from exc
        finally:
            if self.http_client is None:
                await http.aclose()
        if answer.text is None:
            raise SpecialistError("returned no answer")
        data = answer.data
        confidence = data.get("confidence") if isinstance(data, dict) else None
        return SpecialistAnswer(text=answer.text, confidence=confidence, data=data)


@dataclass
class _Answer:
    """Folds A2A 1.0 stream events into the first non-empty text and the last data part."""

    text: str | None = None
    data: dict[str, Any] | None = None

    def read(self, event: dict[str, Any]) -> None:
        if error := event.get("error"):
            raise SpecialistError(error.get("message", "A2A error"))
        result = event.get("result") or {}
        if update := result.get("artifactUpdate"):
            self._parts(update["artifact"].get("parts") or [])
        if message := result.get("message"):  # a reply without a task
            self._parts(message.get("parts") or [])
        task = result.get("task") or {}
        for artifact in task.get("artifacts") or []:
            self._parts(artifact.get("parts") or [])
        status = (result.get("statusUpdate") or task).get("status") or {}
        if status.get("state") in FAILED_STATES:
            parts = (status.get("message") or {}).get("parts") or []
            why = next((p["text"] for p in parts if "text" in p), "")
            raise SpecialistError(f"task {status['state']}" + (f": {why[:200]}" if why else ""))

    def _parts(self, parts: list[dict[str, Any]]) -> None:
        # The first artifact (or message) with real text wins; its non-empty text parts
        # are joined. ADK can lead with an empty text part (Claude's empty block before
        # the answer), so "first text part" would read as an empty answer.
        texts = [p["text"] for p in parts if isinstance(p.get("text"), str) and p["text"].strip()]
        if texts and self.text is None:
            self.text = "\n".join(texts)
        for part in parts:
            if isinstance(part.get("data"), dict):
                # fasta2a wraps structured output as {"result": {...}}; ADK sends it bare.
                self.data = part["data"].get("result", part["data"])


@dataclass
class Specialist:
    name: str  # from the agent card, e.g. "docs-agent"
    description: str
    client: SpecialistClient
    examples: list[str] = field(default_factory=list)
    conversational: bool = False  # declares CONVERSATION_EXTENSION
    accepts_identity: bool = False  # declares IDENTITY_EXTENSION

    def identity(self, credential: str | None) -> dict[str, Any] | None:
        """params.metadata carrying the user's token, only for a specialist that declares it."""
        if not credential or not self.accepts_identity:
            return None
        return {IDENTITY_EXTENSION: {"token": credential}}

    def context_id(self, conversation_key: str | None) -> str | None:
        """Stable per (conversation, specialist), opaque: no channel or thread ids leak."""
        if not self.conversational or not conversation_key:
            return None
        return uuid.uuid5(uuid.NAMESPACE_URL, f"sb-support/{conversation_key}/{self.name}").hex

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
    extensions = (card.get("capabilities") or {}).get("extensions") or []
    return Specialist(
        name=card["name"],
        description=card.get("description", ""),
        client=SpecialistClient(url, http),
        examples=examples,
        conversational=any(e.get("uri") == CONVERSATION_EXTENSION for e in extensions),
        accepts_identity=any(e.get("uri") == IDENTITY_EXTENSION for e in extensions),
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
