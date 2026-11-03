# SPDX-License-Identifier: Apache-2.0
"""In-memory CallStore used by the tests."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from . import fixtures as fx


class MemoryStore:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = list(rows if rows is not None else fx.ALL_ROWS)

    async def ping(self) -> bool:
        return True

    async def load_topology_rows(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "users": fx.USERS,
            "devices": fx.DEVICES,
            "trunks": fx.TRUNKS,
            "incoming": fx.INCOMING,
            "ring_groups": fx.RING_GROUPS,
            "routes": fx.ROUTES,
            "phonebook": fx.PHONEBOOK,
        }

    async def call_ids(self, start: datetime | None, before: datetime | None, limit: int) -> list[tuple[str, datetime]]:
        first: dict[str, datetime] = {}
        for r in self.rows:
            t = r["calldate"]
            if start is not None and t < start:
                continue
            if before is not None and t >= before:
                continue
            first[r["linkedid"]] = min(first.get(r["linkedid"], t), t)
        return sorted(first.items(), key=lambda kv: kv[1], reverse=True)[:limit]

    async def rows_for_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        wanted = set(ids)
        return [r for r in self.rows if r["linkedid"] in wanted]

    async def rows_between(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        return [r for r in self.rows if start <= r["calldate"] < end]
