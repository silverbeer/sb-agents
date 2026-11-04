"""Typed tables from a specialist's data part (SB-1528), channel-neutral.

A specialist may send `tables: [{title, columns, rows}]` in its A2A data part (mt-agent
builds them from its tool results). They ride on the `Reply`; each channel adapter
decides how to show them (Slack: a Block Kit table). Anything malformed is dropped: a
table is an extra, the text part is the answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Table:
    title: str
    columns: list[str]
    rows: list[list[str]]
    notes: list[str] = field(default_factory=list)  # the specialist's trend lines (SB-1533)


def parse_tables(data: Any) -> list[Table]:
    if not isinstance(data, dict) or not isinstance(data.get("tables"), list):
        return []
    out = []
    for raw in data["tables"]:
        if not isinstance(raw, dict):
            continue
        columns, rows = raw.get("columns"), raw.get("rows")
        if not isinstance(columns, list) or not isinstance(rows, list) or not columns:
            continue
        width = len(columns)
        if not all(isinstance(r, list) and len(r) == width for r in rows):
            continue
        out.append(
            Table(
                title=str(raw.get("title") or ""),
                columns=[_cell(c) for c in columns],
                rows=[[_cell(c) for c in r] for r in rows],
                notes=[str(n) for n in raw.get("notes") or [] if n]
                if isinstance(raw.get("notes"), list)
                else [],
            )
        )
    return out


def _cell(value: Any) -> str:
    # a2a-sdk sends numbers as floats; a whole number reads better without ".0".
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return "" if value is None else str(value)
