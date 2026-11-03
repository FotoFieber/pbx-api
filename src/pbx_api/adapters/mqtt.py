# SPDX-License-Identifier: Apache-2.0
"""MQTT projection of the domain model, with Home Assistant MQTT discovery.

Topics (``<base>`` = configured base topic):

* ``<base>/status``                    online/offline (retained, last will)
* ``<base>/extension/<id>/state``      Extension (retained)
* ``<base>/line/<id>/state``           Line (retained)
* ``<base>/calls/recent``              {"count", "calls": [Call]} (retained)
* ``<base>/calls/active``              {"count", "calls": [ActiveCall]} (retained)
* ``<base>/calls/summary/today``       CallSummary (retained)
* ``<base>/events/call``               {"event_type", "call"} (not retained)
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from collections.abc import Iterable
from datetime import date
from typing import Any

import aiomqtt
from pydantic import BaseModel

from .. import __version__
from ..config import MqttConfig, read_secret
from ..domain.live import Change
from ..domain.models import ExtensionStatus, Outcome
from ..service import PbxService

log = logging.getLogger(__name__)

EVENT_TYPES = ["ringing", "answered", "ended", "missed"]


def _dump(model: BaseModel | Iterable[BaseModel] | Any) -> Any:
    if isinstance(model, BaseModel):
        return model.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(model, list):
        return [_dump(m) for m in model]
    return model


class MqttProjection:
    def __init__(self, cfg: MqttConfig, service: PbxService) -> None:
        self.cfg = cfg
        self.svc = service
        self.base = cfg.base_topic.rstrip("/")
        self._queue: asyncio.Queue[tuple[str, str, bool]] = asyncio.Queue()
        self._connected = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._summary_day: date | None = None
        self._discovery_topics: set[str] = set()

    # ---- lifecycle --------------------------------------------------------------

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._run(), name="mqtt"),
            asyncio.create_task(self._ticker(), name="mqtt-ticker"),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()

    def _client(self) -> aiomqtt.Client:
        tls = ssl.create_default_context() if self.cfg.tls else None
        return aiomqtt.Client(
            hostname=self.cfg.host,
            port=self.cfg.port,
            username=self.cfg.user,
            password=read_secret(self.cfg.password_file),
            identifier=f"pbx-api-{self.cfg.node_id}",
            will=aiomqtt.Will(f"{self.base}/status", "offline", qos=1, retain=True),
            tls_context=tls,
            keepalive=30,
        )

    async def _run(self) -> None:
        delay = 2
        while True:
            try:
                async with self._client() as client:
                    log.info("MQTT connected to %s:%s", self.cfg.host, self.cfg.port)
                    self.svc.mqtt_connected = True
                    delay = 2
                    self._drain()
                    await client.publish(f"{self.base}/status", "online", qos=1, retain=True)
                    await self.publish_all()
                    self._connected.set()
                    while True:
                        topic, payload, retain = await self._queue.get()
                        await client.publish(topic, payload, qos=1, retain=retain)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on any broker problem
                log.warning("MQTT connection problem: %s", exc)
            self.svc.mqtt_connected = False
            self._connected.clear()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    def _drain(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()

    async def _ticker(self) -> None:
        """Republish today's summary when the day changes, and the call list periodically."""
        tick = 0
        while True:
            await asyncio.sleep(60)
            tick += 1
            if not self._connected.is_set():
                continue
            try:
                today = self.svc.today_range()[0].date()
                if today != self._summary_day:
                    await self.publish_summary()
                if tick % 5 == 0:
                    await self.publish_recent()
                    await self.publish_summary()
            except Exception:  # noqa: BLE001
                log.exception("periodic MQTT publish failed")

    # ---- publishing -------------------------------------------------------------

    def _put(self, topic: str, payload: Any, retain: bool = True) -> None:
        text = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"))
        self._queue.put_nowait((topic, text, retain))

    async def publish_all(self) -> None:
        self.publish_discovery()
        for ext in self.svc.extensions():
            self._put(f"{self.base}/extension/{ext.id}/state", _dump(ext))
        for line in self.svc.lines():
            self._put(f"{self.base}/line/{line.id}/state", _dump(line))
        self.publish_active()
        await self.publish_recent()
        await self.publish_summary()

    async def publish_recent(self) -> None:
        page = await self.svc.list_calls(limit=self.cfg.recent_limit)
        self._put(f"{self.base}/calls/recent", {"count": len(page.items), "calls": _dump(page.items)})

    async def publish_summary(self) -> None:
        summary = await self.svc.today_summary()
        self._summary_day = summary.from_.date()
        self._put(f"{self.base}/calls/summary/today", _dump(summary))

    def publish_active(self) -> None:
        calls = self.svc.active_calls()
        self._put(f"{self.base}/calls/active", {"count": len(calls), "calls": _dump(calls)})

    def publish_extension(self, ext_id: str) -> None:
        for ext in self.svc.extensions():
            if ext.id == ext_id:
                self._put(f"{self.base}/extension/{ext.id}/state", _dump(ext))

    def publish_line(self, line_id: str) -> None:
        for line in self.svc.lines():
            if line.id == line_id:
                self._put(f"{self.base}/line/{line.id}/state", _dump(line))

    def publish_event(self, event_type: str, call: BaseModel) -> None:
        self._put(f"{self.base}/events/call", {"event_type": event_type, "call": _dump(call)}, retain=False)

    # ---- reacting to live changes ---------------------------------------------------

    async def on_changes(self, changes: list[Change]) -> None:
        if not self._connected.is_set():
            return
        touched_calls = False
        for ch in changes:
            if ch.kind == "resync":
                await self.publish_all()
                return
            if ch.kind == "extension":
                self.publish_extension(ch.id)
            elif ch.kind == "line":
                self.publish_line(ch.id)
            elif ch.kind == "call":
                touched_calls = True
                if ch.detail in ("ringing", "connected"):
                    active = next((a for a in self.svc.active_calls() if a.id == ch.id), None)
                    if active:
                        self.publish_event("ringing" if ch.detail == "ringing" else "answered", active)
            elif ch.kind == "call_ended":
                touched_calls = True
                asyncio.create_task(self._finish_call(ch.id))
        if touched_calls:
            self.publish_active()
            # the current call is part of the extension payload
            for ext in self.svc.extensions():
                self._put(f"{self.base}/extension/{ext.id}/state", _dump(ext))

    async def _finish_call(self, call_id: str) -> None:
        """After hangup the CDR is written; publish the finished call once it is readable."""
        for attempt in range(5):
            await asyncio.sleep(1.5 * (attempt + 1))
            call = await self.svc.get_call(call_id)
            if call:
                self.publish_event("missed" if call.outcome is Outcome.missed else "ended", call)
                await self.publish_recent()
                await self.publish_summary()
                return
        log.info("no CDR found for ended call %s", call_id)
        await self.publish_recent()

    # ---- Home Assistant discovery -------------------------------------------------

    def _device(self) -> dict[str, Any]:
        return {
            "identifiers": [f"pbx_api_{self.cfg.node_id}"],
            "name": self.cfg.device_name,
            "manufacturer": "pbx-api",
            "model": "FreePBX/Asterisk bridge",
            "sw_version": __version__,
        }

    def _entity(self, component: str, object_id: str, config: dict[str, Any]) -> None:
        node = self.cfg.node_id
        topic = f"{self.cfg.discovery_prefix}/{component}/{node}/{object_id}/config"
        payload = {
            "unique_id": f"{node}_{object_id}",
            "device": self._device(),
            "origin": {"name": "pbx-api", "sw_version": __version__},
            "availability_topic": f"{self.base}/status",
            "has_entity_name": True,
            **config,
        }
        self._discovery_topics.add(topic)
        self._put(topic, payload)

    def publish_discovery(self) -> None:
        previous, self._discovery_topics = self._discovery_topics, set()
        statuses = [s.value for s in ExtensionStatus]
        for ext in self.svc.topology.extensions.values():
            state = f"{self.base}/extension/{ext.id}/state"
            self._entity("sensor", f"ext_{ext.id}_status", {
                "name": ext.name,
                "icon": "mdi:phone-classic",
                "state_topic": state,
                "value_template": "{{ value_json.status }}",
                "json_attributes_topic": state,
                "device_class": "enum",
                "options": statuses,
            })
            self._entity("binary_sensor", f"ext_{ext.id}_registered", {
                "name": f"{ext.name} registered",
                "device_class": "connectivity",
                "entity_category": "diagnostic",
                "state_topic": state,
                "value_template": "{{ 'ON' if value_json.registered else 'OFF' }}",
            })
        for line in self.svc.topology.lines.values():
            if line.loop_context:
                continue
            self._entity("binary_sensor", f"line_{_slug(line.id)}_registered", {
                "name": f"Line {line.label}",
                "device_class": "connectivity",
                "entity_category": "diagnostic",
                "state_topic": f"{self.base}/line/{line.id}/state",
                "value_template": "{{ 'ON' if value_json.registered else 'OFF' }}",
                "json_attributes_topic": f"{self.base}/line/{line.id}/state",
            })
        summary = f"{self.base}/calls/summary/today"
        self._entity("sensor", "calls_today", {
            "name": "Calls today", "icon": "mdi:phone-log", "state_topic": summary,
            "value_template": "{{ value_json.totals.total }}", "unit_of_measurement": "calls",
            "state_class": "measurement", "json_attributes_topic": summary,
            "json_attributes_template": "{{ value_json.totals | tojson }}",
        })
        self._entity("sensor", "missed_today", {
            "name": "Missed calls today", "icon": "mdi:phone-missed", "state_topic": summary,
            "value_template": "{{ value_json.totals.missedInbound }}", "unit_of_measurement": "calls",
            "state_class": "measurement",
        })
        recent = f"{self.base}/calls/recent"
        self._entity("sensor", "recent_calls", {
            "name": "Recent calls", "icon": "mdi:phone-log-outline", "state_topic": recent,
            "value_template": "{{ value_json.count }}", "json_attributes_topic": recent,
            "json_attributes_template": "{{ {'calls': value_json.calls} | tojson }}",
        })
        active = f"{self.base}/calls/active"
        self._entity("sensor", "active_calls", {
            "name": "Active calls", "icon": "mdi:phone-in-talk", "state_topic": active,
            "value_template": "{{ value_json.count }}", "json_attributes_topic": active,
            "json_attributes_template": "{{ {'calls': value_json.calls} | tojson }}",
        })
        self._entity("event", "call", {
            "name": "Call", "icon": "mdi:phone-ring", "state_topic": f"{self.base}/events/call",
            "event_types": EVENT_TYPES,
        })
        for stale in previous - self._discovery_topics:
            self._put(stale, "")  # removes the entity in Home Assistant


def _slug(value: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in value).strip("_").lower()
