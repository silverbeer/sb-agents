"""docs-agent CLI: `ask` a question, or run the `eval` set."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

from docs_agent.agent import Deps, WithDeps, build_agent
from docs_agent.corpus import Corpus, KeywordRetriever
from sb_obs import setup_telemetry


def _ask(question: str, show_tools: bool) -> int:
    corpus = Corpus.load()
    deps = Deps(corpus, KeywordRetriever(corpus))
    start = time.perf_counter()
    result = WithDeps(build_agent(), deps, caller="cli").run_sync(question)
    out = result.output

    if show_tools:
        for msg in result.all_messages():
            for part in getattr(msg, "parts", ()):
                if getattr(part, "part_kind", "") == "tool-call":
                    print(f"  → {part.tool_name}({part.args})", file=sys.stderr)

    print(out.answer)
    print(f"\nconfidence: {out.confidence}")
    for s in out.sources:
        print(f"  - {s.ref}" + (f" § {s.heading}" if s.heading else ""))
    usage = result.usage
    print(
        f"\n[{time.perf_counter() - start:.1f}s, {usage.requests} requests, "
        f"{usage.input_tokens} in / {usage.output_tokens} out tokens]",
        file=sys.stderr,
    )
    return 0


def _eval() -> int:
    from docs_agent.evals import PASS_THRESHOLD, run_evals

    rate = asyncio.run(run_evals())
    return 0 if rate >= PASS_THRESHOLD else 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="docs-agent", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    ask = sub.add_parser("ask", help="answer a question from SB docs")
    ask.add_argument("question")
    ask.add_argument("-v", "--verbose", action="store_true", help="show tool calls")
    ev = sub.add_parser("eval", help="run the eval set against the real model")
    ev.add_argument(
        "--model",
        help="agent model, e.g. ollama:gemma4:12b or openrouter:<id> (default: DOCS_AGENT_MODEL)",
    )
    args = parser.parse_args()
    if args.cmd == "eval" and args.model:
        os.environ["DOCS_AGENT_MODEL"] = args.model
    setup_telemetry("docs-agent")

    if args.cmd == "ask":
        raise SystemExit(_ask(args.question, args.verbose))
    raise SystemExit(_eval())
