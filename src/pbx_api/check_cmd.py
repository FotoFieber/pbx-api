# SPDX-License-Identifier: Apache-2.0
"""``pbx-api check``: verify configuration and connectivity without starting the service."""

from __future__ import annotations

import asyncio
import contextlib
import sys

from .adapters.ami import AmiLive
from .adapters.freepbx_db import FreePbxDatabase
from .config import Config, read_secret
from .domain.live import LiveModel
from .service import PbxService


async def _check(config: Config) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []

    tokens = config.server.tokens()
    results.append(("api tokens", bool(tokens), f"{len(tokens)} configured" if tokens else "none: API unauthenticated"))

    db = FreePbxDatabase(config.freepbx.db)
    svc = PbxService(config, db)
    try:
        await db.start()
        topo = await svc.refresh_topology()
        results.append(("database", True,
                        f"{len(topo.extensions)} extensions, {len(topo.lines)} lines, {len(topo.dids)} DIDs, "
                        f"{len(topo.ring_groups)} ring groups"))
        page = await svc.list_calls(limit=1)
        results.append(("call history", True, f"latest call {page.items[0].started_at:%Y-%m-%d %H:%M}"
                        if page.items else "no calls yet"))
    except Exception as exc:  # noqa: BLE001
        results.append(("database", False, str(exc)))

    if config.ami.enabled:
        model = LiveModel(topology=lambda: svc.topology, clock=svc.now)
        synced = asyncio.Event()

        async def on_change(changes):
            if model.connected:
                synced.set()

        ami = AmiLive(config.ami, model, on_change)
        try:
            await ami.start()
            await asyncio.wait_for(synced.wait(), 10)
            results.append(("ami", True, f"{len(model.device_states)} device states"))
        except Exception as exc:  # noqa: BLE001
            results.append(("ami", False, f"no successful login/resync within 10 s ({exc or 'timeout'})"))
        finally:
            await ami.stop()
    else:
        results.append(("ami", True, "disabled"))

    if config.mqtt.enabled:
        import aiomqtt

        try:
            async with aiomqtt.Client(hostname=config.mqtt.host, port=config.mqtt.port, username=config.mqtt.user,
                                      password=read_secret(config.mqtt.password_file),
                                      identifier=f"pbx-api-check-{config.mqtt.node_id}", timeout=10):
                pass
            results.append(("mqtt", True, f"{config.mqtt.host}:{config.mqtt.port}"))
        except Exception as exc:  # noqa: BLE001
            results.append(("mqtt", False, str(exc)))
    else:
        results.append(("mqtt", True, "disabled"))

    with contextlib.suppress(Exception):
        await db.stop()
    return results


def main(config: Config) -> int:
    results = asyncio.run(_check(config))
    width = max(len(name) for name, _, _ in results)
    for name, ok, detail in results:
        print(f"{'OK ' if ok else 'ERR'}  {name.ljust(width)}  {detail}")
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(1)
