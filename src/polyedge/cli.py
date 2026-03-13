"""PolyEdge CLI — command-line interface for the trading bot."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from polyedge.core.config import load_config

# Force stdout, no TTY auto-detection — ensures output appears in DO runtime logs
console = Console(file=sys.stdout, highlight=False)


def setup_logging() -> None:
    """Route all logging module output to stdout for DO runtime log capture."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(handler)


def run_async(coro):
    """Helper to run async functions from Click commands."""
    return asyncio.get_event_loop().run_until_complete(coro)


def get_settings():
    return load_config()


@click.group()
@click.option("--config", "-c", default=None, help="Path to config YAML file")
@click.pass_context
def cli(ctx, config):
    """PolyEdge — AI-powered Polymarket trading bot."""
    setup_logging()
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config


@cli.command("health-server")
@click.option("--port", default=8080, help="Port to listen on (default 8080)")
def health_server(port):
    """Run only the health-check HTTP server (no trading).

    Used as the container entrypoint on DigitalOcean App Platform so the
    process stays alive and passes readiness probes while you start trading
    strategies manually from the console.
    """
    from polyedge.health import start_health_server

    console.print(f"[cyan]Health server listening on 0.0.0.0:{port}[/cyan]")
    run_async(start_health_server(port=port))


# --- Setup ---


@cli.command()
def setup():
    """Generate a new wallet and derive API credentials."""
    from polyedge.core.client import PolyClient

    console.print("[bold cyan]PolyEdge Setup[/bold cyan]\n")

    settings = get_settings()

    from polyedge.core.config import _set_in_keychain

    if settings.poly_private_key:
        console.print("[yellow]Wallet already configured")
        if not click.confirm("Generate a NEW wallet?", default=False):
            # Just derive API keys
            console.print("\nDeriving API credentials...")
            client = PolyClient(settings)
            try:
                creds = client.derive_api_keys()
                console.print("[green]API credentials derived successfully!")
                console.print(f"  API Key: {creds['api_key'][:20]}...")
                console.print(f"  Secret: {creds['api_secret'][:20]}...")
                console.print(f"  Passphrase: {creds['api_passphrase'][:20]}...")

                if click.confirm("\nStore these in macOS Keychain?", default=True):
                    _set_in_keychain("poly_api_key", creds['api_key'])
                    _set_in_keychain("poly_api_secret", creds['api_secret'])
                    _set_in_keychain("poly_api_passphrase", creds['api_passphrase'])
                    console.print("[green]Stored in Keychain!")
                else:
                    console.print("\n[yellow]Add these to your .env file:")
                    console.print(f"  POLY_API_KEY={creds['api_key']}")
                    console.print(f"  POLY_API_SECRET={creds['api_secret']}")
                    console.print(f"  POLY_API_PASSPHRASE={creds['api_passphrase']}")
            except Exception as e:
                console.print(f"[red]Failed to derive credentials: {e}")
            return

    # Generate new wallet
    console.print("Generating new trading wallet...")
    wallet = PolyClient.generate_wallet()

    console.print(f"\n[green]Wallet generated!")
    console.print(f"  Address: {wallet['address']}")
    console.print(f"  Private Key: {wallet['private_key'][:20]}...")

    if click.confirm("\nStore wallet keys in macOS Keychain?", default=True):
        _set_in_keychain("poly_private_key", wallet['private_key'])
        _set_in_keychain("poly_wallet_address", wallet['address'])
        console.print("[green]Stored in Keychain!")
    else:
        console.print("\n[yellow]Add to your .env file:")
        console.print(f"  POLY_PRIVATE_KEY={wallet['private_key']}")
        console.print(f"  POLY_WALLET_ADDRESS={wallet['address']}")

    console.print(
        "\n[bold]Next steps:"
        "\n  1. Send $200 USDC to the address on Polygon"
        "\n  2. Send ~$0.50 of MATIC/POL for gas"
        "\n  3. Run 'polyedge setup' again to derive API credentials"
    )


