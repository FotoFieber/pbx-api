# SPDX-License-Identifier: Apache-2.0
"""Entry point: ``pbx-api --config /etc/pbx-api/config.yaml``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from .adapters.ami import AmiLive
from .adapters.freepbx_db import FreePbxDatabase
from .adapters.mqtt import MqttProjection
from .app import create_app
from .config import Config, load_config
from .domain.live import Change, LiveModel
from .service import PbxService

log = logging.getLogger("pbx_api")


class Runtime:
    def __init__(self, config_path: str, config: Config) -> None:
        self.config_path = config_path
        self.config = config
        self.db = FreePbxDatabase(config.freepbx.db)
        self.service = PbxService(config, self.db)
        self.mqtt = MqttProjection(config.mqtt, self.service) if config.mqtt.enabled else None
        self.ami: AmiLive | None = None
        if config.ami.enabled:
            model = LiveModel(topology=lambda: self.service.topology, clock=self.service.now)
            self.ami = AmiLive(config.ami, model, self.on_live_change)
            self.service.live = self.ami
        self._refresh_task: asyncio.Task | None = None
        self._reload = asyncio.Event()

    async def on_live_change(self, changes: list[Change]) -> None:
        if self.mqtt:
            await self.mqtt.on_changes(changes)

    async def refresh(self, reload_config: bool = False) -> None:
        if reload_config:
            fresh = load_config(self.config_path)
            # only settings that are safe to change at runtime
            self.config.overrides = fresh.overrides
            self.config.discovery = fresh.discovery
            self.config.calls = fresh.calls
            log.info("configuration overrides reloaded")
        await self.service.refresh_topology()
        if self.mqtt:
            await self.mqtt.publish_all()

    async def _refresh_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._reload.wait(), timeout=self.config.discovery.refresh_sec)
                reload_config = True
            except TimeoutError:
                reload_config = False
            self._reload.clear()
            try:
                await self.refresh(reload_config)
            except Exception:  # noqa: BLE001
                log.exception("topology refresh failed")

    @asynccontextmanager
    async def lifespan(self, _app: FastAPI) -> AsyncIterator[None]:
        await self.db.start()
        await self.service.refresh_topology()
        if self.ami:
            await self.ami.start()
        if self.mqtt:
            await self.mqtt.start()
        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGHUP, self._reload.set)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - non-POSIX
            pass
        self._refresh_task = asyncio.create_task(self._refresh_loop(), name="refresh")
        try:
            yield
        finally:
            self._refresh_task.cancel()
            if self.mqtt:
                await self.mqtt.stop()
            if self.ami:
                await self.ami.stop()
            await self.db.stop()


def main() -> None:
    parser = argparse.ArgumentParser(prog="pbx-api", description=__doc__)
    parser.add_argument("--config", default="/etc/pbx-api/config.yaml")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    config = load_config(args.config)
    runtime = Runtime(args.config, config)
    tokens = config.server.tokens()
    if not tokens:
        log.warning("no bearer tokens configured: the API is unauthenticated")
    app = create_app(runtime.service, tokens=tokens, lifespan=runtime.lifespan)
    uvicorn.run(app, host=config.server.bind, port=config.server.port, log_config=None, access_log=False,
                proxy_headers=False)


if __name__ == "__main__":
    main()
