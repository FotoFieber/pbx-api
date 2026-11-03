# SPDX-License-Identifier: Apache-2.0
"""Live PBX state derived from Asterisk Manager Interface events.

Pure logic without I/O so it can be tested with recorded or synthetic events.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .models import ActiveCall, Direction, ExtensionStatus, LineRef, Party
from .topology import Topology

DEVICE_STATES = {
    "NOT_INUSE": ExtensionStatus.idle,
    "INUSE": ExtensionStatus.in_call,
    "BUSY": ExtensionStatus.busy,
    "RINGING": ExtensionStatus.ringing,
    "RINGINUSE": ExtensionStatus.ringing,
    "ONHOLD": ExtensionStatus.on_hold,
    "UNAVAILABLE": ExtensionStatus.unavailable,
    "INVALID": ExtensionStatus.unavailable,
    "UNKNOWN": ExtensionStatus.unknown,
}
_UNREGISTERED = {"UNAVAILABLE", "INVALID"}


@dataclass
class Channel:
    name: str
    uniqueid: str
    linkedid: str
    state: str
    caller_num: str
    caller_name: str
    connected_num: str
    connected_name: str
    exten: str
    created: datetime


@dataclass(frozen=True)
class Change:
    """Something observers may want to react to."""

    kind: str  # "extension", "line", "call", "call_ended", "resync"
    id: str
    detail: str | None = None


def _clean(value: Any) -> str:
    s = "" if value is None else str(value)
    return "" if s in ("<unknown>", "<unknown>".upper()) else s


@dataclass
class LiveModel:
    topology: Callable[[], Topology]
    clock: Callable[[], datetime]
    device_states: dict[str, str] = field(default_factory=dict)
    channels: dict[str, Channel] = field(default_factory=dict)
    connected: bool = False
    _call_states: dict[str, str] = field(default_factory=dict)

    # ---- event handling ---------------------------------------------------------

    def reset(self) -> None:
        self.device_states.clear()
        self.channels.clear()
        self._call_states.clear()

    def handle(self, event: Mapping[str, Any]) -> list[Change]:
        name = event.get("Event")
        if name == "DeviceStateChange":
            return self._device(event)
        if name in ("Newchannel", "CoreShowChannel"):
            return self._new_channel(event)
        if name in ("Newstate", "NewConnectedLine"):
            return self._update_channel(event)
        if name == "Hangup":
            return self._hangup(event)
        return []

    def _device(self, ev: Mapping[str, Any]) -> list[Change]:
        device, state = str(ev.get("Device") or ""), str(ev.get("State") or "UNKNOWN").upper()
        if "/" not in device:
            return []
        self.device_states[device] = state
        tech, resource = device.split("/", 1)
        topo = self.topology()
        if resource in topo.extensions:
            return [Change("extension", resource)]
        for line in topo.lines.values():
            if line.channel_tech == tech and line.channel_resource == resource:
                return [Change("line", line.id)]
        return []

    def _new_channel(self, ev: Mapping[str, Any]) -> list[Change]:
        uid = str(ev.get("Uniqueid") or ev.get("UniqueID") or "")
        if not uid:
            return []
        self.channels[uid] = Channel(
            name=str(ev.get("Channel") or ""),
            uniqueid=uid,
            linkedid=str(ev.get("Linkedid") or uid),
            state=str(ev.get("ChannelStateDesc") or ""),
            caller_num=_clean(ev.get("CallerIDNum")),
            caller_name=_clean(ev.get("CallerIDName")),
            connected_num=_clean(ev.get("ConnectedLineNum")),
            connected_name=_clean(ev.get("ConnectedLineName")),
            exten=_clean(ev.get("Exten")),
            created=self.clock(),
        )
        return self._call_changed(self.channels[uid].linkedid)

    def _update_channel(self, ev: Mapping[str, Any]) -> list[Change]:
        uid = str(ev.get("Uniqueid") or "")
        ch = self.channels.get(uid)
        if ch is None:
            return self._new_channel(ev)
        ch.state = str(ev.get("ChannelStateDesc") or ch.state)
        ch.caller_num = _clean(ev.get("CallerIDNum")) or ch.caller_num
        ch.caller_name = _clean(ev.get("CallerIDName")) or ch.caller_name
        ch.connected_num = _clean(ev.get("ConnectedLineNum")) or ch.connected_num
        ch.connected_name = _clean(ev.get("ConnectedLineName")) or ch.connected_name
        ch.exten = _clean(ev.get("Exten")) or ch.exten
        return self._call_changed(ch.linkedid)

    def _hangup(self, ev: Mapping[str, Any]) -> list[Change]:
        ch = self.channels.pop(str(ev.get("Uniqueid") or ""), None)
        if ch is None:
            return []
        if any(c.linkedid == ch.linkedid for c in self.channels.values()):
            return self._call_changed(ch.linkedid)
        self._call_states.pop(ch.linkedid, None)
        return [Change("call_ended", ch.linkedid)]

    def _call_changed(self, linkedid: str) -> list[Change]:
        call = self.active_call(linkedid)
        if call is None:
            return []
        previous = self._call_states.get(linkedid)
        self._call_states[linkedid] = call.state
        if previous == call.state:
            return [Change("call", linkedid)]
        return [Change("call", linkedid, detail=call.state)]

    # ---- queries ----------------------------------------------------------------

    def extension_status(self, ext: str) -> tuple[ExtensionStatus, bool | None]:
        info = self.topology().extensions.get(ext)
        tech = info.technology if info and info.technology else "PJSIP"
        raw = self.device_states.get(f"{tech}/{ext}")
        if raw is None:
            return ExtensionStatus.unknown, None
        return DEVICE_STATES.get(raw, ExtensionStatus.unknown), raw not in _UNREGISTERED

    def line_registered(self, line_id: str) -> bool | None:
        line = self.topology().lines.get(line_id)
        if not line or not line.channel_tech or not line.channel_resource:
            return None
        raw = self.device_states.get(f"{line.channel_tech}/{line.channel_resource}")
        return None if raw is None else raw not in _UNREGISTERED

    def active_calls(self) -> list[ActiveCall]:
        ids = dict.fromkeys(c.linkedid for c in sorted(self.channels.values(), key=lambda c: c.created))
        return [call for call in (self.active_call(i) for i in ids) if call]

    def active_call(self, linkedid: str) -> ActiveCall | None:
        chans = sorted((c for c in self.channels.values() if c.linkedid == linkedid), key=lambda c: c.created)
        if not chans:
            return None
        topo = self.topology()
        first = chans[0]
        first_line = topo.line_for_channel(first.name)
        other_lines = [l for l in (topo.line_for_channel(c.name) for c in chans[1:]) if l]

        ringing, connected = [], []
        for c in chans[1:]:
            ext = topo.extension_for_channel(c.name)
            if not ext:
                continue
            if c.state in ("Ringing", "Ring"):
                ringing.append(ext)
            elif c.state == "Up":
                connected.append(ext)
        trunk_up = any(c.state == "Up" and topo.line_for_channel(c.name) for c in chans[1:])

        if first_line and not first_line.loop_context:
            direction, line = Direction.inbound, first_line
            caller = Party(number=first.caller_num or None,
                           name=first.caller_name or topo.lookup_name(first.caller_num))
            target = connected[0] if connected else (ringing[0] if len(ringing) == 1 else None)
            callee = Party(number=first.exten or None, name=topo.extension_name(target), extension_id=target)
        else:
            direction = Direction.outbound if other_lines else Direction.internal
            line = other_lines[0] if other_lines else None
            caller_ext = topo.extension_for_channel(first.name)
            caller = Party(number=caller_ext or first.caller_num or None,
                           name=topo.extension_name(caller_ext) or first.caller_name or None,
                           extension_id=caller_ext)
            callee_num = first.exten or first.connected_num or None
            callee = Party(
                number=callee_num,
                name=topo.lookup_name(callee_num) or first.connected_name or None,
                extension_id=callee_num if callee_num in topo.extensions else None,
            )
        # once connected, a call stays connected while its legs hang up one by one
        was_connected = self._call_states.get(linkedid) == "connected"
        state = "connected" if connected or trunk_up or was_connected else "ringing"
        return ActiveCall(
            id=linkedid,
            started_at=first.created,
            direction=direction,
            state=state,
            caller=caller,
            callee=callee,
            line=LineRef(id=line.id, label=line.label, number=line.number) if line else None,
            ringing_extensions=ringing,
            connected_extensions=connected,
        )
