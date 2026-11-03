# SPDX-License-Identifier: Apache-2.0
"""Client IP allow list. Addresses are from the documentation ranges (RFC 5737/3849)."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from pbx_api.access import is_allowed, parse_entry
from pbx_api.app import create_app
from pbx_api.config import Config, ServerConfig
from pbx_api.service import PbxService

from .memory_store import MemoryStore

TZ = ZoneInfo("UTC")
TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
MIXED = ["192.0.2.10", "198.51.100.0/24", "2001:db8::/32"]


@pytest.fixture
async def service():
    svc = PbxService(Config(), MemoryStore(), tz=TZ, now=lambda: datetime(2030, 1, 10, 18, 0, tzinfo=TZ))
    await svc.refresh_topology()
    return svc


def client(svc, host, allowed=MIXED):
    nets = ServerConfig(allowed_clients=allowed).client_networks()
    return TestClient(create_app(svc, tokens=[TOKEN], allowed_clients=nets), client=(host, 40000))


@pytest.mark.parametrize("host,ok", [
    ("192.0.2.10", True),          # single address, exact match
    ("192.0.2.11", False),         # neighbour of a single address
    ("198.51.100.1", True),        # inside subnet
    ("198.51.100.254", True),
    ("198.51.101.1", False),       # outside subnet
    ("2001:db8::42", True),        # IPv6 subnet
    ("2001:db9::1", False),
    ("::ffff:192.0.2.10", True),   # IPv4-mapped IPv6
    ("::ffff:203.0.113.9", False),
    ("127.0.0.1", True),           # loopback always allowed
    ("::1", True),
    ("203.0.113.9", False),
    ("testclient", False),         # not an IP address
    ("", False),
])
def test_mixed_list(host, ok):
    nets = ServerConfig(allowed_clients=MIXED).client_networks()
    assert is_allowed(host, nets) is ok


def test_empty_list_allows_everyone():
    assert is_allowed("203.0.113.9", [])
    assert is_allowed("testclient", [])


def test_config_parsing():
    cfg = ServerConfig(allowed_clients=["192.0.2.10", "198.51.100.0/24", "2001:db8::/32"])
    assert cfg.allowed_clients == ["192.0.2.10/32", "198.51.100.0/24", "2001:db8::/32"]
    assert ServerConfig(allowed_clients="192.0.2.10").allowed_clients == ["192.0.2.10/32"]
    assert ServerConfig(allowed_clients=None).allowed_clients == []


@pytest.mark.parametrize("bad", ["foo", "192.0.2.300", "198.51.100.0/33"])
def test_invalid_entries_rejected(bad):
    with pytest.raises(ValidationError, match="allowed_clients"):
        ServerConfig(allowed_clients=["192.0.2.10", bad])


def test_host_bits_give_a_helpful_error():
    with pytest.raises(ValueError, match=r"did you mean 192\.0\.2\.0/24 \(subnet\) or 192\.0\.2\.10 \(host\)"):
        parse_entry("192.0.2.10/24")


async def test_http_allowed_and_rejected(service):
    with client(service, "198.51.100.77") as c:
        assert c.get("/v1/health").status_code == 200
        assert c.get("/v1/calls", headers=AUTH).status_code == 200
        assert c.get("/v1/calls").status_code == 401  # allowed IP, still needs a token
    with client(service, "203.0.113.9") as c:
        for path in ("/v1/health", "/v1/calls", "/docs", "/openapi.json"):
            r = c.get(path, headers=AUTH)  # a valid token does not help
            assert r.status_code == 403, path
            assert r.headers["content-type"] == "application/problem+json"
            assert r.json() == {"type": "about:blank", "title": "Client not allowed", "status": 403}


async def test_no_list_keeps_previous_behaviour(service):
    with client(service, "203.0.113.9", allowed=[]) as c:
        assert c.get("/v1/calls", headers=AUTH).status_code == 200


async def test_list_can_be_replaced_at_runtime(service):
    with client(service, "203.0.113.9") as c:
        assert c.get("/v1/health").status_code == 403
        c.app.state.allowed_clients = ServerConfig(allowed_clients=["203.0.113.0/24"]).client_networks()
        assert c.get("/v1/health").status_code == 200
