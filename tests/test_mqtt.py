# SPDX-License-Identifier: Apache-2.0
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from pbx_api.adapters.mqtt import MqttProjection
from pbx_api.config import Config, MqttConfig
from pbx_api.domain.live import Change
from pbx_api.service import PbxService

from .memory_store import MemoryStore

TZ = ZoneInfo("UTC")


@pytest.fixture
async def projection():
    svc = PbxService(Config(), MemoryStore(), tz=TZ, now=lambda: datetime(2030, 1, 10, 18, 0, tzinfo=TZ))
    await svc.refresh_topology()
    proj = MqttProjection(MqttConfig(enabled=True, node_id="test", base_topic="pbx"), svc)
    return proj


def drain(proj):
    out = {}
    while not proj._queue.empty():
        topic, payload, retain = proj._queue.get_nowait()
        out[topic] = (json.loads(payload) if payload else None, retain)
    return out


async def test_full_publish(projection):
    await projection.publish_all()
    msgs = drain(projection)
    cfg, retain = msgs["homeassistant/sensor/test/ext_201_status/config"]
    assert retain and cfg["unique_id"] == "test_ext_201_status" and cfg["name"] == "Kitchen"
    assert cfg["availability_topic"] == "pbx/status"
    assert "homeassistant/binary_sensor/test/line_gw_a_registered/config" in msgs
    assert not any("line_loop" in t for t in msgs)  # loopback lines are not real lines
    assert msgs["homeassistant/event/test/call/config"][0]["event_types"] == ["ringing", "answered", "ended", "missed"]
    recent, _ = msgs["pbx/calls/recent"]
    assert recent["count"] == 8 and "startedAt" in recent["calls"][0]
    assert all(None not in c.values() for c in recent["calls"])  # exclude_none keeps payload small
    assert msgs["pbx/calls/summary/today"][0]["totals"]["total"] == 8
    assert msgs["pbx/extension/202/state"][0]["name"] == "Office"


async def test_stale_discovery_is_removed(projection):
    projection.publish_discovery()
    drain(projection)
    del projection.svc.topology.extensions["203"]
    projection.publish_discovery()
    msgs = drain(projection)
    assert msgs["homeassistant/sensor/test/ext_203_status/config"] == (None, True)


async def test_changes_ignored_while_disconnected(projection):
    await projection.on_changes([Change("extension", "201")])
    assert projection._queue.empty()
    projection._connected.set()
    await projection.on_changes([Change("extension", "201")])
    assert "pbx/extension/201/state" in drain(projection)
