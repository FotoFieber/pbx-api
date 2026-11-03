# SPDX-License-Identifier: Apache-2.0
"""PBX topology discovered from the FreePBX configuration at runtime.

Nothing about a concrete installation is hard-coded: extensions, lines (trunks),
DIDs, ring groups, routes and the phone book are read from the PBX and can only be
adjusted through configuration overrides.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .models import Destination, Line, RingGroup

_TECH_NAMES = {"pjsip": "PJSIP", "sip": "SIP", "iax2": "IAX2", "iax": "IAX2", "dahdi": "DAHDI"}


def parse_channel(channel: str | None) -> tuple[str, str] | None:
    """Split an Asterisk channel name into (technology, resource).

    ``PJSIP/trunk-a-0000001c`` -> ``("PJSIP", "trunk-a")``
    ``Local/123@from-pstn-00000000;2`` -> ``("Local", "123@from-pstn")``
    """
    if not channel or "/" not in channel:
        return None
    tech, rest = channel.split("/", 1)
    rest = rest.split(";", 1)[0]
    if "-" in rest:
        rest = rest.rsplit("-", 1)[0]
    return tech, rest


def normalize_number(number: str | None, match_digits: int) -> str | None:
    if not number:
        return None
    digits = re.sub(r"\D", "", number)
    if not digits:
        return None
    return digits[-match_digits:] if match_digits > 0 else digits


@dataclass(frozen=True)
class ExtensionInfo:
    id: str
    name: str
    technology: str | None


@dataclass(frozen=True)
class LineInfo:
    id: str
    label: str
    technology: str
    channel_tech: str | None
    channel_resource: str | None
    number: str | None
    loop_context: str | None
    disabled: bool


@dataclass
class Overrides:
    line_labels: dict[str, str] = field(default_factory=dict)
    extension_names: dict[str, str] = field(default_factory=dict)
    hidden_extensions: set[str] = field(default_factory=set)
    hidden_lines: set[str] = field(default_factory=set)
    default_line_label: str = "name"  # "name" or "number"


def parse_destination(raw: str | None) -> Destination | None:
    if not raw:
        return None
    parts = raw.split(",")
    context = parts[0]
    target = parts[1] if len(parts) > 1 else None
    if context in ("from-did-direct", "ext-local") and target and target.isdigit():
        return Destination(type="extension", id=target, raw=raw)
    if context == "ext-group" and target:
        return Destination(type="ring_group", id=target, raw=raw)
    return Destination(type="other", id=target, raw=raw)


@dataclass
class Topology:
    extensions: dict[str, ExtensionInfo] = field(default_factory=dict)
    lines: dict[str, LineInfo] = field(default_factory=dict)
    ring_groups: dict[str, RingGroup] = field(default_factory=dict)
    dids: dict[str, Destination] = field(default_factory=dict)
    routes: dict[str, list[str]] = field(default_factory=dict)
    phonebook: dict[str, str] = field(default_factory=dict)
    match_digits: int = 9
    loaded_at: datetime | None = None

    # ---- construction -------------------------------------------------------------

    @classmethod
    def from_rows(
        cls,
        *,
        users: Iterable[Mapping[str, Any]],
        devices: Iterable[Mapping[str, Any]],
        trunks: Iterable[Mapping[str, Any]],
        incoming: Iterable[Mapping[str, Any]],
        ring_groups: Iterable[Mapping[str, Any]],
        routes: Iterable[Mapping[str, Any]],
        phonebook: Iterable[Mapping[str, Any]],
        overrides: Overrides | None = None,
        match_digits: int = 9,
        loaded_at: datetime | None = None,
    ) -> Topology:
        ov = overrides or Overrides()
        topo = cls(match_digits=match_digits, loaded_at=loaded_at)

        tech_by_ext = {str(d["id"]): str(d.get("tech") or "") for d in devices}
        for u in users:
            ext = str(u["extension"])
            if ext in ov.hidden_extensions:
                continue
            name = ov.extension_names.get(ext) or (u.get("name") or "").strip() or ext
            tech = tech_by_ext.get(ext) or None
            topo.extensions[ext] = ExtensionInfo(id=ext, name=name, technology=_TECH_NAMES.get(tech or "", tech))

        for t in trunks:
            line_id = str(t["name"])
            if line_id in ov.hidden_lines:
                continue
            tech = str(t.get("tech") or "").lower()
            channel_id = str(t.get("channelid") or "")
            number = (t.get("outcid") or "").strip() or None
            channel_tech, resource, loop_context = _trunk_channel(tech, channel_id)
            default_label = number if ov.default_line_label == "number" and number else line_id
            topo.lines[line_id] = LineInfo(
                id=line_id,
                label=ov.line_labels.get(line_id, default_label),
                technology=tech,
                channel_tech=channel_tech,
                channel_resource=resource,
                number=number,
                loop_context=loop_context,
                disabled=str(t.get("disabled") or "off").lower() == "on",
            )

        for i in incoming:
            did = str(i.get("extension") or "").strip()
            dest = parse_destination(i.get("destination"))
            if did and dest:
                topo.dids[did] = dest

        for g in ring_groups:
            gid = str(g["grpnum"])
            members = [m for m in re.split(r"[-,\s]+", str(g.get("grplist") or "")) if m]
            topo.ring_groups[gid] = RingGroup(
                id=gid,
                name=(g.get("description") or gid),
                strategy=str(g.get("strategy") or ""),
                members=members,
            )

        for r in routes:
            topo.routes.setdefault(str(r["route_name"]), []).append(str(r["trunk_name"]))

        for p in phonebook:
            key = normalize_number(p.get("number"), match_digits)
            name = (p.get("name") or "").strip()
            if key and name:
                topo.phonebook.setdefault(key, name)
        return topo

    # ---- lookups ------------------------------------------------------------------

    def line_for_channel(self, channel: str | None) -> LineInfo | None:
        parsed = parse_channel(channel)
        if not parsed:
            return None
        tech, resource = parsed
        if tech == "Local":
            context = resource.split("@", 1)[1] if "@" in resource else None
            for line in self.lines.values():
                if line.loop_context and line.loop_context == context:
                    return line
            return None
        for line in self.lines.values():
            if line.channel_tech == tech and line.channel_resource == resource:
                return line
        return None

    def extension_for_channel(self, channel: str | None) -> str | None:
        parsed = parse_channel(channel)
        if not parsed:
            return None
        tech, resource = parsed
        if tech != "Local" and resource in self.extensions:
            return resource
        return None

    def extension_name(self, ext: str | None) -> str | None:
        info = self.extensions.get(ext or "")
        return info.name if info else None

    def lookup_name(self, number: str | None) -> str | None:
        if not number:
            return None
        if number in self.extensions:
            return self.extensions[number].name
        key = normalize_number(number, self.match_digits)
        return self.phonebook.get(key) if key else None

    def line_models(self, registered: Mapping[str, bool | None] | None = None) -> list[Line]:
        registered = registered or {}
        out: list[Line] = []
        for line in self.lines.values():
            dest = None
            if line.number:
                dest = self.dids.get(line.number)
            out.append(
                Line(
                    id=line.id,
                    label=line.label,
                    technology=line.technology,
                    number=line.number,
                    registered=registered.get(line.id),
                    inbound_destination=dest,
                    outbound_routes=sorted(n for n, ls in self.routes.items() if line.id in ls),
                    loopback=line.loop_context is not None,
                )
            )
        return out


def _trunk_channel(tech: str, channel_id: str) -> tuple[str | None, str | None, str | None]:
    """Return (channel technology, channel resource, loop context) for a trunk."""
    if tech in _TECH_NAMES:
        return _TECH_NAMES[tech], channel_id, None
    if tech == "custom" and "/" in channel_id:
        ctech, rest = channel_id.split("/", 1)
        if ctech == "Local":
            context = rest.split("@", 1)[1] if "@" in rest else None
            return "Local", None, context
        return ctech, rest.split("/", 1)[0], None
    return None, None, None