@cli.command()
def init():
    """First-run wizard — configure secrets, database, and trading params."""
    from polyedge.core.config import (
        KEYCHAIN_KEYS, _get_from_keychain, _set_in_keychain, load_keychain_secrets,
        save_config_to_db,
    )

    console.print("[bold cyan]PolyEdge First-Run Setup[/bold cyan]")
    console.print("[dim]This wizard walks you through everything needed to start trading.\n")

    # --- Step 1: Secrets ---
    console.print("[bold]Step 1: Secrets (macOS Keychain)[/bold]")
    console.print("[dim]All secrets are stored encrypted in your Mac's Keychain.\n")

    existing = load_keychain_secrets()

    secret_prompts = {
        "database_url": ("PostgreSQL connection string", "postgresql://user:pass@host:5432/polyedge"),
        "anthropic_api_key": ("Anthropic API key (for Claude)", "sk-ant-..."),
        "openai_api_key": ("OpenAI API key (optional, for ensemble mode)", "sk-..."),
        "news_api_key": ("News API key (optional, for news context)", ""),
    }

    for key, (label, hint) in secret_prompts.items():
        if key in existing:
            masked = existing[key][:4] + "..." + existing[key][-4:] if len(existing[key]) > 12 else "***"
            console.print(f"  [green]{label}[/green]: already set ({masked})")
            if not click.confirm(f"  Update {key}?", default=False):
                continue

        if hint:
            console.print(f"  [dim]Format: {hint}[/dim]")

        if "optional" in label.lower():
            if not click.confirm(f"  Set up {label}?", default=False):
                continue

        value = click.prompt(f"  {label}", hide_input=True)
        if value.strip():
            _set_in_keychain(key, value.strip())
            console.print(f"  [green]Stored![/green]")
        else:
            console.print(f"  [dim]Skipped[/dim]")

    # --- Step 2: Database ---
    console.print(f"\n[bold]Step 2: Database[/bold]")
    db_ready = False
    if _get_from_keychain("database_url"):
        if click.confirm("  Initialize database schema now?", default=True):
            async def _init_db():
                from polyedge.core.db import Database
                settings = get_settings()
                db = Database(settings.database_url)
                await db.connect()
                await db.init_schema()
                await db.close()
                console.print("  [green]Database schema created (10 tables)![/green]")
            try:
                run_async(_init_db())
                db_ready = True
            except Exception as e:
                console.print(f"  [red]Database error: {e}[/red]")
                console.print("  [dim]Check your database_url and try again later with 'polyedge initdb'[/dim]")
    else:
        console.print("  [yellow]No database_url set — skipping. Run 'polyedge vault store database_url' first.[/yellow]")

    # --- Step 3: Wallet ---
    console.print(f"\n[bold]Step 3: Wallet[/bold]")
    if _get_from_keychain("poly_private_key"):
        masked = _get_from_keychain("poly_private_key")
        masked = masked[:6] + "..." + masked[-4:]
        console.print(f"  [green]Wallet already configured[/green] ({masked})")
    else:
        if click.confirm("  Generate a new trading wallet?", default=True):
            from polyedge.core.client import PolyClient
            wallet = PolyClient.generate_wallet()
            _set_in_keychain("poly_private_key", wallet['private_key'])
            _set_in_keychain("poly_wallet_address", wallet['address'])
            console.print(f"  [green]Wallet generated and stored in Keychain![/green]")
            console.print(f"  Address: {wallet['address']}")
            console.print(f"\n  [bold yellow]Fund this address with USDC on Polygon + tiny MATIC for gas[/bold yellow]")
        else:
            console.print("  [dim]Import existing wallet with 'polyedge vault store poly_private_key'[/dim]")

    # --- Step 4: Trading Configuration ---
    console.print(f"\n[bold]Step 4: Trading Configuration[/bold]")
    console.print("[dim]All config is stored in the database — portable across environments.\n")

    settings = get_settings()
    risk = settings.risk
    agent = settings.agent
    ai = settings.ai

    # Bankroll-based risk
    bankroll = click.prompt(
        "  Starting bankroll (USD)",
        type=float, default=200.0,
    )

    # Risk appetite
    console.print("\n  [bold]Risk profile:[/bold]")
    console.print("    1. Conservative — Quarter Kelly, 5% min edge, 10% max position")
    console.print("    2. Moderate     — Half Kelly, 4% min edge, 15% max position")
    console.print("    3. Aggressive   — Full Kelly, 3% min edge, 25% max position")
    console.print("    4. Custom       — Set everything manually")

    profile = click.prompt("  Choose profile", type=int, default=1)

    if profile == 1:
        risk.kelly_fraction = 0.25
        risk.min_edge_threshold = 0.05
        risk.min_confidence = 0.60
        risk.max_position_pct = 0.10
        risk.max_exposure_pct = 0.50
        risk.max_positions = 10
        risk.daily_loss_limit_pct = 0.15
        risk.drawdown_circuit_breaker = 0.25
    elif profile == 2:
        risk.kelly_fraction = 0.50
        risk.min_edge_threshold = 0.04
        risk.min_confidence = 0.55
        risk.max_position_pct = 0.15
        risk.max_exposure_pct = 0.60
        risk.max_positions = 15
        risk.daily_loss_limit_pct = 0.20
        risk.drawdown_circuit_breaker = 0.30
    elif profile == 3:
        risk.kelly_fraction = 1.0
        risk.min_edge_threshold = 0.03
        risk.min_confidence = 0.50
        risk.max_position_pct = 0.25
        risk.max_exposure_pct = 0.75
        risk.max_positions = 20
        risk.daily_loss_limit_pct = 0.25
        risk.drawdown_circuit_breaker = 0.40
    elif profile == 4:
        risk.kelly_fraction = click.prompt("  Kelly fraction (0.25 = quarter)", type=float, default=risk.kelly_fraction)
        risk.min_edge_threshold = click.prompt("  Min edge to trade (0.05 = 5%)", type=float, default=risk.min_edge_threshold)
        risk.min_confidence = click.prompt("  Min AI confidence (0.60 = 60%)", type=float, default=risk.min_confidence)
        risk.max_position_pct = click.prompt("  Max single position (% of bankroll)", type=float, default=risk.max_position_pct)
        risk.max_exposure_pct = click.prompt("  Max total exposure (% of bankroll)", type=float, default=risk.max_exposure_pct)
        risk.max_positions = click.prompt("  Max concurrent positions", type=int, default=risk.max_positions)
        risk.daily_loss_limit_pct = click.prompt("  Daily loss limit (%)", type=float, default=risk.daily_loss_limit_pct)
        risk.drawdown_circuit_breaker = click.prompt("  Drawdown circuit breaker (%)", type=float, default=risk.drawdown_circuit_breaker)

    risk.max_trades_per_day = click.prompt("\n  Max trades per day", type=int, default=risk.max_trades_per_day)

    # AI budget
    ai.max_analysis_cost_per_day = click.prompt(
        "  AI budget per day (USD)",
        type=float, default=ai.max_analysis_cost_per_day,
    )

    # Agent mode
    console.print("\n  [bold]Starting mode:[/bold]")
    console.print("    signals  — AI finds edges, shows you, doesn't trade")
    console.print("    copilot  — AI recommends, you approve each trade")
    console.print("    autopilot — fully autonomous trading")
    agent.mode = click.prompt(
        "  Mode", type=click.Choice(["signals", "copilot", "autopilot"]),
        default=agent.mode,
    )

    # Scan frequency
    agent.scan_interval_minutes = click.prompt(
        "  Scan interval (minutes)",
        type=int, default=agent.scan_interval_minutes,
    )

    # Save config to database
    if db_ready:
        async def _save_config():
            from polyedge.core.db import Database
            db = Database(settings.database_url)
            await db.connect()
            await save_config_to_db(settings, db)
            await db.close()

        try:
            run_async(_save_config())
            console.print(f"\n  [green]Config saved to database (portable across environments)[/green]")
        except Exception as e:
            console.print(f"\n  [red]Failed to save config to DB: {e}[/red]")
            console.print(f"  [dim]You can save later with 'polyedge config save'[/dim]")
    else:
        console.print(f"\n  [yellow]Database not available — config not saved.[/yellow]")
        console.print(f"  [dim]Run 'polyedge init' again after setting up the database.[/dim]")

    # --- Summary ---
    console.print(f"\n[bold cyan]Setup Complete![/bold cyan]\n")

    loss_limit = bankroll * risk.daily_loss_limit_pct
    breaker = bankroll * (1 - risk.drawdown_circuit_breaker)
    max_pos = bankroll * risk.max_position_pct

    console.print(f"  Bankroll:           ${bankroll:,.0f}")
    console.print(f"  Risk profile:       {'Conservative' if profile == 1 else 'Moderate' if profile == 2 else 'Aggressive' if profile == 3 else 'Custom'}")
    console.print(f"  Kelly fraction:     {risk.kelly_fraction:.0%}")
    console.print(f"  Max position:       ${max_pos:,.0f} ({risk.max_position_pct:.0%} of bankroll)")
    console.print(f"  Daily loss limit:   ${loss_limit:,.0f}")
    console.print(f"  Circuit breaker:    pauses at ${breaker:,.0f}")
    console.print(f"  AI budget:          ${ai.max_analysis_cost_per_day:.2f}/day")
    console.print(f"  Mode:               {agent.mode}")
    console.print(f"  Scan every:         {agent.scan_interval_minutes} min")

    console.print(f"\n[bold]Next steps:")
    if not _get_from_keychain("poly_private_key"):
        console.print("  1. Generate wallet:  polyedge setup")
        console.print("  2. Fund wallet with USDC on Polygon")
    console.print(f"  {'3' if not _get_from_keychain('poly_private_key') else '1'}. Start trading:    polyedge autopilot")
    console.print(f"  [dim]View config:      polyedge config show[/dim]")
    console.print(f"  [dim]Change a value:   polyedge config set risk.kelly_fraction 0.5[/dim]")


# --- Market Data ---


@cli.command()
@click.option("--limit", "-l", default=20, help="Number of markets to show")
@click.option("--min-liquidity", default=1000.0, help="Minimum liquidity in USDC")
@click.option("--category", "-cat", default=None, help="Filter by category")
def scan(limit, min_liquidity, category):
    """Scan active Polymarket markets."""

    async def _scan():
        from polyedge.data.markets import fetch_active_markets

        settings = get_settings()
        console.print("[dim]Fetching markets...[/dim]")

        markets, _ = await fetch_active_markets(
            settings,
            limit=limit,
            min_liquidity=min_liquidity,
            category=category,
        )

        if not markets:
            console.print("[yellow]No markets found")
            return

        table = Table(title=f"Active Markets ({len(markets)})")
        table.add_column("#", width=3)
        table.add_column("Question", max_width=50)
        table.add_column("YES", justify="right", width=7)
        table.add_column("NO", justify="right", width=7)
        table.add_column("Volume", justify="right", width=12)
        table.add_column("Liquidity", justify="right", width=12)
        table.add_column("Category", width=15)

        for i, m in enumerate(markets[:limit], 1):
            table.add_row(
                str(i),
                m.question[:50],
                f"${m.yes_price:.2f}",
                f"${m.no_price:.2f}",
                f"${m.volume:,.0f}",
                f"${m.liquidity:,.0f}",
                m.category[:15] if m.category else "",
            )

        console.print(table)

    run_async(_scan())


@cli.command()
@click.argument("query")
def search(query):
    """Search markets by keyword."""

    async def _search():
        from polyedge.data.markets import search_markets

        settings = get_settings()
        console.print(f"[dim]Searching for '{query}'...[/dim]")
        markets = await search_markets(settings, query)

        if not markets:
            console.print("[yellow]No markets found")
            return

        for i, m in enumerate(markets[:10], 1):
            console.print(
                f"  {i}. {m.question[:60]} — "
                f"YES: ${m.yes_price:.2f} | "
                f"Vol: ${m.volume:,.0f}"
            )

    run_async(_search())


