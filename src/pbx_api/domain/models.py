# SPDX-License-Identifier: Apache-2.0
"""Domain model shared by the REST API and the MQTT projection."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class ApiModel(BaseModel):
    """Base model: camelCase on the wire, snake_case in Python."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=False)


class Direction(str, Enum):
    inbound = "inbound"
    outbound = "outbound"
    internal = "internal"


class Outcome(str, Enum):
    answered = "answered"
    missed = "missed"  # inbound call nobody answered
    no_answer = "no_answer"  # outbound/internal call the other side did not answer
    busy = "busy"
    failed = "failed"


class ExtensionStatus(str, Enum):
    idle = "idle"
    ringing = "ringing"
    in_call = "in_call"
    on_hold = "on_hold"
    busy = "busy"
    unavailable = "unavailable"
    unknown = "unknown"


class Party(ApiModel):
    """One side of a call."""

    number: str | None = Field(default=None, description="Phone number or extension number")
    name: str | None = Field(default=None, description="Display name, if known")
    extension_id: str | None = Field(default=None, description="Set when this party is a local extension")


class LineRef(ApiModel):
    id: str = Field(description="Line (trunk) identifier")
    label: str
    number: str | None = Field(default=None, description="Own number of the line, if known")


class Call(ApiModel):
    """A complete call as experienced by the user, merged from all its legs."""

    id: str = Field(description="Stable call identifier")
    started_at: datetime
    ended_at: datetime
    direction: Direction
    outcome: Outcome
    caller: Party
    callee: Party
    dialed_number: str | None = Field(default=None, description="Number dialed by the caller")
    line: LineRef | None = Field(default=None, description="External line used, if any")
    ringing_extensions: list[str] = Field(default_factory=list, description="Extensions that rang")
    answered_by: str | None = Field(default=None, description="Extension that answered (inbound/internal)")
    ring_sec: int = Field(ge=0, description="Seconds until answered or given up")
    talk_sec: int = Field(ge=0, description="Seconds of conversation")


class CallPage(ApiModel):
    items: list[Call]
    next_before: datetime | None = Field(
        default=None, description="Pass as 'before' to fetch the next (older) page; null when exhausted"
    )


class Counts(ApiModel):
    total: int = 0
    inbound: int = 0
    outbound: int = 0
    internal: int = 0
    answered: int = 0
    missed_inbound: int = 0
    talk_sec: int = 0


class SummaryGroup(ApiModel):
    key: str = Field(description="Group key: ISO date, line id or extension id")
    counts: Counts


class CallSummary(ApiModel):
    from_: datetime = Field(alias="from")
    to: datetime
    totals: Counts
    groups: list[SummaryGroup] = Field(default_factory=list)


class ActiveCall(ApiModel):
    id: str
    started_at: datetime
    direction: Direction
    state: str = Field(description="ringing or connected")
    caller: Party
    callee: Party
    line: LineRef | None = None
    ringing_extensions: list[str] = Field(default_factory=list)
    connected_extensions: list[str] = Field(default_factory=list)


class Extension(ApiModel):
    id: str
    name: str
    technology: str | None = None
    status: ExtensionStatus = ExtensionStatus.unknown
    registered: bool | None = Field(default=None, description="null while live state is not known")
    current_call: ActiveCall | None = None


class Destination(ApiModel):
    type: str = Field(description="extension, ring_group or other")
    id: str | None = None
    raw: str


class Line(ApiModel):
    id: str
    label: str
    technology: str
    number: str | None = None
    registered: bool | None = None
    inbound_destination: Destination | None = None
    outbound_routes: list[str] = Field(default_factory=list)
    loopback: bool = Field(default=False, description="Routes calls back into the PBX instead of out")


class RingGroup(ApiModel):
    id: str
    name: str
    strategy: str
    members: list[str]


class Health(ApiModel):
    status: str
    version: str
    database: bool
    ami: bool | None = None
    mqtt: bool | None = None
    topology_loaded_at: datetime | None = None


class Problem(ApiModel):
    """RFC 9457 problem details."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None


def call_day(call: Call) -> date:
    return call.started_at.date()
