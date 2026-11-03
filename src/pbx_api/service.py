# SPDX-License-Identifier: Apache-2.0
"""Application service: business operations on top of the store and live state."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from . import __version__
from .adapters.freepbx_db import CallStore
from .config import Config
from .domain.call_builder import build_calls
from .domain.models import (
    ActiveCall,
    Call,
    CallPage,
    CallSummary,
    Direction,
    Extension,
    ExtensionStatus,
    Health,
    Line,
    Outcome,
    RingGroup,
)
from .domain.summary import summarize
from .domain.topology import Topology

log = logging.getLogger(__name__)


def system_zone() -> tzinfo:
    """The system time zone as an IANA zone (DST aware), falling back to the current offset."""
    name = os.environ.get("TZ")
    if not name:
        tzfile = Path("/etc/timezone")
        if tzfile.exists():
            name = tzfile.read_text(encoding="utf-8").strip()
    if not name:
        localtime = Path("/etc/localtime")
        if localtime.is_symlink() and "zoneinfo/" in str(localtime.resolve()):
            name = str(localtime.resolve()).split("zoneinfo/", 1)[1]
    if name:
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001
            log.warning("unknown time zone %r, using current offset", name)
    return datetime.now().astimezone().tzinfo  # type: ignore[return-value]


class LiveState(Protocol):
    """Live view of the PBX, provided by the AMI adapter."""

    connected: bool

    def extension_status(self, ext: str) -> tuple[ExtensionStatus, bool | None]: ...

    def line_registered(self, line_id: str) -> bool | None: ...

    def active_calls(self) -> list[ActiveCall]: ...


class PbxService:
    def __init__(
        self,
        config: Config,
        store: CallStore,
        live: LiveState | None = None,
        tz: tzinfo | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.live = live
        self.tz = tz or (ZoneInfo(config.freepbx.timezone) if config.freepbx.timezone else system_zone())
        self._now = now or (lambda: datetime.now(self.tz))
        self.topology = Topology()
        self.mqtt_connected: bool | None = None

    # ---- time helpers -----------------------------------------------------------

    def now(self) -> datetime:
        return self._now()

    def _local(self, dt: datetime | None) -> datetime | None:
        """Aware datetime -> naive PBX local time (as stored in the CDR)."""
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.tz)
        return dt.astimezone(self.tz).replace(tzinfo=None)

    def today_range(self) -> tuple[datetime, datetime]:
        today = self.now().astimezone(self.tz).date()
        start = datetime.combine(today, time.min, tzinfo=self.tz)
        return start, start + timedelta(days=1)

    # ---- topology ---------------------------------------------------------------

    async def refresh_topology(self) -> Topology:
        rows = await self.store.load_topology_rows()
        self.topology = Topology.from_rows(
            **rows,
            overrides=self.config.overrides.to_domain(),
            match_digits=self.config.discovery.phonebook_match_digits,
            loaded_at=self.now(),
        )
        log.info(
            "topology loaded: %d extensions, %d lines, %d DIDs, %d ring groups, %d phone book numbers",
            len(self.topology.extensions),
            len(self.topology.lines),
            len(self.topology.dids),
            len(self.topology.ring_groups),
            len(self.topology.phonebook),
        )
        return self.topology

    # ---- calls ------------------------------------------------------------------

    def _build(self, rows) -> list[Call]:
        return build_calls(rows, self.topology, self.tz, self.config.calls.dedup_window_sec)

    async def list_calls(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        before: datetime | None = None,
        direction: Direction | None = None,
        outcome: Outcome | None = None,
        extension: str | None = None,
        line: str | None = None,
        limit: int = 50,
    ) -> CallPage:
        limit = max(1, min(limit, self.config.calls.max_page_size))
        cursor = self._local(min(x for x in (before, end) if x is not None)) if (before or end) else None
        lower = self._local(start)
        chunk = max(limit * 2, 50)
        items: list[Call] = []
        exhausted = False
        for _ in range(50):  # hard bound on round trips per request
            ids = await self.store.call_ids(lower, cursor, chunk)
            if not ids:
                exhausted = True
                break
            calls = self._build(await self.store.rows_for_ids([i for i, _ in ids]))
            items.extend(c for c in calls if _matches(c, direction, outcome, extension, line))
            cursor = min(t for _, t in ids)
            if len(ids) < chunk:
                exhausted = True
                break
            if len(items) >= limit:
                break
        items.sort(key=lambda c: (c.started_at, c.id), reverse=True)
        page = items[:limit]
        more = len(items) > limit or not exhausted
        return CallPage(items=page, next_before=page[-1].started_at if page and more else None)

    async def get_call(self, call_id: str) -> Call | None:
        rows = await self.store.rows_for_ids([call_id])
        if not rows:
            return None
        t = min(r["calldate"] for r in rows)
        w = timedelta(seconds=max(self.config.calls.dedup_window_sec, 0) + 1)
        near = await self.store.call_ids(t - w, t + w, 20)
        ids = list(dict.fromkeys([call_id, *(i for i, _ in near)]))
        calls = self._build(await self.store.rows_for_ids(ids))
        exact = next((c for c in calls if c.id == call_id), None)
        if exact:
            return exact
        # the requested leg was merged into another call (gateway duplicate)
        started = t.replace(tzinfo=self.tz)
        return min(calls, key=lambda c: abs(c.started_at - started), default=None)

    async def calls_between(self, start: datetime, end: datetime) -> list[Call]:
        return self._build(await self.store.rows_between(self._local(start), self._local(end)))

    async def summary(self, start: datetime, end: datetime, group_by: str | None = None) -> CallSummary:
        return summarize(await self.calls_between(start, end), start, end, group_by)

    async def today_summary(self) -> CallSummary:
        start, end = self.today_range()
        return await self.summary(start, end)

    # ---- extensions / lines -----------------------------------------------------

    def extensions(self) -> list[Extension]:
        active = self.live.active_calls() if self.live else []
        out = []
        for info in self.topology.extensions.values():
            status, registered = (
                self.live.extension_status(info.id) if self.live else (ExtensionStatus.unknown, None)
            )
            current = next(
                (a for a in active if info.id in a.ringing_extensions or info.id in a.connected_extensions
                 or info.id in (a.caller.extension_id, a.callee.extension_id)),
                None,
            )
            out.append(
                Extension(
                    id=info.id,
                    name=info.name,
                    technology=info.technology,
                    status=status,
                    registered=registered,
                    current_call=current,
                )
            )
        return out

    def lines(self) -> list[Line]:
        reg = {lid: self.live.line_registered(lid) for lid in self.topology.lines} if self.live else {}
        return self.topology.line_models(reg)

    def ring_groups(self) -> list[RingGroup]:
        return list(self.topology.ring_groups.values())

    def active_calls(self) -> list[ActiveCall]:
        return self.live.active_calls() if self.live else []

    async def health(self) -> Health:
        db = await self.store.ping()
        ami = self.live.connected if self.live else None
        ok = db and ami is not False and self.mqtt_connected is not False
        return Health(
            status="ok" if ok else "degraded",
            version=__version__,
            database=db,
            ami=ami,
            mqtt=self.mqtt_connected,
            topology_loaded_at=self.topology.loaded_at,
        )


def _matches(
    call: Call,
    direction: Direction | None,
    outcome: Outcome | None,
    extension: str | None,
    line: str | None,
) -> bool:
    if direction and call.direction is not direction:
        return False
    if outcome and call.outcome is not outcome:
        return False
    if line and (not call.line or call.line.id != line):
        return False
    if extension:
        involved = {call.caller.extension_id, call.callee.extension_id, call.answered_by, *call.ringing_extensions}
        if extension not in involved:
            return False
    return True
