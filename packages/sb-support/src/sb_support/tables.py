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
    highlight: list[int] = field(default_factory=list)  # row indexes to emphasize (SB-1535)


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
                notes=_notes(raw.get("notes")),
                highlight=_highlight(raw.get("highlight"), len(rows)),
            )
        )
    return out


def _cell(value: Any) -> str:
    # a2a-sdk sends numbers as floats; a whole number reads better without ".0".
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return "" if value is None else str(value)


def _notes(value: Any) -> list[str]:
    return [_cell(n) for n in value if n not in (None, "")] if isinstance(value, list) else []


def _highlight(value: Any, n_rows: int) -> list[int]:
    """Valid row indexes only; a2a-sdk turns ints into floats on the way."""
    if not isinstance(value, list):
        return []
    out = []
    for i in value:
        if isinstance(i, (int, float)) and not isinstance(i, bool) and float(i).is_integer():
            if 0 <= int(i) < n_rows:
                out.append(int(i))
    return out
