# SPDX-License-Identifier: Apache-2.0
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from pbx_api.app import create_app
from pbx_api.config import Config
from pbx_api.service import PbxService

from .memory_store import MemoryStore

TZ = ZoneInfo("Europe/Berlin")
TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def client():
    svc = PbxService(Config(), MemoryStore(), tz=TZ, now=lambda: datetime(2030, 1, 10, 18, 0, tzinfo=TZ))
    await svc.refresh_topology()
    with TestClient(create_app(svc, tokens=[TOKEN])) as c:
        yield c


def test_auth_required(client):
    r = client.get("/v1/calls")
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/problem+json")
    assert client.get("/v1/calls", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_health_is_public(client):
    r = client.get("/v1/health")
    assert r.status_code == 200
    assert r.json()["database"] is True


def test_list_calls_camel_case_and_offsets(client):
    r = client.get("/v1/calls", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 8 and body["nextBefore"] is None
    first = body["items"][0]
    assert first["startedAt"].endswith("+01:00")
    assert {"ringingExtensions", "answeredBy", "talkSec", "dialedNumber"} <= first.keys()


def test_paging(client):
    seen, before = [], None
    for _ in range(10):
        params = {"limit": 3, **({"before": before} if before else {})}
        body = client.get("/v1/calls", params=params, headers=AUTH).json()
        seen += [c["id"] for c in body["items"]]
        before = body["nextBefore"]
        if not before:
            break
    assert len(seen) == 8 and len(set(seen)) == 8


def test_filters(client):
    q = lambda **p: client.get("/v1/calls", params=p, headers=AUTH).json()["items"]  # noqa: E731
    assert len(q(direction="inbound")) == 3
    assert [c["id"] for c in q(direction="inbound", outcome="missed")] == ["1003.1"]
    assert {c["id"] for c in q(line="gw-a")} == {"1002.1", "1003.1", "1007.1"}
    assert {c["id"] for c in q(extension="202")} == {"1000.1", "1001.1", "1004.1", "1005.1", "1006.1"}
    assert client.get("/v1/calls", params={"direction": "sideways"}, headers=AUTH).status_code == 422


def test_get_call_and_merged_leg(client):
    assert client.get("/v1/calls/1004.1", headers=AUTH).json()["answeredBy"] == "202"
    # the second gateway leg resolves to the merged call
    assert client.get("/v1/calls/1003.2", headers=AUTH).json()["id"] == "1003.1"
    assert client.get("/v1/calls/nope", headers=AUTH).status_code == 404


def test_summary_defaults_to_today(client):
    body = client.get("/v1/calls/summary", params={"group_by": "day"}, headers=AUTH).json()
    assert body["totals"]["total"] == 8
    assert body["totals"]["missedInbound"] == 1
    assert body["groups"] == [{"key": "2030-01-10", "counts": body["totals"]}]
    assert body["from"].startswith("2030-01-10T00:00:00")


def test_lines_extensions_ring_groups(client):
    lines = {l["id"]: l for l in client.get("/v1/lines", headers=AUTH).json()}
    assert lines["gw-b"]["number"] == "15550101"
    assert lines["gw-b"]["inboundDestination"] == {"type": "ring_group", "id": "300", "raw": "ext-group,300,1"}
    assert lines["loop"]["loopback"] is True
    exts = client.get("/v1/extensions", headers=AUTH).json()
    assert [e["name"] for e in exts] == ["Kitchen", "Office", "Softphone"]
    assert exts[0]["status"] == "unknown" and exts[0]["registered"] is None
    assert client.get("/v1/extensions/999", headers=AUTH).status_code == 404
    assert client.get("/v1/ring-groups", headers=AUTH).json()[0]["members"] == ["201", "202"]
