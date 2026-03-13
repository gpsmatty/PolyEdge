"""Minimal HTTP health-check server for DigitalOcean App Platform.

DO App Platform expects a process to listen on 0.0.0.0:8080 and return
a 2xx response for health/liveness probes. This module starts a lightweight
aiohttp server as a background asyncio task alongside the trading runners.

Usage (inside an async context):
    from polyedge.health import start_health_server
    asyncio.create_task(start_health_server())
"""

from __future__ import annotations

import asyncio
import json
import os

from aiohttp import web

_PORT = int(os.environ.get("PORT", 8080))


async def _handle_health(request: web.Request) -> web.Response:
    return web.Response(
        text=json.dumps({"status": "ok"}),
        content_type="application/json",
        status=200,
    )


async def start_health_server(port: int = _PORT) -> None:
    """Run a tiny HTTP server that returns 200 on / and /health.

    Designed to be launched as an asyncio background task so it doesn't
    block the main trading loop.
    """
    app = web.Application()
    app.router.add_get("/", _handle_health)
    app.router.add_get("/health", _handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    try:
        await site.start()
    except OSError:
        # Port already in use — health-server CMD is already running, nothing to do
        await runner.cleanup()
        return

    # Run forever — cancelled when the parent task is cancelled
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