@cli.command()
@click.argument("market_query")
def price(market_query):
    """Get current price for a market (search by keyword)."""

    async def _price():
        from polyedge.data.markets import search_markets

        settings = get_settings()
        markets = await search_markets(settings, market_query)

        if not markets:
            console.print("[yellow]No matching markets found")
            return

        market = markets[0]
        console.print(f"\n[bold]{market.question}")
        console.print(f"  YES: [green]${market.yes_price:.3f}[/green] ({market.yes_price*100:.1f}%)")
        console.print(f"  NO:  [red]${market.no_price:.3f}[/red] ({market.no_price*100:.1f}%)")
        console.print(f"  Volume: ${market.volume:,.0f} | Liquidity: ${market.liquidity:,.0f}")
        if market.hours_to_resolution:
            hrs = market.hours_to_resolution
            if hrs < 24:
                console.print(f"  Resolves in: {hrs:.1f} hours")
            else:
                console.print(f"  Resolves in: {hrs/24:.1f} days")

    run_async(_price())


# --- Strategies ---


@cli.command()
@click.option("--limit", "-l", default=20, help="Number of results")
@click.option("--max-price", default=0.15, help="Maximum price threshold")
def hunt(limit, max_price):
    """Run the Cheap Event Hunter — find underpriced tail events."""

    async def _hunt():
        from polyedge.data.markets import fetch_all_markets
        from polyedge.strategies.cheap_hunter import CheapHunterStrategy
        from polyedge.risk.sizing import calculate_position_size

        settings = get_settings()
        settings.strategies.cheap_hunter.max_price = max_price

        console.print("[dim]Scanning for cheap events...[/dim]")
        markets = await fetch_all_markets(settings, min_liquidity=settings.risk.min_liquidity)

        strategy = CheapHunterStrategy(settings)
        signals = strategy.evaluate_batch(markets)

        if not signals:
            console.print("[yellow]No cheap event opportunities found")
            return

        table = Table(title=f"Cheap Events ({len(signals)} found)")
        table.add_column("#", width=3)
        table.add_column("Market", max_width=45)
        table.add_column("Side", width=5)
        table.add_column("Price", justify="right", width=7)
        table.add_column("Edge", justify="right", width=7)
        table.add_column("EV/$ ", justify="right", width=7)
        table.add_column("Size $", justify="right", width=8)

        for i, sig in enumerate(signals[:limit], 1):
            size = calculate_position_size(
                bankroll=200.0,
                edge=sig.edge,
                probability=sig.edge + (sig.market.yes_price if sig.side.value == "YES" else sig.market.no_price),
                kelly_fraction=settings.risk.kelly_fraction,
                max_position_pct=settings.risk.max_position_pct,
            )
            price = sig.market.yes_price if sig.side.value == "YES" else sig.market.no_price
            table.add_row(
                str(i),
                sig.market.question[:45],
                sig.side.value,
                f"${price:.3f}",
                f"{sig.edge*100:.1f}%",
                f"{sig.ev:.2f}",
                f"${size:.2f}" if size > 0 else "-",
            )

        console.print(table)

    run_async(_hunt())


@cli.command()
@click.option("--limit", "-l", default=10, help="Number of markets to analyze")
@click.option("--provider", "-p", default=None, help="AI provider: claude, openai, ensemble")
def edges(limit, provider):
    """Run the AI Edge Finder — find mispricings using LLM analysis."""

    async def _edges():
        from polyedge.data.markets import fetch_all_markets
        from polyedge.ai.llm import LLMClient
        from polyedge.ai.analyst import analyze_market
        from polyedge.ai.news import get_news_context
        from polyedge.strategies.edge_finder import EdgeFinderStrategy

        settings = get_settings()

        if not settings.anthropic_api_key and not settings.openai_api_key:
            console.print("[red]No AI API keys configured. Set ANTHROPIC_API_KEY or OPENAI_API_KEY in .env")
            return

        llm = LLMClient(
            settings.ai,
            anthropic_key=settings.anthropic_api_key,
            openai_key=settings.openai_api_key,
        )

        console.print("[dim]Fetching markets...[/dim]")
        markets = await fetch_all_markets(settings, min_liquidity=settings.risk.min_liquidity)
        markets = markets[:limit]

        console.print(f"[dim]AI analyzing {len(markets)} markets...[/dim]")

        strategy = EdgeFinderStrategy(settings)
        signals = []

        for market in markets:
            try:
                news = await get_news_context(llm, market, settings.news_api_key)
                analysis = await analyze_market(
                    llm, market, news_context=news,
                    provider=provider or settings.ai.provider,
                )

                signal = strategy.evaluate_with_analysis(market, analysis)
                if signal:
                    signals.append(signal)

                console.print(
                    f"  [{analysis.provider}] {market.question[:50]} — "
                    f"AI: {analysis.probability*100:.0f}% vs Market: {market.yes_price*100:.0f}% "
                    f"(edge: {abs(analysis.probability - market.yes_price)*100:.1f}%)"
                )
            except Exception as e:
                console.print(f"  [red]Failed: {market.question[:40]}: {e}")

        if signals:
            signals.sort(key=lambda s: s.ev, reverse=True)
            console.print(f"\n[bold green]Found {len(signals)} edges!")
            for i, sig in enumerate(signals[:10], 1):
                console.print(
                    f"  {i}. {sig.side.value} '{sig.market.question[:50]}' "
                    f"| Edge: {sig.edge*100:.1f}% | AI: {sig.ai_probability*100:.0f}%"
                )
        else:
            console.print("[yellow]No significant edges found")

        console.print(f"\n[dim]AI cost: ${llm.total_cost_today:.4f}[/dim]")

    run_async(_edges())


@cli.command()
@click.argument("market_query")
@click.option("--provider", "-p", default=None, help="AI provider")
def analyze(market_query, provider):
    """Deep-dive AI analysis of a specific market."""

    async def _analyze():
        from polyedge.data.markets import search_markets
        from polyedge.ai.llm import LLMClient
        from polyedge.ai.analyst import analyze_market
        from polyedge.ai.news import get_news_context

        settings = get_settings()

        if not settings.anthropic_api_key and not settings.openai_api_key:
            console.print("[red]No AI API keys configured")
            return

        markets = await search_markets(settings, market_query)
        if not markets:
            console.print("[yellow]No matching markets found")
            return

        market = markets[0]
        console.print(f"\n[bold]Analyzing: {market.question}")
        console.print(f"  Current price: YES ${market.yes_price:.3f} | NO ${market.no_price:.3f}\n")

        llm = LLMClient(
            settings.ai,
            anthropic_key=settings.anthropic_api_key,
            openai_key=settings.openai_api_key,
        )

        console.print("[dim]Fetching news...[/dim]")
        news = await get_news_context(llm, market, settings.news_api_key)
        if news:
            console.print(f"[dim]News context: {news[:200]}...[/dim]\n")

        console.print("[dim]Running AI analysis...[/dim]")
        analysis = await analyze_market(
            llm, market, news_context=news,
            provider=provider or settings.ai.provider,
        )

        edge = analysis.probability - market.yes_price
        edge_style = "green" if abs(edge) >= 0.05 else "yellow"

        console.print(f"\n[bold]AI Analysis ({analysis.provider}/{analysis.model})")
        console.print(f"  Probability: [bold]{analysis.probability*100:.1f}%[/bold]")
        console.print(f"  Confidence:  {analysis.confidence*100:.0f}%")
        console.print(f"  Market:      {market.yes_price*100:.1f}%")
        console.print(f"  Edge:        [{edge_style}]{edge*100:+.1f}%[/{edge_style}]")
        console.print(f"\n  Reasoning: {analysis.reasoning}")
        if analysis.risk_factors:
            console.print(f"  Risks: {', '.join(analysis.risk_factors)}")
        console.print(f"\n[dim]Cost: ${analysis.cost_usd:.4f}[/dim]")

    run_async(_analyze())


# --- Trading ---


