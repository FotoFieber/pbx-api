# SPDX-License-Identifier: Apache-2.0
"""Asterisk Manager Interface connection feeding :class:`LiveModel`.

panoramisk handles reconnects itself; after every successful login the live model
is rebuilt from ``DeviceStateList`` and ``CoreShowChannels``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from panoramisk import Manager

from ..config import AmiConfig, read_secret
from ..domain.live import Change, LiveModel

log = logging.getLogger(__name__)

EVENTS = ("DeviceStateChange", "Newchannel", "Newstate", "NewConnectedLine", "Hangup")


class AmiLive:
    """Keeps a LiveModel in sync with Asterisk and reports changes to a callback."""

    def __init__(self, cfg: AmiConfig, model: LiveModel, on_change: Callable[[list[Change]], Awaitable[None]]):
        self._cfg = cfg
        self.model = model
        self._on_change = on_change
        self._manager: Manager | None = None
        self._tasks: set[asyncio.Task] = set()

    # ---- LiveState protocol (delegated to the model) ------------------------------

    @property
    def connected(self) -> bool:
        return self.model.connected

    def extension_status(self, ext):
        return self.model.extension_status(ext)

    def line_registered(self, line_id):
        return self.model.line_registered(line_id)

    def active_calls(self):
        return self.model.active_calls()

    # ---- lifecycle --------------------------------------------------------------

    async def start(self) -> None:
        self._manager = Manager(
            loop=asyncio.get_running_loop(),
            host=self._cfg.host,
            port=self._cfg.port,
            username=self._cfg.user,
            secret=read_secret(self._cfg.secret_file) or "",
            reconnect_timeout=5,
            on_login=self._on_login,
            on_disconnect=self._on_disconnect,
            log=log,
        )
        for name in EVENTS:
            self._manager.register_event(name, self._dispatch)
        self._manager.connect()

    async def stop(self) -> None:
        if self._manager:
            # prevent panoramisk from scheduling a reconnect after close
            self._manager.connection_lost = lambda exc: None  # type: ignore[method-assign]
            self._manager.close()
        for task in self._tasks:
            task.cancel()

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---- callbacks --------------------------------------------------------------

    def _on_login(self, manager: Manager) -> None:
        log.info("AMI logged in to %s:%s", self._cfg.host, self._cfg.port)
        self._spawn(self._resync(manager))

    def _on_disconnect(self, _manager: Manager, exc: Exception | None) -> None:
        log.warning("AMI disconnected: %s", exc)
        self.model.connected = False
        self._spawn(self._on_change([Change("resync", "ami")]))

    async def _dispatch(self, _manager: Manager, message) -> None:
        try:
            changes = self.model.handle(message)
            if changes:
                await self._on_change(changes)
        except Exception:  # noqa: BLE001 - never kill the event loop on one bad event
            log.exception("failed to handle AMI event %s", message.get("Event"))

    async def _resync(self, manager: Manager) -> None:
        try:
            self.model.reset()
            for action in ("DeviceStateList", "CoreShowChannels"):
                # without the timeout a single error response (e.g. permission denied)
                # would leave the list future pending forever
                response = await asyncio.wait_for(manager.send_action({"Action": action}, as_list=True), 15)
                messages = response if isinstance(response, list) else [response]
                if messages and messages[0].get("Response") == "Error":
                    raise RuntimeError(f"AMI action {action} failed: {messages[0].get('Message')}")
                for msg in messages:
                    self.model.handle(msg)
            self.model.connected = True
            log.info(
                "AMI state synced: %d devices, %d channels",
                len(self.model.device_states),
                len(self.model.channels),
            )
        except Exception:  # noqa: BLE001
            log.exception("AMI resync failed")
        await self._on_change([Change("resync", "ami")])
