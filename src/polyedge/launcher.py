"""PolyEdge Launcher — persistent orchestrator for DigitalOcean App Platform.

Runs as PID 1 in the container. Keeps the health server alive permanently
so DO health probes always pass, while allowing the trading strategy to be
started/stopped without killing the container.

Control via HTTP endpoints on the health server (port 8080):
    GET  /status  → {"strategy": "micro", "running": true, ...}
    POST /stop    → gracefully stop the strategy
    POST /start   → restart the strategy
    GET  /health  → always 200 (DO probe)

From the DO Console:
    curl localhost:8080/stop
    curl -X POST localhost:8080/stop
    curl localhost:8080/start
    curl -X POST localhost:8080/start
    curl localhost:8080/status
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

from aiohttp import web
from rich.console import Console

logger = logging.getLogger(__name__)

# Force stdout for DO runtime log capture
console = Console(force_terminal=True, force_jupyter=False, file=sys.stdout, highlight=False)

_PORT = int(os.environ.get("PORT", 8080))


class Launcher:
    """Orchestrates health server + trading strategy lifecycle."""

    def __init__(
        self,
        strategy: str = "micro",
        strategy_args: dict | None = None,
        port: int = _PORT,
        paused: bool = False,
    ):
        self.strategy = strategy
        self.strategy_args = strategy_args or {}
        self.port = port
        self.paused = paused

        # State
        self._strategy_task: asyncio.Task | None = None
        self._running = False
        self._started_at: float | None = None
        self._stopped_at: float | None = None
        self._stop_requested = False

        # Resources (initialized in run())
        self._settings = None
        self._db = None
        self._client = None

    @property
    def is_running(self) -> bool:
        return self._running and self._strategy_task is not None and not self._strategy_task.done()

    async def _init_resources(self):
        """Initialize settings, DB, and client once."""
        from polyedge.core.config import load_config, apply_db_config
        from polyedge.core.client import PolyClient
        from polyedge.core.db import Database

        self._settings = load_config()

        if not self._settings.poly_private_key:
            console.print("[red]Wallet not configured. Set POLY_PRIVATE_KEY env var.[/red]")
            return False

        self._db = Database(self._settings.database_url)
        await self._db.connect()
        await self._db.init_schema()

        self._settings = await apply_db_config(self._settings, self._db)
        self._client = PolyClient(self._settings)
        return True

    async def _run_micro(self):
        """Run the micro sniper strategy."""
        from polyedge.strategies.micro_runner import MicroRunner

        args = self.strategy_args
        runner = MicroRunner(
            settings=self._settings,
            client=self._client,
            db=self._db,
            auto_execute=args.get("auto", True),
            dry_run=args.get("dry", False),
            verbose=args.get("verbose", False),
            quiet=args.get("quiet", False),
            market_filter=args.get("market", None),
            skip_warmup=args.get("no_warmup", False),
        )
        await runner.run()

    async def _run_sniper(self):
        """Run the crypto sniper strategy."""
        from polyedge.strategies.sniper_runner import SniperRunner

        args = self.strategy_args
        runner = SniperRunner(
            settings=self._settings,
            client=self._client,
            db=self._db,
            auto_execute=args.get("auto", True),
            dry_run=args.get("dry", False),
        )
        await runner.run()

    async def _run_weather(self):
        """Run the weather sniper strategy."""
        from polyedge.strategies.weather_runner import WeatherRunner

        args = self.strategy_args
        runner = WeatherRunner(
            settings=self._settings,
            client=self._client,
            db=self._db,
            auto_execute=args.get("auto", True),
            dry_run=args.get("dry", False),
        )
        await runner.run()

    async def start_strategy(self) -> str:
        """Start the trading strategy. Returns status message."""
        if self.is_running:
            return f"Strategy '{self.strategy}' is already running."

        self._stop_requested = False

        # Re-apply DB config in case it changed while stopped
        if self._db and self._settings:
            from polyedge.core.config import apply_db_config
            self._settings = await apply_db_config(self._settings, self._db)

        strategy_map = {
            "micro": self._run_micro,
            "sniper": self._run_sniper,
            "weather": self._run_weather,
        }

        runner_fn = strategy_map.get(self.strategy)
        if not runner_fn:
            return f"Unknown strategy: {self.strategy}"

        self._strategy_task = asyncio.create_task(self._strategy_wrapper(runner_fn))
        self._running = True
        self._started_at = time.time()
        self._stopped_at = None

        msg = f"Strategy '{self.strategy}' started."
        console.print(f"[bold green]{msg}[/bold green]")
        logger.info(msg)
        return msg

    async def _strategy_wrapper(self, runner_fn):
        """Wraps the strategy runner to catch errors and update state."""
        try:
            await runner_fn()
        except asyncio.CancelledError:
            console.print(f"[yellow]Strategy '{self.strategy}' stopped.[/yellow]")
        except Exception as e:
            console.print(f"[red]Strategy '{self.strategy}' crashed: {e}[/red]")
            logger.exception(f"Strategy crashed: {e}")
        finally:
            self._running = False
            self._stopped_at = time.time()

    async def stop_strategy(self) -> str:
        """Stop the trading strategy. Returns status message."""
        if not self.is_running:
            return f"Strategy '{self.strategy}' is not running."

        self._stop_requested = True
        self._strategy_task.cancel()

        # Wait for clean shutdown (up to 10s)
        try:
            await asyncio.wait_for(asyncio.shield(self._strategy_task), timeout=10.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

        self._running = False
        self._stopped_at = time.time()

        msg = f"Strategy '{self.strategy}' stopped."
        console.print(f"[yellow]{msg}[/yellow]")
        logger.info(msg)
        return msg

    def get_status(self) -> dict:
        """Get current launcher status."""
        uptime = None
        if self._started_at:
            ref = time.time() if self.is_running else (self._stopped_at or time.time())
            uptime = int(ref - self._started_at)

        return {
            "strategy": self.strategy,
            "running": self.is_running,
            "strategy_args": self.strategy_args,
            "started_at": datetime.fromtimestamp(self._started_at, tz=timezone.utc).isoformat() if self._started_at else None,
            "stopped_at": datetime.fromtimestamp(self._stopped_at, tz=timezone.utc).isoformat() if self._stopped_at else None,
            "uptime_seconds": uptime,
            "health": "ok",
        }

    # --- HTTP Handlers ---

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.Response(
            text=json.dumps({"status": "ok"}),
            content_type="application/json",
            status=200,
        )

    async def _handle_status(self, request: web.Request) -> web.Response:
        return web.Response(
            text=json.dumps(self.get_status(), indent=2),
            content_type="application/json",
            status=200,
        )

    async def _handle_stop(self, request: web.Request) -> web.Response:
        msg = await self.stop_strategy()
        return web.Response(
            text=json.dumps({"message": msg, **self.get_status()}),
            content_type="application/json",
            status=200,
        )

    async def _handle_start(self, request: web.Request) -> web.Response:
        # Accept optional JSON body to override strategy + args:
        #   curl localhost:8080/start
        #   curl -X POST localhost:8080/start -d '{"strategy":"micro","market":"btc 5m"}'
        #   curl "localhost:8080/start?strategy=sniper"
        try:
            if request.content_type == "application/json":
                body = await request.json()
            else:
                body = {}
        except Exception:
            body = {}

        # Also accept query params
        params = dict(request.query)
        body.update(params)

        # Override strategy if provided
        if "strategy" in body:
            self.strategy = body["strategy"]

        # Override strategy_args from body
        arg_keys = ["market", "auto", "dry", "verbose", "quiet", "no_warmup"]
        for key in arg_keys:
            if key in body:
                val = body[key]
                # Parse string booleans
                if isinstance(val, str) and val.lower() in ("true", "1", "yes"):
                    val = True
                elif isinstance(val, str) and val.lower() in ("false", "0", "no"):
                    val = False
                self.strategy_args[key] = val

        msg = await self.start_strategy()
        return web.Response(
            text=json.dumps({"message": msg, **self.get_status()}, indent=2),
            content_type="application/json",
            status=200,
        )

    async def run(self):
        """Main entry point. Starts health server + strategy, runs forever."""
        console.print(f"[bold cyan]PolyEdge Launcher starting...[/bold cyan]")
        console.print(f"[dim]Strategy: {self.strategy} | Args: {self.strategy_args}[/dim]")
        console.print(f"[dim]Health + control server on 0.0.0.0:{self.port}[/dim]")
        console.print(f"[dim]Endpoints: /health /status /start /stop[/dim]")

        # Initialize DB, client, etc.
        ok = await self._init_resources()
        if not ok:
            console.print("[red]Failed to initialize. Running health server only.[/red]")

        # Build aiohttp app with all routes
        app = web.Application()
        app.router.add_get("/", self._handle_health)
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/status", self._handle_status)
        # Accept both GET and POST for convenience (curl from DO console)
        app.router.add_get("/stop", self._handle_stop)
        app.router.add_post("/stop", self._handle_stop)
        app.router.add_get("/start", self._handle_start)
        app.router.add_post("/start", self._handle_start)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.port)
        await site.start()
        console.print(f"[green]Health + control server listening on 0.0.0.0:{self.port}[/green]")

        # Auto-start the strategy unless --paused
        if ok and not self.paused:
            await self.start_strategy()
        elif self.paused:
            console.print(f"[yellow]Started paused — curl localhost:{self.port}/start to begin trading[/yellow]")

        # Run forever
        try:
            await asyncio.Event().wait()
        finally:
            if self.is_running:
                await self.stop_strategy()
            if self._db:
                await self._db.close()
            await runner.cleanup()
