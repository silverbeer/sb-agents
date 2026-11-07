"""mt-mcp `suggest_motw_candidates` result, as this agent reads it (MT SB-1550/SB-1551).

Mirrors missing-table `backend/mt_tools/motw_candidates.py`. Lenient on purpose:
unknown fields are ignored, so MT can add fields without breaking the weekly run.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict

FactKind = Literal["standings", "form", "stakes", "rivalry", "storyline"]
FACT_KINDS: tuple[str, ...] = ("standings", "form", "stakes", "rivalry", "storyline")


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Ref(_Lenient):
    id: int
    name: str


class DivisionRef(Ref):
    league: str | None = None


class CandidateTeam(_Lenient):
    id: int
    name: str
    club_name: str | None = None
    position: int | None = None  # null: no row in the table yet
    table_size: int | None = None
    played: int = 0
    points: int | None = None
    strength: float = 0.0
    form: float | None = None  # null: no League/Flex results yet
    low_sample: bool = False


class Score(_Lenient):
    quality: float = 0.0
    closeness: float = 0.0
    stakes: float = 0.0
    form: float = 0.0
    penalty: float = 1.0
    total: float = 0.0


class MotwFact(_Lenient):
    id: str  # "<match_id>:<slug>", e.g. "4096:pos:home"
    kind: FactKind
    text: str
    team_id: int | None = None
    rule_id: str | None = None
    # Set on qualification-rule facts. False: may only be stated as unconfirmed.
    confirmed: bool | None = None

    @property
    def is_rule(self) -> bool:
        return self.rule_id is not None


class RuleRef(_Lenient):
    id: str
    pathway: str
    confirmed: bool
    summary: str
    source_url: str | None = None


class MotwCandidate(_Lenient):
    match_id: int
    match_date: date
    scheduled_kickoff: str | None = None
    age_group: Ref
    division: DivisionRef
    competition: str
    home: CandidateTeam
    away: CandidateTeam
    score: Score
    flags: list[str] = []
    facts: list[MotwFact] = []
    rules: list[RuleRef] = []

    @property
    def label(self) -> str:
        return f"{self.home.name} vs {self.away.name}"

    @property
    def table(self) -> str:
        league = f"{self.division.league} " if self.division.league else ""
        return f"{self.age_group.name} {league}{self.division.name}"

    def fact(self, fact_id: str) -> MotwFact | None:
        return next((f for f in self.facts if f.id == fact_id), None)


class CurrentPick(_Lenient):
    match_id: int
    blurb: str | None = None
    has_reasons: bool = False


class Coverage(_Lenient):
    divisions: int = 0
    fixtures_in_week: int = 0
    eligible: int = 0
    teams_low_sample: int = 0
    rules_season: str | None = None


class ToolError(_Lenient):
    kind: str
    message: str


class CandidatesResult(_Lenient):
    week_start: date
    week_end: date
    today: date | None = None
    season: Ref | None = None
    current_pick: CurrentPick | None = None  # the week is already picked: never overwrite
    candidates: list[MotwCandidate] | None = None  # [] + reason: nothing eligible
    reason: str | None = None
    coverage: Coverage | None = None
    error: ToolError | None = None  # MT failed; candidates is null
