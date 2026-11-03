# SPDX-License-Identifier: Apache-2.0
"""Client IP allow list for the HTTP API.

Entries are single IP addresses or subnets in CIDR notation (IPv4 and IPv6, freely
mixed). Loopback is always allowed so tools on the host itself keep working.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import time
from collections.abc import Iterable
from typing import Any

log = logging.getLogger(__name__)

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

LOOPBACK: tuple[IPNetwork, ...] = (ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128"))


def parse_entry(entry: str) -> IPNetwork:
    """Parse one allow-list entry: an address (``192.0.2.10``) or a subnet (``198.51.100.0/24``)."""
    text = str(entry).strip()
    if "/" not in text:
        return ipaddress.ip_network(ipaddress.ip_address(text))
    try:
        return ipaddress.ip_network(text, strict=True)
    except ValueError as exc:
        if "host bits set" in str(exc):
            net = ipaddress.ip_network(text, strict=False)
            raise ValueError(f"{text!r} has host bits set; did you mean {net} (subnet) or {text.split('/')[0]} (host)?") from None
        raise


def parse_client(host: str | None) -> IPAddress | None:
    if not host:
        return None
    try:
        addr = ipaddress.ip_address(host.split("%", 1)[0])  # drop IPv6 zone id
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        return addr.ipv4_mapped
    return addr


def is_allowed(host: str | None, networks: Iterable[IPNetwork]) -> bool:
    networks = list(networks)
    if not networks:
        return True  # no restriction configured
    addr = parse_client(host)
    if addr is None:
        return False
    return any(addr in net for net in (*LOOPBACK, *networks) if addr.version == net.version)


class AllowListMiddleware:
    """ASGI middleware rejecting HTTP requests from clients not on the allow list."""

    def __init__(self, app: Any, state: Any, log_interval_sec: float = 60.0) -> None:
        self.app = app
        self.state = state  # app.state: holds .allowed_clients (list of networks)
        self.log_interval = log_interval_sec
        self._logged: dict[str, float] = {}

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        host = (scope.get("client") or (None, None))[0]
        if is_allowed(host, getattr(self.state, "allowed_clients", [])):
            await self.app(scope, receive, send)
            return
        self._log(host, scope.get("path", ""))
        body = json.dumps({"type": "about:blank", "title": "Client not allowed", "status": 403}).encode()
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [(b"content-type", b"application/problem+json"), (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})

    def _log(self, host: str | None, path: str) -> None:
        now = time.monotonic()
        key = host or "?"
        if now - self._logged.get(key, -1e9) >= self.log_interval:
            self._logged[key] = now
            log.warning("rejected request from %s to %s (not in server.allowed_clients)", key, path)
