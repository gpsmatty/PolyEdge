# PolyEdge

AI-powered trading bot for [Polymarket](https://polymarket.com) prediction markets.

Scans thousands of markets, estimates true probabilities with LLMs, detects mispricings, sizes positions with Kelly criterion, and executes trades on the CLOB — all from a single Python process.

## Quick Start

```bash
# Clone and install
cd PolyEdge
pip install -e ".[dev]"

# First-run wizard (secrets, database, wallet, risk profile — all in one)
polyedge init

# Or set up manually:
polyedge vault store database_url
polyedge vault store anthropic_api_key
polyedge initdb
polyedge setup

# Scan markets
polyedge scan
polyedge hunt          # Find cheap underpriced events
polyedge edges         # AI-powered edge detection
polyedge book "trump"  # Order book intelligence
polyedge movers        # Markets with price movement

# Start the agent
polyedge autopilot --mode signals    # Display-only mode
polyedge autopilot --mode copilot    # Recommends, you approve
polyedge autopilot --mode autopilot  # Fully autonomous

# Stream real-time data
polyedge feed -m "trump" -d 120

# Monitor
polyedge positions
polyedge pnl
polyedge costs
polyedge dashboard
```

## Architecture

Single async Python process. No microservices.

```
polyedge/
├── ai/                 # LLM integration (Claude + OpenAI)
│   ├── agent.py        # Autonomous trading loop
│   ├── analyst.py      # Probability estimation prompts
│   ├── llm.py          # Tiered model client (research + compute)
│   └── news.py         # News context retrieval
├── core/
│   ├── client.py       # Polymarket CLOB SDK wrapper
│   ├── config.py       # YAML + env config (Pydantic)
│   ├── db.py           # PostgreSQL (asyncpg)
│   └── models.py       # All data models
├── data/
│   ├── book_analyzer.py  # Order book microstructure analysis
│   ├── indexer.py        # Market sync (API → DB)
│   ├── markets.py        # Gamma API fetching
│   ├── orderbook.py      # Order book fetching
│   └── ws_feed.py        # WebSocket real-time feed
├── execution/
│   ├── engine.py       # Order placement + risk checks
│   └── tracker.py      # P&L tracking
├── risk/
│   ├── kelly.py        # Kelly criterion
│   ├── portfolio.py    # Portfolio-level risk management
│   └── sizing.py       # Position sizing
├── strategies/
│   ├── cheap_hunter.py # Underpriced tail events
│   ├── edge_finder.py  # AI-detected mispricings
│   └── market_maker.py # Spread capture (WIP)
├── dashboard/
│   └── live.py         # Rich terminal dashboard
└── cli.py              # Click CLI entry point
```

## Strategies

### Cheap Event Hunter
Zero AI cost. Scans all markets for YES/NO outcomes priced under $0.15. Estimates true probability via liquidity, time, and price heuristics. Ranks by expected value per dollar.

### AI Edge Finder
Uses LLMs to estimate true probabilities independent of market price. When `AI_probability - market_price > 5%`, generates a trade signal. Two-tier model system:
- **Compute model** (Haiku) — quick-scores all candidates, ~$0.001/market
- **Research model** (Sonnet) — deep analysis on top candidates with news + order book context, ~$0.003/market

### Market Maker (WIP)
Quote both sides on wide-spread markets. Inventory management and adverse selection avoidance.

## Risk Management

All configurable in `config/default.yaml` and overridable at runtime via database:

| Parameter | Default | Description |
|-----------|---------|-------------|
| max_position_pct | 10% | Max single position as % of bankroll |
| max_exposure_pct | 50% | Max total exposure |
| max_positions | 10 | Max concurrent positions |
| kelly_fraction | 0.25 | Quarter Kelly (conservative) |
| min_edge_threshold | 5% | Minimum edge to trade |
| min_confidence | 60% | Minimum AI confidence |
| max_trades_per_day | 20 | Daily trade limit |
| daily_loss_limit_pct | 15% | Stop-loss for the day |
| drawdown_circuit_breaker | 25% | Pause trading if bankroll drops 25% |
| max_analysis_cost_per_day | $5.00 | AI API budget cap |

## Data Pipeline

1. **Market Indexer** syncs all markets from Gamma API to PostgreSQL every 15 minutes (paginated, handles 1000+ markets)
2. **Price History** records snapshots each sync for movement detection
3. **Market Lifecycle** automatically deactivates markets that vanish from API or pass end date
4. **WebSocket Feed** provides real-time price/book updates via `wss://ws-subscriptions-clob.polymarket.com`
5. **Order Book Analyzer** extracts microstructure intelligence (imbalance, depth, whales, walls)

## Agent Scan Cycle

Each cycle (every 5 minutes):

1. Load markets from DB (auto-sync if stale)
2. Run Cheap Hunter on all markets (free)
3. Pick top 20 AI candidates (price movers + highest volume)
4. Quick-score with Haiku ($0.001/market)
5. Deep-analyze top half with Sonnet + news + book context ($0.003/market)
6. Fetch order book intelligence for trade candidates
7. Generate signals, size positions with Kelly, execute trades
8. Record memories and lessons for future context

## Stack

- Python 3.11+ with asyncio
- py-clob-client (Polymarket SDK)
- asyncpg + PostgreSQL
- Anthropic SDK (Claude) + OpenAI SDK
- websockets (real-time feed)
- Click (CLI) + Rich (terminal UI)
- Pydantic v2 (config + models)

## Testing

```bash
.venv/bin/pytest tests/ -v
```

44 tests covering models, Kelly criterion, position sizing, portfolio risk, order book analysis, and strategy logic.

## Secrets Management (macOS Keychain)

All secrets are stored in macOS Keychain — encrypted at rest, unlocked when you log in. No `.env` file with plaintext keys needed.

```bash
# Store secrets (prompts for hidden input)
polyedge vault store poly_private_key
polyedge vault store poly_wallet_address 0x...
polyedge vault store database_url
polyedge vault store anthropic_api_key
polyedge vault store openai_api_key
polyedge vault store poly_api_key
polyedge vault store poly_api_secret
polyedge vault store poly_api_passphrase
polyedge vault store news_api_key

# Verify what's stored
polyedge vault list

# Remove a key
polyedge vault remove openai_api_key
```

Priority order: Keychain > environment variables > `.env` file > defaults.

## Configuration

All trading config lives in the **database** — portable across environments (Mac, VPS, App Platform, etc.).

```bash
# View all config
polyedge config show

# Change a value (takes effect on next scan cycle)
polyedge config set risk.kelly_fraction 0.5
polyedge config set agent.mode autopilot
polyedge config set ai.max_analysis_cost_per_day 10.0

# Push current settings to DB
polyedge config save
```

**Secrets**: macOS Keychain via `polyedge vault` (see above). Falls back to environment variables or `.env` file.

**Trading config**: Database (`risk_config` table). Set via `polyedge init` wizard or `polyedge config set`.

**Fallback defaults**: `config/default.yaml` — used when DB has no value for a key.

Priority: Database > Keychain (secrets) > env vars > `.env` > YAML > Pydantic defaults.