@cli.command()
@click.argument("market_query")
@click.argument("side", type=click.Choice(["YES", "NO"], case_sensitive=False))
@click.argument("amount", type=float)
@click.option("--price", "-p", default=None, type=float, help="Limit price (default: market price)")
@click.option("--yolo", is_flag=True, help="Skip confirmation and risk checks")
def trade(market_query, side, amount, price, yolo):
    """Place a trade on Polymarket."""

    async def _trade():
        from polyedge.data.markets import search_markets
        from polyedge.core.client import PolyClient
        from polyedge.core.db import Database
        from polyedge.core.config import apply_db_config
        from polyedge.execution.engine import ExecutionEngine

        settings = get_settings()

        if not settings.poly_private_key:
            console.print("[red]Wallet not configured. Run 'polyedge setup' first.")
            return

        markets = await search_markets(settings, market_query)
        if not markets:
            console.print("[yellow]No matching markets found")
            return

        market = markets[0]
        console.print(f"[bold]{market.question}")

        # Determine token and price
        side_upper = side.upper()
        token_id = market.yes_token_id if side_upper == "YES" else market.no_token_id
        if not token_id:
            console.print(f"[red]No {side_upper} token available for this market")
            return

        trade_price = price or (market.yes_price if side_upper == "YES" else market.no_price)
        size = amount / trade_price if trade_price > 0 else 0

        # Connect to DB and execute
        db = Database(settings.database_url)
        await db.connect()
        await db.init_schema()

        settings = await apply_db_config(settings, db)

        client = PolyClient(settings)
        engine = ExecutionEngine(client, db, settings)

        await engine.place_order(
            market=market,
            token_id=token_id,
            side=side_upper,
            price=trade_price,
            size=size,
            amount_usd=amount,
            strategy="manual",
            force=yolo,
        )

        await db.close()

    run_async(_trade())


# --- Positions & P&L ---


@cli.command()
def positions():
    """Show open positions."""

    async def _positions():
        settings = get_settings()
        from polyedge.core.db import Database
        from polyedge.execution.tracker import PnLTracker

        db = Database(settings.database_url)
        await db.connect()
        await db.init_schema()

        tracker = PnLTracker(db)
        await tracker.display_positions()
        await db.close()

    run_async(_positions())


@cli.group(invoke_without_command=True)
@click.pass_context
def pnl(ctx):
    """P&L commands. Run without subcommand for summary."""
    if ctx.invoked_subcommand is None:
        # Default: show both internal tracker and reconciled summary
        async def _pnl():
            settings = get_settings()
            from polyedge.core.db import Database
            from polyedge.execution.tracker import PnLTracker
            from polyedge.execution.reconciler import PnLReconciler
            from polyedge.core.client import PolyClient

            db = Database(settings.database_url)
            await db.connect()
            await db.init_schema()

            # Show internal tracker (today's trades)
            tracker = PnLTracker(db)
            await tracker.display_pnl()
            await tracker.display_trades()

            # Show reconciled summary if available
            client = PolyClient(settings)
            reconciler = PnLReconciler(client, db, settings)
            await reconciler.display_summary()

            await db.close()

        run_async(_pnl())


@pnl.command("reconcile")
def pnl_reconcile():
    """Pull fills from CLOB API and reconcile P&L with fees."""

    async def _reconcile():
        settings = get_settings()
        from polyedge.core.db import Database
        from polyedge.core.client import PolyClient
        from polyedge.execution.reconciler import PnLReconciler

        db = Database(settings.database_url)
        await db.connect()
        await db.init_schema()

        client = PolyClient(settings)
        reconciler = PnLReconciler(client, db, settings)

        # Reconcile trade fills
        stats = await reconciler.reconcile()

        # Check for resolved markets
        console.print("\n[dim]Checking for resolved markets...[/dim]")
        resolved = await reconciler.check_resolutions()
        if resolved:
            console.print(f"[green]{len(resolved)} resolved positions processed[/green]")
        else:
            console.print("[dim]No resolved positions[/dim]")

        await db.close()

    run_async(_reconcile())


@pnl.command("history")
@click.option("--limit", "-n", default=20, help="Number of entries to show")
@click.option("--strategy", "-s", default=None, help="Filter by strategy")
def pnl_history(limit, strategy):
    """Show reconciled trade history with fees."""

    async def _history():
        settings = get_settings()
        from polyedge.core.db import Database
        from polyedge.core.client import PolyClient
        from polyedge.execution.reconciler import PnLReconciler

        db = Database(settings.database_url)
        await db.connect()
        await db.init_schema()

        client = PolyClient(settings)
        reconciler = PnLReconciler(client, db, settings)
        await reconciler.display_history(limit=limit, strategy=strategy)
        await reconciler.display_summary(strategy=strategy)

        await db.close()

    run_async(_history())


@pnl.command("strategy")
@click.argument("name", required=False, default=None)
def pnl_strategy(name):
    """Show P&L breakdown by strategy (or for a specific strategy)."""

    async def _strategy():
        settings = get_settings()
        from polyedge.core.db import Database
        from polyedge.core.client import PolyClient
        from polyedge.execution.reconciler import PnLReconciler

        db = Database(settings.database_url)
        await db.connect()
        await db.init_schema()

        client = PolyClient(settings)
        reconciler = PnLReconciler(client, db, settings)

        if name:
            await reconciler.display_summary(strategy=name)
        else:
            # Show all strategies
            for strat in ["micro_sniper", "crypto_sniper", "weather_sniper", "agent"]:
                await reconciler.display_summary(strategy=strat)

        await db.close()

    run_async(_strategy())


@pnl.command("debug-fills")
@click.option("--limit", "-n", default=5, help="Number of fills to dump")
def pnl_debug_fills(limit):
    """Dump raw CLOB fill data to debug fee_rate_bps format."""

    async def _debug():
        settings = get_settings()
        from polyedge.core.client import PolyClient

        client = PolyClient(settings)
        fills = client.get_trades()
        console.print(f"[cyan]Total fills: {len(fills)}[/cyan]\n")

        for i, f in enumerate(fills[:limit]):
            console.print(f"[bold]Fill {i+1}:[/bold]")
            for key in ["id", "side", "size", "price", "fee_rate_bps", "match_time", "type", "market", "asset_id", "taker_order_id", "status"]:
                val = f.get(key, "MISSING")
                console.print(f"  {key}: {val}")

            # Compute what the reconciler would compute
            price = float(f.get("price", 0))
            size = float(f.get("size", 0))
            fee_bps = float(f.get("fee_rate_bps", 0))
            computed_fee = price * size * (fee_bps / 10000)
            console.print(f"  [yellow]→ computed fee: ${computed_fee:.4f} (price={price} * size={size} * {fee_bps}/10000)[/yellow]")
            console.print()

        # Summary stats on fee_rate_bps values
        bps_vals = [float(f.get("fee_rate_bps", 0)) for f in fills]
        unique_bps = sorted(set(bps_vals))
        console.print(f"[bold]fee_rate_bps distribution:[/bold]")
        for v in unique_bps:
            count = bps_vals.count(v)
            console.print(f"  {v}: {count} fills")

    run_async(_debug())


