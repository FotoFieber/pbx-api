# SPDX-License-Identifier: Apache-2.0
"""Read-only access to the FreePBX configuration and CDR databases.

This is the only module that knows table and column names.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Protocol

import aiomysql
import pymysql

from ..config import DbConfig, read_secret

log = logging.getLogger(__name__)

CDR_COLUMNS = (
    "calldate, src, dst, dcontext, channel, dstchannel, disposition, duration, billsec, "
    "linkedid, uniqueid, sequence, did, cnum, cnam, dst_cnam"
)

TOPOLOGY_QUERIES: dict[str, str] = {
    "users": "SELECT extension, name FROM users",
    "devices": "SELECT id, tech FROM devices",
    "trunks": "SELECT trunkid, name, tech, channelid, outcid, disabled FROM trunks",
    "incoming": "SELECT extension, cidnum, destination FROM incoming",
    "ring_groups": "SELECT grpnum, strategy, grplist, description FROM ringgroups",
    "routes": (
        "SELECT r.name AS route_name, t.name AS trunk_name FROM outbound_routes r "
        "JOIN outbound_route_trunks s ON s.route_id = r.route_id "
        "JOIN trunks t ON t.trunkid = s.trunk_id ORDER BY r.route_id, s.seq"
    ),
    "phonebook": (
        "SELECT COALESCE(NULLIF(e.displayname, ''), TRIM(CONCAT_WS(' ', e.fname, e.lname))) AS name, "
        "n.number AS number FROM contactmanager_entry_numbers n "
        "JOIN contactmanager_group_entries e ON e.id = n.entryid"
    ),
}

# Tables of optional FreePBX modules; missing tables are treated as empty.
OPTIONAL = {"ring_groups", "routes", "phonebook", "incoming"}


class CallStore(Protocol):
    async def load_topology_rows(self) -> dict[str, list[dict[str, Any]]]: ...

    async def call_ids(self, start: datetime | None, before: datetime | None, limit: int) -> list[tuple[str, datetime]]: ...

    async def rows_for_ids(self, ids: list[str]) -> list[dict[str, Any]]: ...

    async def rows_between(self, start: datetime, end: datetime) -> list[dict[str, Any]]: ...

    async def ping(self) -> bool: ...


class FreePbxDatabase:
    """aiomysql-backed implementation of :class:`CallStore`. Datetimes are naive PBX local time."""

    def __init__(self, cfg: DbConfig) -> None:
        self._cfg = cfg
        self._pool: aiomysql.Pool | None = None

    async def start(self) -> None:
        kwargs: dict[str, Any] = {
            "user": self._cfg.user,
            "password": read_secret(self._cfg.password_file) or "",
            "autocommit": True,
            "minsize": 1,
            "maxsize": 4,
            "charset": "utf8mb4",
            "cursorclass": aiomysql.DictCursor,
        }
        if self._cfg.unix_socket:
            kwargs["unix_socket"] = self._cfg.unix_socket
        else:
            kwargs.update(host=self._cfg.host, port=self._cfg.port)
        self._pool = await aiomysql.create_pool(**kwargs)

    async def stop(self) -> None:
        if self._pool:
            self._pool.close()
            await self._pool.wait_closed()

    async def _fetch(self, database: str, sql: str, args: Any = None) -> list[dict[str, Any]]:
        assert self._pool, "database not started"
        async with self._pool.acquire() as conn:
            await conn.select_db(database)
            async with conn.cursor() as cur:
                await cur.execute(sql, args)
                return list(await cur.fetchall())

    async def ping(self) -> bool:
        try:
            await self._fetch(self._cfg.cdr_database, "SELECT 1 AS ok")
            return True
        except Exception:  # noqa: BLE001 - health check
            log.exception("database ping failed")
            return False

    async def load_topology_rows(self) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for key, sql in TOPOLOGY_QUERIES.items():
            try:
                out[key] = await self._fetch(self._cfg.config_database, sql)
            except pymysql.err.ProgrammingError as exc:
                if key in OPTIONAL and exc.args and exc.args[0] in (1146, 1054):
                    log.warning("topology source %s unavailable (%s); treating as empty", key, exc.args[1])
                    out[key] = []
                else:
                    raise
        return out

    async def call_ids(self, start: datetime | None, before: datetime | None, limit: int) -> list[tuple[str, datetime]]:
        where, args = [], []
        if start is not None:
            where.append("calldate >= %s")
            args.append(start)
        if before is not None:
            where.append("calldate < %s")
            args.append(before)
        sql = "SELECT linkedid, MIN(calldate) AS t FROM cdr"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " GROUP BY linkedid ORDER BY t DESC LIMIT %s"
        args.append(limit)
        rows = await self._fetch(self._cfg.cdr_database, sql, args)
        return [(r["linkedid"], r["t"]) for r in rows]

    async def rows_for_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        if not ids:
            return []
        marks = ", ".join(["%s"] * len(ids))
        sql = f"SELECT {CDR_COLUMNS} FROM cdr WHERE linkedid IN ({marks})"  # noqa: S608 - placeholders only
        return await self._fetch(self._cfg.cdr_database, sql, ids)

    async def rows_between(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        sql = f"SELECT {CDR_COLUMNS} FROM cdr WHERE calldate >= %s AND calldate < %s"  # noqa: S608
        return await self._fetch(self._cfg.cdr_database, sql, (start, end))
