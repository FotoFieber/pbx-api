# SPDX-License-Identifier: Apache-2.0
"""Entry point: ``pbx-api --config /etc/pbx-api/config.yaml``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
import yaml
from pydantic import ValidationError
from fastapi import FastAPI

from .adapters.ami import AmiLive
from .adapters.freepbx_db import FreePbxDatabase
from .adapters.mqtt import MqttProjection
from .app import create_app
from .config import Config, load_config
from .domain.live import Change, LiveModel
from .service import PbxService

log = logging.getLogger("pbx_api")


def describe_access(config: Config) -> str:
    clients = config.server.allowed_clients
    return "allowed clients " + (", ".join(clients) + " + loopback" if clients else "unrestricted")


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
        self.app: FastAPI | None = None

    async def on_live_change(self, changes: list[Change]) -> None:
        if self.mqtt:
            await self.mqtt.on_changes(changes)

    async def refresh(self, reload_config: bool = False) -> None:
        if reload_config:
            # an invalid file raises here and leaves the running configuration untouched
            fresh = load_config(self.config_path)
            tokens = fresh.server.tokens()
            # only settings that are safe to change at runtime
            self.config.overrides = fresh.overrides
            self.config.discovery = fresh.discovery
            self.config.calls = fresh.calls
            self.config.server.allowed_clients = fresh.server.allowed_clients
            if self.app is not None:
                self.app.state.tokens = tokens
                self.app.state.allowed_clients = fresh.server.client_networks()
            log.info("configuration reloaded (overrides, tokens, %s)", describe_access(fresh))
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
            except (ValidationError, OSError, yaml.YAMLError) as exc:
                if not reload_config:
                    log.exception("topology refresh failed")
                    continue
                log.error("configuration %s is invalid, keeping the running settings:\n%s", self.config_path, exc)
            except Exception:  # noqa: BLE001
                log.exception("topology refresh failed")

    @asynccontextmanager
    async def lifespan(self, app: FastAPI) -> AsyncIterator[None]:
        self.app = app
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


def serve(config_path: str) -> int:
    config = load_config(config_path)
    runtime = Runtime(config_path, config)
    tokens = config.server.tokens()
    if not tokens:
        log.warning("no bearer tokens configured: the API is unauthenticated")
    log.info("API access: %s", describe_access(config))
    if not config.server.allowed_clients:
        log.warning("server.allowed_clients is empty: any client IP may reach the API")
    app = create_app(runtime.service, tokens=tokens, lifespan=runtime.lifespan,
                     allowed_clients=config.server.client_networks())
    uvicorn.run(app, host=config.server.bind, port=config.server.port, log_config=None, access_log=False,
                proxy_headers=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pbx-api", description="Business REST API and MQTT bridge for FreePBX")
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="run the service (default)")
    p_run.add_argument("--config", default="/etc/pbx-api/config.yaml")

    p_check = sub.add_parser("check", help="verify configuration and connectivity")
    p_check.add_argument("--config", default="/etc/pbx-api/config.yaml")

    from . import setup_cmd

    p_setup = sub.add_parser("setup", help="prepare this PBX host (run as root)",
                             description=setup_cmd.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    setup_cmd.add_arguments(p_setup)

    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.command == "setup":
        return setup_cmd.main(args)
    if args.command == "check":
        from . import check_cmd

        logging.getLogger().setLevel(logging.WARNING)
        return check_cmd.main(load_config(args.config))
    return serve(getattr(args, "config", "/etc/pbx-api/config.yaml"))


if __name__ == "__main__":
    sys.exit(main())
