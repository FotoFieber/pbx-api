# SPDX-License-Identifier: Apache-2.0
"""Turn raw CDR rows into business-level calls.

Asterisk writes one CDR row per call leg. A single call as a person experiences it
("Anna called our main number, it rang in the kitchen and the office, the office
picked up") can span several rows that share a ``linkedid``. Some gateways even
deliver one incoming call as two parallel calls with different linkedids; those are
merged when they start within a configurable window.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Any

from .models import Call, Direction, LineRef, Outcome, Party
from .topology import LineInfo, Topology

_OUTCOME_RANK = {
    Outcome.answered: 5,
    Outcome.busy: 4,
    Outcome.no_answer: 3,
    Outcome.missed: 3,
    Outcome.failed: 1,
}


@dataclass(frozen=True)
class CdrRow:
    calldate: datetime
    src: str
    dst: str
    dcontext: str
    channel: str
    dstchannel: str
    disposition: str
    duration: int
    billsec: int
    linkedid: str
    sequence: int
    did: str
    cnum: str
    cnam: str
    dst_cnam: str

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> CdrRow:
        def s(key: str) -> str:
            value = row.get(key)
            return "" if value is None else str(value)

        return cls(
            calldate=row["calldate"],
            src=s("src"),
            dst=s("dst"),
            dcontext=s("dcontext"),
            channel=s("channel"),
            dstchannel=s("dstchannel"),
            disposition=s("disposition").upper(),
            duration=int(row.get("duration") or 0),
            billsec=int(row.get("billsec") or 0),
            linkedid=s("linkedid") or s("uniqueid"),
            sequence=int(row.get("sequence") or 0),
            did=s("did"),
            cnum=s("cnum"),
            cnam=s("cnam"),
            dst_cnam=s("dst_cnam"),
        )


def build_calls(
    rows: Iterable[CdrRow | Mapping[str, Any]],
    topology: Topology,
    tz: tzinfo,
    dedup_window_sec: int = 2,
) -> list[Call]:
    """Build calls from CDR rows; newest first."""
    groups: dict[str, list[CdrRow]] = {}
    for raw in rows:
        row = raw if isinstance(raw, CdrRow) else CdrRow.from_mapping(raw)
        groups.setdefault(row.linkedid, []).append(row)

    calls = [_build_one(sorted(g, key=lambda r: (r.calldate, r.sequence)), topology, tz) for g in groups.values()]
    calls.sort(key=lambda c: (c.started_at, c.id))
    merged = _dedup(calls, dedup_window_sec)
    merged.sort(key=lambda c: (c.started_at, c.id), reverse=True)
    return merged


def _line_ref(line: LineInfo | None) -> LineRef | None:
    if not line:
        return None
    return LineRef(id=line.id, label=line.label, number=line.number)


def _ext_of_party(number: str, channel: str, topo: Topology) -> str | None:
    if number in topo.extensions:
        return number
    return topo.extension_for_channel(channel)


def _build_one(rows: list[CdrRow], topo: Topology, tz: tzinfo) -> Call:
    first = rows[0]

    inbound_line: LineInfo | None = None
    outbound_line: LineInfo | None = None
    for r in rows:
        line = topo.line_for_channel(r.channel)
        if line and not line.loop_context and inbound_line is None:
            inbound_line = line
        line = topo.line_for_channel(r.dstchannel)
        if line and outbound_line is None:
            outbound_line = line

    if inbound_line:
        direction, line = Direction.inbound, inbound_line
    elif outbound_line:
        direction, line = Direction.outbound, outbound_line
    else:
        direction, line = Direction.internal, None

    ringing: list[str] = []
    for r in rows:
        ext = topo.extension_for_channel(r.dstchannel)
        if ext and ext not in ringing:
            ringing.append(ext)

    answered_rows = [r for r in rows if r.disposition == "ANSWERED"]
    answered_by = next(
        (e for e in (topo.extension_for_channel(r.dstchannel) for r in answered_rows) if e), None
    )

    if answered_rows:
        outcome = Outcome.answered
    elif any(r.disposition == "BUSY" for r in rows):
        outcome = Outcome.busy
    elif any(r.disposition in ("FAILED", "CONGESTION") for r in rows):
        outcome = Outcome.failed
    else:
        outcome = Outcome.missed if direction is Direction.inbound else Outcome.no_answer

    started = min(r.calldate for r in rows).replace(tzinfo=tz)
    ended = max(r.calldate + timedelta(seconds=r.duration) for r in rows).replace(tzinfo=tz)
    talk = max((r.billsec for r in rows), default=0)
    if answered_rows:
        best = max(answered_rows, key=lambda r: r.billsec)
        ring = max(best.duration - best.billsec, 0)
    else:
        ring = max((r.duration for r in rows), default=0)

    if direction is Direction.inbound:
        name = first.cnam if first.cnam and first.cnam != first.src else None
        caller = Party(number=first.src or None, name=name or topo.lookup_name(first.src))
        target_ext = answered_by or (ringing[0] if len(ringing) == 1 else None)
        callee = Party(
            number=first.did or (line.number if line else None),
            name=topo.extension_name(target_ext) if target_ext else (line.label if line else None),
            extension_id=target_ext,
        )
        dialed = first.did or None
    else:
        caller_ext = _ext_of_party(first.cnum, first.channel, topo)
        caller = Party(
            number=caller_ext or first.cnum or first.src or None,
            name=topo.extension_name(caller_ext) or (first.cnam or None),
            extension_id=caller_ext,
        )
        callee_ext = first.dst if first.dst in topo.extensions else None
        group = topo.ring_groups.get(first.dst)
        callee = Party(
            number=first.dst or None,
            name=topo.lookup_name(first.dst) or (group.name if group else None) or (first.dst_cnam or None),
            extension_id=callee_ext,
        )
        dialed = first.dst or None

    return Call(
        id=first.linkedid,
        started_at=started,
        ended_at=ended,
        direction=direction,
        outcome=outcome,
        caller=caller,
        callee=callee,
        dialed_number=dialed,
        line=_line_ref(line),
        ringing_extensions=ringing,
        answered_by=answered_by,
        ring_sec=ring,
        talk_sec=talk,
    )


def _same_call(a: Call, b: Call, window: timedelta) -> bool:
    return (
        abs(b.started_at - a.started_at) <= window
        and a.direction == b.direction
        and a.caller.number == b.caller.number
        and a.dialed_number == b.dialed_number
        and (a.line.id if a.line else None) == (b.line.id if b.line else None)
    )


def _merge(a: Call, b: Call) -> Call:
    best = a if _OUTCOME_RANK[a.outcome] >= _OUTCOME_RANK[b.outcome] else b
    ringing = list(dict.fromkeys(a.ringing_extensions + b.ringing_extensions))
    return best.model_copy(
        update={
            "id": a.id,
            "started_at": min(a.started_at, b.started_at),
            "ended_at": max(a.ended_at, b.ended_at),
            "ringing_extensions": ringing,
            "talk_sec": max(a.talk_sec, b.talk_sec),
        }
    )


def _dedup(calls: list[Call], window_sec: int) -> list[Call]:
    if window_sec <= 0:
        return calls
    window = timedelta(seconds=window_sec)
    out: list[Call] = []
    for call in calls:
        merged = False
        for i in range(len(out) - 1, -1, -1):
            if call.started_at - out[i].started_at > window:
                break
            if _same_call(out[i], call, window):
                out[i] = _merge(out[i], call)
                merged = True
                break
        if not merged:
            out.append(call)
    return out