@pnl.command("cleanup")
@click.option("--fix", is_flag=True, help="Actually remove orphaned records (default: dry run)")
def pnl_cleanup(fix):
    """Find and clean up orphaned positions/trades.

    Checks every DB position against CLOB token balance. If the CLOB says
    balance is 0 but DB says we have a position, it's orphaned (dust from
    wrong balance tracking, manual sells, or resolved markets).

    Also finds trades stuck in OPEN status that should be closed.
    """

    async def _cleanup():
        settings = get_settings()
        from polyedge.core.db import Database
        from polyedge.core.client import PolyClient

        db = Database(settings.database_url)
        await db.connect()
        await db.init_schema()

        client = PolyClient(settings)

        console.print("\n[bold]Orphaned Position/Trade Cleanup[/bold]")
        if not fix:
            console.print("[yellow]DRY RUN — use --fix to actually remove orphans[/yellow]\n")

        # --- 1. Check DB positions against CLOB balances ---
        console.print("[dim]Checking open positions against CLOB...[/dim]")
        positions = await db.get_open_positions()
        console.print(f"  Found {len(positions)} open positions in DB\n")

        orphaned_positions = []
        real_positions = []

        for pos in positions:
            token_id = pos.get("token_id", "")
            market_id = pos.get("market_id", "")
            side = pos.get("side", "")
            db_size = pos.get("size", 0)
            entry_price = pos.get("entry_price", 0)
            question = pos.get("question", "")[:60]

            clob_balance = 0
            raw_val = "?"
            try:
                bal = client.get_token_balance(token_id)
                raw_bal = bal.get("balance", 0) if isinstance(bal, dict) else bal
                raw_val = str(raw_bal)
                raw_float = float(raw_val)
                # Try multiple interpretations
                for divisor in [1e6, 1e4, 1e3, 1]:
                    val = raw_float / divisor
                    if 0 < val < db_size * 3 and val > 0.01:
                        clob_balance = round(val, 2)
                        break
            except Exception as e:
                raw_val = f"ERROR: {e}"

            is_orphan = clob_balance < 0.01

            if is_orphan:
                orphaned_positions.append(pos)
                console.print(
                    f"  [red]ORPHAN[/red] {side.upper()} {db_size:.1f} @ ${entry_price:.3f} "
                    f"| CLOB: {clob_balance} (raw={raw_val}) | {question}"
                )
            else:
                real_positions.append(pos)
                console.print(
                    f"  [green]OK[/green]     {side.upper()} {db_size:.1f} @ ${entry_price:.3f} "
                    f"| CLOB: {clob_balance} (raw={raw_val}) | {question}"
                )

        # --- 2. Check for stuck OPEN trades ---
        console.print(f"\n[dim]Checking for stuck OPEN trades...[/dim]")
        async with db.pool.acquire() as conn:
            stuck_trades = await conn.fetch(
                """
                SELECT trade_id, market_id, token_id, side, entry_price, size, question,
                       opened_at, status
                FROM polyedge.trades
                WHERE status = 'OPEN'
                ORDER BY opened_at
                """
            )
        stuck_trades = [dict(r) for r in stuck_trades]
        console.print(f"  Found {len(stuck_trades)} OPEN trades in DB\n")

        # An OPEN trade is orphaned if there's no matching open position
        open_market_ids = {p.get("market_id") for p in positions}
        orphaned_trades = []
        for t in stuck_trades:
            mid = t.get("market_id", "")
            if mid not in open_market_ids:
                orphaned_trades.append(t)
                question = t.get("question", "")[:50]
                console.print(
                    f"  [red]ORPHAN[/red] {t['side']} {t['size']:.1f} @ ${t['entry_price']:.3f} "
                    f"| opened {t.get('opened_at', '')} | {question}"
                )

        # --- 3. Summary ---
        console.print(f"\n[bold]Summary:[/bold]")
        console.print(f"  Positions: {len(real_positions)} real, {len(orphaned_positions)} orphaned")
        console.print(f"  Trades:    {len(stuck_trades) - len(orphaned_trades)} with positions, {len(orphaned_trades)} orphaned")

        # --- 4. Fix if requested ---
        if fix and (orphaned_positions or orphaned_trades):
            console.print(f"\n[bold yellow]Cleaning up...[/bold yellow]")

            for pos in orphaned_positions:
                try:
                    await db.remove_position(
                        pos["market_id"], pos["token_id"], pos["side"]
                    )
                    console.print(f"  [green]Removed position[/green]: {pos['side']} on {pos.get('question', '')[:40]}")
                except Exception as e:
                    console.print(f"  [red]Failed to remove position: {e}[/red]")

            for t in orphaned_trades:
                try:
                    # Close the trade with $0 exit (lost/expired)
                    await db.close_trade(
                        t["trade_id"],
                        exit_price=0.0,
                        pnl=-(t.get("entry_price", 0) * t.get("size", 0)),
                        status="ORPHANED",
                    )
                    console.print(f"  [green]Closed trade[/green]: {t['trade_id'][:12]}... as ORPHANED")
                except Exception as e:
                    console.print(f"  [red]Failed to close trade: {e}[/red]")

            console.print(f"\n[green]Cleanup complete![/green]")
        elif not fix and (orphaned_positions or orphaned_trades):
            console.print(f"\n[yellow]Run 'polyedge pnl cleanup --fix' to remove these orphans[/yellow]")
        else:
            console.print(f"\n[green]No orphans found — everything looks clean![/green]")

        # --- 5. Show current USDC balance for reference ---
        try:
            usdc_bal = client.get_collateral_balance()
            raw_usdc = usdc_bal.get("balance", 0) if isinstance(usdc_bal, dict) else usdc_bal
            usdc_float = float(str(raw_usdc))
            # USDC uses 6 decimals
            usdc_amount = usdc_float / 1e6 if usdc_float > 1000 else usdc_float
            console.print(f"\n  USDC balance: ${usdc_amount:.2f}")
        except Exception:
            pass

        await db.close()

    run_async(_cleanup())


@cli.command()
def status():
    """Smoke test CLOB connectivity, wallet balance, and open orders."""

    async def _status():
        settings = get_settings()
        from polyedge.core.client import PolyClient
        from polyedge.core.db import Database

        console.print("\n[bold]PolyEdge Status Check[/bold]\n")

        # 1. Database connection
        console.print("[dim]Checking database...[/dim]")
        try:
            db = Database(settings.database_url)
            await db.connect()
            await db.init_schema()
            trades = await db.get_trades_today()
            positions = await db.get_open_positions()
            console.print(
                f"  [green]✓ DB connected[/green] — "
                f"{len(trades)} trades today, {len(positions)} open positions"
            )
        except Exception as e:
            console.print(f"  [red]✗ DB failed: {e}[/red]")
            return

        # 2. CLOB client initialization
        console.print("[dim]Checking CLOB API credentials...[/dim]")
        try:
            client = PolyClient(settings)
            client.ensure_ready()
            console.print(f"  [green]✓ CLOB client initialized[/green]")
        except Exception as e:
            console.print(f"  [red]✗ CLOB init failed: {e}[/red]")
            return

        # 3. Wallet address
        wallet = settings.poly_wallet_address
        proxy = settings.poly_proxy_address
        if wallet:
            console.print(f"  [green]✓ Wallet (EOA):[/green] {wallet[:6]}...{wallet[-4:]}")
        else:
            console.print("  [yellow]⚠ No wallet address configured[/yellow]")
        if proxy:
            console.print(f"  [green]✓ Proxy (funder):[/green] {proxy[:6]}...{proxy[-4:]}")
        else:
            console.print("  [dim]  No proxy address (using EOA-only mode)[/dim]")

        # 4. USDC balance
        console.print("[dim]Checking USDC balance...[/dim]")
        try:
            bal = client.get_collateral_balance()
            balance = float(bal.get("balance", 0)) / 1e6  # USDC has 6 decimals
            allowance = float(bal.get("allowance", 0)) / 1e6
            bal_style = "green" if balance > 10 else "yellow" if balance > 0 else "red"
            console.print(
                f"  [{bal_style}]✓ USDC Balance: ${balance:,.2f}[/{bal_style}] "
                f"(allowance: ${allowance:,.2f})"
            )
        except Exception as e:
            console.print(f"  [red]✗ Balance check failed: {e}[/red]")

        # 5. Open orders
        console.print("[dim]Checking open orders...[/dim]")
        try:
            open_orders = client.get_open_orders()
            if open_orders:
                console.print(f"  [yellow]⚠ {len(open_orders)} open orders on the book[/yellow]")
                for o in open_orders[:5]:
                    oid = o.get("id", "")[:8]
                    side = o.get("side", "?")
                    price = float(o.get("price", 0))
                    size = float(o.get("original_size", 0))
                    matched = float(o.get("size_matched", 0))
                    console.print(
                        f"    {oid}... {side} {size:.1f} @ ${price:.2f} "
                        f"(filled: {matched:.1f})"
                    )
            else:
                console.print(f"  [green]✓ No open orders[/green]")
        except Exception as e:
            console.print(f"  [red]✗ Open orders check failed: {e}[/red]")

        # 6. Trade history (recent)
        console.print("[dim]Checking trade history access...[/dim]")
        try:
            recent_trades = client.get_trades()
            console.print(
                f"  [green]✓ Trade history accessible[/green] — "
                f"{len(recent_trades)} total fills on record"
            )
        except Exception as e:
            console.print(f"  [red]✗ Trade history failed: {e}[/red]")

        # 7. Order placement test (dry — just verify we CAN create signed orders)
        console.print("[dim]Checking order signing...[/dim]")
        try:
            from py_clob_client.clob_types import OrderArgs
            from py_clob_client.order_builder.constants import BUY
            test_args = OrderArgs(
                token_id="0" * 40,  # Dummy token
                price=0.50,
                size=1.0,
                side=BUY,
            )
            signed = client.client.create_order(test_args)
            if signed:
                console.print(f"  [green]✓ Order signing works[/green]")
            else:
                console.print(f"  [yellow]⚠ Order signing returned empty[/yellow]")
        except Exception as e:
            # This might fail for dummy token — that's fine, we just want to
            # verify the signing pipeline doesn't error before reaching the API
            err_str = str(e).lower()
            if "sign" in err_str or "key" in err_str:
                console.print(f"  [red]✗ Order signing failed: {e}[/red]")
            else:
                console.print(f"  [green]✓ Order signing pipeline OK[/green] (dummy token rejected as expected)")

        console.print("\n[bold]Status check complete.[/bold]\n")
        await db.close()

    run_async(_status())


