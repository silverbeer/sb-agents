"""The MOTW suggestion contract between motw-agent (writes it) and sb-support (reads it).

Two packages, no shared code: this test is what keeps them agreeing (SB-1545, SB-1546).
"""

from motw_agent import fixtures
from motw_agent.slack_post import draft_text as agent_draft_text
from motw_agent.slack_post import suggestion_message
from motw_agent.writer import Written as AgentWritten
from motw_agent.writer import fallback_draft
from sb_support.motw import Suggestion, draft_text
from sb_support.slack_motw import PICK_ACTION, SKIP_ACTION


def posted() -> dict:
    result = fixtures.result("top-clash", "low-sample", "unconfirmed-rule")
    written = [
        (c, AgentWritten(fallback_draft(c), fallback=False)) for c in result.candidates or []
    ]
    return suggestion_message(result.week_start, result, written)


def test_sb_support_reads_what_motw_agent_posts() -> None:
    msg = posted()
    s = Suggestion.from_metadata(msg["metadata"])
    assert s is not None
    assert s.week_start == "2026-11-09" and s.status == "open"
    assert [(c.n, c.match_id) for c in s.candidates] == [(1, 4096), (2, 5120), (3, 6200)]
    assert all(c.reasons and c.blurb for c in s.candidates)


def test_block_ids_and_action_ids_match() -> None:
    msg = posted()
    ids = {b.get("block_id") for b in msg["blocks"]}
    assert {"motw_actions", "motw_draft_1", "motw_draft_2", "motw_draft_3"} <= ids
    actions = next(b for b in msg["blocks"] if b.get("block_id") == "motw_actions")
    action_ids = [e["action_id"] for e in actions["elements"]]
    assert all(PICK_ACTION.match(a) for a in action_ids[:-1]) and action_ids[-1] == SKIP_ACTION


def test_an_edited_draft_renders_like_the_original() -> None:
    s = Suggestion.from_metadata(posted()["metadata"])
    assert s is not None
    for c in s.candidates:
        reasons = [r.as_dict() for r in c.reasons]
        assert draft_text(c) == agent_draft_text(c.n, c.blurb, reasons, fallback=False)
