"""PolyEdge CLI — command-line interface for the trading bot."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from polyedge.core.config import load_config

console = Console()


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
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config


# --- Setup ---


@cli.command()
def setup():
    """Generate a new wallet and derive API credentials."""
    from polyedge.core.client import PolyClient

    console.print("[bold cyan]PolyEdge Setup[/bold cyan]\n")

    settings = get_settings()

    if settings.poly_private_key:
        console.print("[yellow]Wallet already configured in .env")
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

    console.print("\n[bold yellow]IMPORTANT: Add to your .env file:")
    console.print(f"  POLY_PRIVATE_KEY={wallet['private_key']}")
    console.print(f"  POLY_WALLET_ADDRESS={wallet['address']}")

    console.print(
        "\n[bold]Next steps:"
        "\n  1. Add the private key and address to your .env file"
        "\n  2. Send $200 USDC to the address on Polygon"
        "\n  3. Send ~$0.50 of MATIC/POL for gas"
        "\n  4. Run 'polyedge setup' again to derive API credentials"
    )


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

        markets = await fetch_active_markets(
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


@cli.command()
def pnl():
    """Show P&L summary and recent trades."""

    async def _pnl():
        settings = get_settings()
        from polyedge.core.db import Database
        from polyedge.execution.tracker import PnLTracker

        db = Database(settings.database_url)
        await db.connect()
        await db.init_schema()

        tracker = PnLTracker(db)
        await tracker.display_pnl()
        await tracker.display_trades()
        await db.close()

    run_async(_pnl())


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
        from polyedge.ai.llm import LLMClient
        from polyedge.ai.agent import TradingAgent

        settings = get_settings()

        if mode:
            settings.agent.mode = mode

        if not settings.poly_private_key:
            console.print("[red]Wallet not configured. Run 'polyedge setup' first.")
            return

        if not settings.anthropic_api_key and not settings.openai_api_key:
            console.print("[red]No AI API keys configured")
            return

        db = Database(settings.database_url)
        await db.connect()
        await db.init_schema()

        client = PolyClient(settings)
        llm = LLMClient(
            settings.ai,
            anthropic_key=settings.anthropic_api_key,
            openai_key=settings.openai_api_key,
            db=db,
        )

        agent = TradingAgent(settings, client, db, llm)

        try:
            await agent.run()
        finally:
            await db.close()

    run_async(_autopilot())


# --- Dashboard ---


@cli.command()
def dashboard():
    """Show live monitoring dashboard."""

    async def _dashboard():
        from polyedge.core.db import Database
        from polyedge.dashboard.live import Dashboard

        settings = get_settings()

        db = Database(settings.database_url)
        await db.connect()
        await db.init_schema()

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


if __name__ == "__main__":
    cli()