# --- Agent ---


@cli.command()
@click.option(
    "--mode",
    "-m",
    type=click.Choice(["autopilot", "copilot", "signals"], case_sensitive=False),
    default=None,
    help="Agent operating mode",
)
def autopilot(mode):
    """Start the autonomous trading agent."""

    async def _autopilot():
        from polyedge.core.client import PolyClient
        from polyedge.core.db import Database
        from polyedge.core.config import apply_db_config
        from polyedge.ai.llm import LLMClient
        from polyedge.ai.agent import TradingAgent
        from polyedge.health import start_health_server

        settings = get_settings()

        if not settings.poly_private_key:
            console.print("[red]Wallet not configured. Run 'polyedge setup' first.")
            return

        if not settings.anthropic_api_key and not settings.openai_api_key:
            console.print("[red]No AI API keys configured")
            return

        db = Database(settings.database_url)
        await db.connect()
        await db.init_schema()

        # Load config from DB (overrides YAML defaults)
        settings = await apply_db_config(settings, db)

        if mode:
            settings.agent.mode = mode

        client = PolyClient(settings)
        llm = LLMClient(
            settings.ai,
            anthropic_key=settings.anthropic_api_key,
            openai_key=settings.openai_api_key,
            db=db,
        )

        agent = TradingAgent(settings, client, db, llm)

        asyncio.create_task(start_health_server())

        try:
            await agent.run()
        finally:
            await db.close()

    run_async(_autopilot())


# --- Crypto Sniper ---


@cli.command()
@click.option("--auto", is_flag=True, help="Auto-execute trades (no confirmation)")
@click.option("--dry", is_flag=True, help="Dry run — show opportunities but don't trade")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed evaluation for every market")
@click.option("--quiet", "-q", is_flag=True, help="Suppress EVAL skip lines — only show opportunities and status")
def sniper(auto, dry, verbose, quiet):
    """Start the crypto sniper — real-time trading on short-duration crypto markets.

    Connects to Binance for live prices, watches Polymarket's 5-minute and
    15-minute crypto "Up or Down" markets, and trades when the price feed
    shows near-certain outcomes before Polymarket adjusts.

    No AI needed — pure math and speed.
    """

    async def _sniper():
        from polyedge.core.client import PolyClient
        from polyedge.core.db import Database
        from polyedge.core.config import apply_db_config
        from polyedge.strategies.sniper_runner import SniperRunner
        from polyedge.health import start_health_server

        settings = get_settings()

        if not settings.poly_private_key:
            console.print("[red]Wallet not configured. Run 'polyedge setup' first.")
            return

        db = Database(settings.database_url)
        await db.connect()
        await db.init_schema()

        # Load config from DB (overrides YAML defaults)
        settings = await apply_db_config(settings, db)

        client = PolyClient(settings)

        runner = SniperRunner(
            settings=settings,
            client=client,
            db=db,
            auto_execute=auto,
            dry_run=dry,
            verbose=verbose,
            quiet=quiet,
        )

        asyncio.create_task(start_health_server())

        try:
            await runner.run()
        finally:
            await db.close()

    run_async(_sniper())


# --- Micro Sniper ---


@cli.command()
@click.option("--auto", is_flag=True, help="Auto-execute trades (no confirmation)")
@click.option("--dry", is_flag=True, help="Dry run — show momentum signals but don't trade")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed evaluations")
@click.option("--quiet", "-q", is_flag=True, help="Suppress eval output, show only trades + status")
@click.option("--market", "-m", default=None, help="Filter to specific market (e.g. 'btc', '5 minute', 'bitcoin 5')")
@click.option("--no-warmup", is_flag=True, help="Skip warmup — trade the current window immediately")
def micro(auto, dry, verbose, quiet, market, no_warmup):
    """Start the micro sniper — high-frequency momentum trading on 5-min crypto markets.

    \b
    Connects to Binance aggTrade for tick-level order flow, reads momentum
    from buy/sell imbalance, VWAP drift, and trade intensity, then trades
    Polymarket's 5-minute up/down crypto markets.

    Can make up to 50 trades per 5-minute window. Small position sizes
    (1-3% of bankroll) with high frequency.

    \b
    Examples:
      polyedge micro --dry --market "btc"           # Watch BTC 5-min only
      polyedge micro --dry --market "5 minute"       # All 5-minute windows
      polyedge micro --auto --market "bitcoin 5"     # Auto-trade BTC 5-min
      polyedge micro --dry                           # All up/down markets

    No AI needed — pure microstructure analysis and speed.
    """

    async def _micro():
        from polyedge.core.client import PolyClient
        from polyedge.core.db import Database
        from polyedge.core.config import apply_db_config
        from polyedge.strategies.micro_runner import MicroRunner
        from polyedge.health import start_health_server

        settings = get_settings()

        if not settings.poly_private_key:
            console.print("[red]Wallet not configured. Run 'polyedge setup' first.")
            return

        db = Database(settings.database_url)
        await db.connect()
        await db.init_schema()

        # Load config from DB (overrides YAML defaults)
        settings = await apply_db_config(settings, db)

        client = PolyClient(settings)

        runner = MicroRunner(
            settings=settings,
            client=client,
            db=db,
            auto_execute=auto,
            dry_run=dry,
            verbose=verbose,
            quiet=quiet,
            market_filter=market,
            skip_warmup=no_warmup,
        )

        asyncio.create_task(start_health_server())

        try:
            await runner.run()
        finally:
            await db.close()

    run_async(_micro())


# --- Price Logger (standalone persistent context builder) ---


