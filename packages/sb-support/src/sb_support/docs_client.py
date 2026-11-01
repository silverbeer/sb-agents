"""Ask docs-agent over A2A (fasta2a client → fasta2a server).

docs-agent's result artifact carries a text part (answer, confidence, sources)
first and a data part second; this returns the text part, which is what a
Slack reply wants. fasta2a's server has no blocking `message/send`, so this
uses `message/stream` and waits for the final artifact, the same path ADK uses.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

import httpx
from fasta2a.client import A2AClient
from fasta2a.schema import Message, Part

from sb_obs import traced_http_client

DEFAULT_URL = "http://127.0.0.1:8101"


class DocsAgentError(RuntimeError):
    pass


@dataclass
class DocsClient:
    url: str | None = None
    http_client: httpx.AsyncClient | None = None
    timeout_s: float = 90.0

    async def ask(self, question: str) -> str:
        url = (self.url or os.environ.get("DOCS_AGENT_A2A_URL", DEFAULT_URL)).rstrip("/")
        http = self.http_client or traced_http_client(timeout=self.timeout_s)
        client = A2AClient(url, http_client=http)
        message = Message(role="user", parts=[Part(text=question)], message_id=uuid.uuid4().hex)
        text: str | None = None
        try:
            async for response in client.stream_message(message):
                if "error" in response:
                    raise DocsAgentError(response["error"].get("message", "A2A error"))
                result = response["result"]
                if update := result.get("artifact_update"):
                    texts = [p["text"] for p in update["artifact"]["parts"] if "text" in p]
                    if texts:
                        text = texts[0]
                if status := result.get("status_update"):
                    if status["status"]["state"] in ("failed", "rejected", "canceled"):
                        raise DocsAgentError(f"docs-agent task {status['status']['state']}")
        except httpx.HTTPError as exc:
            raise DocsAgentError(f"docs-agent unreachable at {url}: {exc}") from exc
        finally:
            if self.http_client is None:
                await http.aclose()
        if text is None:
            raise DocsAgentError("docs-agent returned no answer")
        return text
