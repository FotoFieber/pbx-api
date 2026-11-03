# SPDX-License-Identifier: Apache-2.0
"""Aggregations over calls."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from .models import Call, CallSummary, Counts, Direction, Outcome, SummaryGroup


def _add(counts: Counts, call: Call) -> None:
    counts.total += 1
    setattr(counts, call.direction.value, getattr(counts, call.direction.value) + 1)
    if call.outcome is Outcome.answered:
        counts.answered += 1
    if call.direction is Direction.inbound and call.outcome is Outcome.missed:
        counts.missed_inbound += 1
    counts.talk_sec += call.talk_sec


def _keys(call: Call, group_by: str | None) -> list[str]:
    if group_by == "day":
        return [call.started_at.date().isoformat()]
    if group_by == "line":
        return [call.line.id] if call.line else ["none"]
    if group_by == "extension":
        exts = {call.caller.extension_id, call.callee.extension_id, call.answered_by, *call.ringing_extensions}
        return sorted(e for e in exts if e) or ["none"]
    return []


def summarize(calls: Iterable[Call], start: datetime, end: datetime, group_by: str | None = None) -> CallSummary:
    totals = Counts()
    groups: dict[str, Counts] = {}
    for call in calls:
        _add(totals, call)
        for key in _keys(call, group_by):
            _add(groups.setdefault(key, Counts()), call)
    return CallSummary(
        from_=start,
        to=end,
        totals=totals,
        groups=[SummaryGroup(key=k, counts=v) for k, v in sorted(groups.items())],
    )