@cli.command("price-logger")
@click.option("--symbols", "-s", default="btcusdt", help="Comma-separated symbols to track")
@click.option("--interval", default=30.0, help="Seconds between DB snapshots")
def price_logger(symbols, interval):
    """Standalone price logger — keeps micro_price_log DB table fresh.

    \b
    Run this in a separate terminal tab. It connects to Binance aggTrade,
    logs price/OFI/volume snapshots to the DB every 30 seconds, and prunes
    old entries. The micro sniper reads these on startup for instant
    cross-restart trend context.

    \b
    The micro sniper also logs prices itself, but this standalone command
    ensures the DB stays fresh even when you stop/restart/switch micro runs.
    If both are running, the DB just gets more frequent snapshots (harmless).

    \b
    Examples:
        polyedge price-logger                    # BTC only, 30s intervals
        polyedge price-logger -s btcusdt,ethusdt # BTC + ETH
        polyedge price-logger --interval 15      # Log every 15s
    """
    from polyedge.core.config import load_config
    from polyedge.core.db import Database
    from polyedge.data.binance_aggtrade import BinanceAggTradeFeed

    async def _price_logger():
        settings = load_config()
        db = Database(settings.database_url)
        await db.connect()
        await db.init_schema()

        # Apply DB config overrides
        from polyedge.core.config import apply_db_config
        await apply_db_config(settings, db)

        sym_list = [s.strip().lower() for s in symbols.split(",")]
        feed = BinanceAggTradeFeed(symbols=sym_list)

        console.print(f"[bold green]Price Logger started[/bold green]")
        console.print(f"[dim]Symbols: {', '.join(s.upper() for s in sym_list)} | Interval: {interval}s[/dim]")
        console.print(f"[dim]Writing to micro_price_log table...[/dim]")

        async def _log_loop():
            prune_counter = 0
            while True:
                await asyncio.sleep(interval)
                for sym in sym_list:
                    micro = feed.get_micro(sym)
                    if not micro or micro.current_price <= 0:
                        continue
                    try:
                        await db.log_micro_price(
                            symbol=sym,
                            price=micro.current_price,
                            ofi_30s=micro.flow_30s.ofi if micro.flow_30s.is_active else 0.0,
                            volume_30s=micro.flow_30s.total_volume,
                            trade_intensity=micro.flow_5s.trade_intensity,
                        )
                        trend = micro.trend_5m
                        console.print(
                            f"[dim]{sym.upper()}: ${micro.current_price:,.2f} | "
                            f"OFI(30s): {micro.flow_30s.ofi:+.2f} | "
                            f"T5m: {trend:+.3%} | "
                            f"logged[/dim]"
                        )
                    except Exception as e:
                        console.print(f"[yellow]Log failed for {sym}: {e}[/yellow]")

                prune_counter += 1
                if prune_counter >= 10:
                    prune_counter = 0
                    try:
                        await db.prune_micro_price_log(keep_minutes=60)
                    except Exception:
                        pass

        import asyncio as _asyncio
        tasks = [
            _asyncio.create_task(feed.start()),
            _asyncio.create_task(_log_loop()),
        ]

        try:
            # Wait for connection
            for _ in range(50):
                if feed.is_connected:
                    break
                await _asyncio.sleep(0.1)
            if feed.is_connected:
                console.print("[green]Binance connected[/green]")

            await _asyncio.gather(*tasks, return_exceptions=True)
        except KeyboardInterrupt:
            console.print("\n[yellow]Price logger stopped[/yellow]")
        finally:
            await feed.stop()
            for t in tasks:
                t.cancel()
            await db.close()

    run_async(_price_logger())


# --- Weather Sniper ---


@cli.command()
@click.option("--auto", is_flag=True, help="Auto-execute trades (no confirmation)")
@click.option("--dry", is_flag=True, help="Dry run — show opportunities but don't trade")
def weather(auto, dry):
    """Start the weather sniper — trade weather markets using forecast data.

    Connects to Open-Meteo for ensemble forecasts, watches Polymarket's
    temperature and precipitation markets, and trades when professional
    forecasts disagree with market prices.

    Also detects neg-risk arbitrage on multi-bucket events.

    No AI needed — pure data comparison.
    """

    async def _weather():
        from polyedge.core.client import PolyClient
        from polyedge.core.db import Database
        from polyedge.core.config import apply_db_config
        from polyedge.strategies.weather_runner import WeatherRunner
        from polyedge.health import start_health_server

        settings = get_settings()

        if not settings.poly_private_key:
            console.print("[red]Wallet not configured. Run 'polyedge setup' first.")
            return

        db = Database(settings.database_url)
        await db.connect()
        await db.init_schema()

        # Load config from DB (overrides YAML defaults)
        settings = await apply_db_config(settings, db)

        client = PolyClient(settings)

        runner = WeatherRunner(
            settings=settings,
            client=client,
            db=db,
            auto_execute=auto,
            dry_run=dry,
        )

        asyncio.create_task(start_health_server())

        try:
            await runner.run()
        finally:
            await db.close()

    run_async(_weather())


# --- Dashboard ---


@cli.command()
def dashboard():
    """Show live monitoring dashboard."""

    async def _dashboard():
        from polyedge.core.db import Database
        from polyedge.core.config import apply_db_config
        from polyedge.dashboard.live import Dashboard

        settings = get_settings()

        db = Database(settings.database_url)
        await db.connect()
        await db.init_schema()

        settings = await apply_db_config(settings, db)

        dash = Dashboard(db, settings)
        try:
            await dash.run()
        finally:
            await db.close()

    run_async(_dashboard())


# --- Database ---


@cli.command()
def initdb():
    """Initialize the database schema."""

    async def _initdb():
        from polyedge.core.db import Database

        settings = get_settings()
        db = Database(settings.database_url)
        await db.connect()
        await db.init_schema()
        console.print("[green]Database schema initialized!")
        await db.close()

    run_async(_initdb())


# --- Market Indexer ---


@cli.command()
@click.option("--force", "-f", is_flag=True, help="Force sync even if recently synced")
def sync(force):
    """Sync all markets from Polymarket API to local database."""

    async def _sync():
        from polyedge.core.db import Database
        from polyedge.data.indexer import MarketIndexer

        settings = get_settings()
        db = Database(settings.database_url)
        await db.connect()
        await db.init_schema()

        indexer = MarketIndexer(settings, db)
        count = await indexer.sync(force=True if force else False)

        if count:
            total = await db.get_market_count()
            console.print(f"[green]Synced {count} markets. Total active in DB: {total}")
        else:
            console.print("[yellow]No markets synced (may be up to date)")

        await db.close()

    run_async(_sync())


@cli.command()
def costs():
    """Show AI cost breakdown for today."""

    async def _costs():
        from polyedge.core.db import Database

        settings = get_settings()
        db = Database(settings.database_url)
        await db.connect()
        await db.init_schema()

        details = await db.get_ai_cost_today_detailed()

        budget = settings.ai.max_analysis_cost_per_day
        spent = details["total_cost"]
        remaining = max(0, budget - spent)

        console.print(f"\n[bold]AI Cost Summary (Today)")
        console.print(f"  Budget:    ${budget:.2f}")
        console.print(f"  Spent:     ${spent:.4f}")
        console.print(f"  Remaining: ${remaining:.4f}")

        if details["breakdown"]:
            table = Table(title="Breakdown by Model")
            table.add_column("Provider", width=10)
            table.add_column("Model", width=30)
            table.add_column("Calls", justify="right", width=6)
            table.add_column("Input Tokens", justify="right", width=12)
            table.add_column("Output Tokens", justify="right", width=12)
            table.add_column("Cost", justify="right", width=10)

            for row in details["breakdown"]:
                table.add_row(
                    row["provider"],
                    row["model"],
                    str(row["calls"]),
                    f"{row['total_input']:,}",
                    f"{row['total_output']:,}",
                    f"${row['total_cost']:.4f}",
                )
            console.print(table)
        else:
            console.print("[dim]  No AI calls logged today[/dim]")

        await db.close()

    run_async(_costs())


@cli.command()
@click.argument("market_query")
def book(market_query):
    """Show order book intelligence for a market (imbalance, depth, whales)."""

    async def _book():
        from polyedge.data.markets import search_markets
        from polyedge.core.client import PolyClient
        from polyedge.data.book_analyzer import get_full_book_intelligence

        settings = get_settings()

        if not settings.poly_private_key:
            console.print("[red]Wallet not configured. Run 'polyedge setup' first.")
            return

        markets = await search_markets(settings, market_query)
        if not markets:
            console.print("[yellow]No matching markets found")
            return

        market = markets[0]
        console.print(f"\n[bold]{market.question}")
        console.print(f"  YES: ${market.yes_price:.3f} | NO: ${market.no_price:.3f}\n")

        client = PolyClient(settings)
        intel = get_full_book_intelligence(client, market)

        for side, book_intel in intel.items():
            console.print(f"[bold cyan]--- {side} Order Book ---")
            console.print(book_intel.summary())
            console.print()

        if not intel:
            console.print("[yellow]No order book data available (no token IDs)")

    run_async(_book())


@cli.command()
@click.option("--market-query", "-m", default=None, help="Subscribe to a specific market")
@click.option("--duration", "-d", default=60, help="How many seconds to listen")
def feed(market_query, duration):
    """Stream real-time WebSocket market data."""

    async def _feed():
        from polyedge.data.markets import search_markets, fetch_active_markets
        from polyedge.data.ws_feed import MarketFeed

        settings = get_settings()

        # Get token IDs to subscribe to
        if market_query:
            markets = await search_markets(settings, market_query)
            if not markets:
                console.print("[yellow]No matching markets found")
                return
            markets = markets[:1]
        else:
            markets, _ = await fetch_active_markets(settings, limit=5)

        asset_ids = []
        for m in markets:
            if m.yes_token_id:
                asset_ids.append(m.yes_token_id)
            if m.no_token_id:
                asset_ids.append(m.no_token_id)

        if not asset_ids:
            console.print("[red]No token IDs available for subscription")
            return

        console.print(f"[dim]Subscribing to {len(asset_ids)} tokens from {len(markets)} markets...[/dim]")

        ws_feed = MarketFeed(settings)

        # Register event handlers
        async def on_trade(event):
            price = event.get("price", "?")
            side = event.get("side", "?")
            size = event.get("size", "?")
            console.print(f"  [green]TRADE[/green] {side} {size} @ ${price}")

        async def on_bid_ask(event):
            bid = event.get("best_bid", "?")
            ask = event.get("best_ask", "?")
            spread = event.get("spread", "?")
            console.print(f"  [cyan]BBA[/cyan] bid=${bid} ask=${ask} spread={spread}")

        async def on_book(event):
            bids = len(event.get("bids", []))
            asks = len(event.get("asks", []))
            console.print(f"  [blue]BOOK[/blue] {bids} bids, {asks} asks")

        ws_feed.on("last_trade_price", on_trade)
        ws_feed.on("best_bid_ask", on_bid_ask)
        ws_feed.on("book", on_book)

        console.print(f"[bold]Streaming for {duration}s (Ctrl+C to stop)...\n")

        # Run with timeout
        feed_task = asyncio.create_task(ws_feed.start(asset_ids))
        try:
            await asyncio.wait_for(asyncio.shield(feed_task), timeout=duration)
        except asyncio.TimeoutError:
            pass
        except KeyboardInterrupt:
            pass
        finally:
            await ws_feed.stop()
            console.print("\n[dim]Feed stopped[/dim]")

    run_async(_feed())


@cli.command()
@click.option("--hours", "-h", default=1, help="Hours to look back")
@click.option("--min-move", default=0.03, help="Minimum price move to show")
def movers(hours, min_move):
    """Show markets with significant price movement."""

    async def _movers():
        from polyedge.core.db import Database
        from polyedge.data.indexer import MarketIndexer

        settings = get_settings()
        db = Database(settings.database_url)
        await db.connect()
        await db.init_schema()

        indexer = MarketIndexer(settings, db)

        # Need at least 2 syncs for price history
        count = await db.get_market_count()
        if count == 0:
            console.print("[yellow]No markets in DB. Run 'polyedge sync' first.")
            await db.close()
            return

        price_movers = await indexer.get_price_movers(
            hours=hours, min_move_pct=min_move,
            min_liquidity=settings.risk.min_liquidity,
        )

        if not price_movers:
            console.print(f"[dim]No markets moved >{min_move*100:.0f}% in the last {hours}h[/dim]")
            console.print("[dim]Tip: Run 'polyedge sync' periodically to build price history[/dim]")
            await db.close()
            return

        table = Table(title=f"Price Movers (last {hours}h, >{min_move*100:.0f}% move)")
        table.add_column("#", width=3)
        table.add_column("Market", max_width=50)
        table.add_column("Dir", width=5)
        table.add_column("Old", justify="right", width=7)
        table.add_column("New", justify="right", width=7)
        table.add_column("Change", justify="right", width=8)

        for i, mover in enumerate(price_movers[:20], 1):
            direction_style = "green" if mover["direction"] == "up" else "red"
            table.add_row(
                str(i),
                mover["market"].question[:50],
                f"[{direction_style}]{mover['direction'].upper()}[/{direction_style}]",
                f"${mover['old_price']:.2f}",
                f"${mover['new_price']:.2f}",
                f"{mover['price_change']*100:+.1f}%",
            )

        console.print(table)
        await db.close()

    run_async(_movers())


# --- Config Management ---


@cli.command("config")
@click.argument("action", type=click.Choice(["show", "set", "save"], case_sensitive=False))
@click.argument("key", required=False)
@click.argument("value", required=False)
def config_cmd(action, key, value):
    """View or change trading config stored in the database.

    \b
    Usage:
      polyedge config show                          # show all config from DB
      polyedge config set risk.kelly_fraction 0.5   # change a value
      polyedge config save                          # push current settings to DB
    """

    async def _config():
        from polyedge.core.db import Database
        from polyedge.core.config import apply_db_config, save_config_to_db, settings_to_db_dict

        settings = get_settings()
        db = Database(settings.database_url)
        await db.connect()
        await db.init_schema()

        if action == "show":
            db_config = await db.get_all_config()
            if not db_config:
                console.print("[yellow]No config in database yet. Run 'polyedge init' or 'polyedge config save'.")
                await db.close()
                return

            # Group by section
            sections: dict[str, list] = {}
            for k in sorted(db_config.keys()):
                section = k.split(".")[0]
                sections.setdefault(section, []).append(k)

            for section, keys in sections.items():
                console.print(f"\n[bold cyan]{section}[/bold cyan]")
                for k in keys:
                    v = db_config[k]
                    console.print(f"  {k} = [green]{v}[/green]")

            console.print(f"\n[dim]{len(db_config)} config values in database[/dim]")

        elif action == "set":
            if not key or value is None:
                console.print("[red]Usage: polyedge config set <key> <value>")
                console.print("[dim]Example: polyedge config set risk.kelly_fraction 0.5")
                await db.close()
                return

            # Auto-convert value types
            parsed_value: any = value
            if value.lower() in ("true", "false"):
                parsed_value = value.lower() == "true"
            elif value.replace(".", "", 1).replace("-", "", 1).isdigit():
                parsed_value = float(value) if "." in value else int(value)

            await db.set_risk_override(key, parsed_value)
            console.print(f"[green]Set {key} = {parsed_value}[/green]")

        elif action == "save":
            # Load current settings (YAML + env), then push all to DB
            settings = await apply_db_config(settings, db)
            await save_config_to_db(settings, db)
            config = settings_to_db_dict(settings)
            console.print(f"[green]Saved {len(config)} config values to database[/green]")

        await db.close()

    run_async(_config())


# --- Keychain Vault ---


@cli.command()
@click.argument("action", type=click.Choice(["store", "list", "remove"], case_sensitive=False))
@click.argument("key", required=False)
@click.argument("value", required=False)
def vault(action, key, value):
    """Manage secrets in macOS Keychain.

    \b
    Usage:
      polyedge vault store poly_private_key       # prompts for value
      polyedge vault store anthropic_api_key sk-ant-...
      polyedge vault list                          # show stored keys
      polyedge vault remove poly_private_key       # delete a key
    """
    from polyedge.core.config import KEYCHAIN_KEYS, _get_from_keychain, _set_in_keychain, KEYCHAIN_SERVICE

    if action == "list":
        console.print("[bold]Keychain secrets (polyedge):\n")
        found = 0
        for k in KEYCHAIN_KEYS:
            val = _get_from_keychain(k)
            if val:
                # Show just first/last few chars
                if len(val) > 12:
                    masked = val[:4] + "..." + val[-4:]
                else:
                    masked = "***"
                console.print(f"  [green]{k}[/green] = {masked}")
                found += 1
            else:
                console.print(f"  [dim]{k}[/dim] = (not set)")
        console.print(f"\n[dim]{found}/{len(KEYCHAIN_KEYS)} keys stored[/dim]")
        return

    if not key:
        console.print("[red]Key name required. Valid keys:")
        for k in KEYCHAIN_KEYS:
            console.print(f"  {k}")
        return

    key = key.lower()
    if key not in KEYCHAIN_KEYS:
        console.print(f"[yellow]Warning: '{key}' is not a recognized key. Storing anyway.")

    if action == "store":
        if not value:
            value = click.prompt(f"Enter value for {key}", hide_input=True)
        if _set_in_keychain(key, value):
            console.print(f"[green]Stored '{key}' in Keychain")
        else:
            console.print(f"[red]Failed to store '{key}' in Keychain")

    elif action == "remove":
        import subprocess
        result = subprocess.run(
            ["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", key],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            console.print(f"[green]Removed '{key}' from Keychain")
        else:
            console.print(f"[yellow]'{key}' not found in Keychain")


if __name__ == "__main__":
    cli()
