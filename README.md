# PolyEdge

AI-powered trading bot for [Polymarket](https://polymarket.com) prediction markets.

Scans thousands of markets, estimates true probabilities with LLMs, detects mispricings, sizes positions with Kelly criterion, and executes trades on the CLOB — all from a single Python process. Includes a real-time crypto sniper that exploits latency between Binance spot prices and Polymarket's short-duration crypto markets, and a high-frequency micro sniper that reads Binance aggTrade order flow to momentum-trade 5-minute crypto windows.

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
polyedge edges         # AI-powered edge detection
polyedge book "trump"  # Order book intelligence
polyedge movers        # Markets with price movement

# Crypto sniper — real-time price feed arbitrage
polyedge sniper --dry   # Watch mode — show opportunities, don't trade
polyedge sniper         # Copilot — confirm each snipe
polyedge sniper --auto  # Autopilot — auto-execute snipes

# Weather sniper — forecast vs market price arbitrage
polyedge weather --dry   # Watch mode
polyedge weather         # Copilot
polyedge weather --auto  # Autopilot

# Micro sniper — high-frequency momentum trading on 5-min crypto markets
polyedge micro --dry --market "btc 5m"   # Watch BTC 5-min only
polyedge micro --market "btc 5m"         # Copilot — confirm each trade
polyedge micro --auto --market "btc 5m"  # Autopilot — auto-execute

# Start the general agent
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
│   ├── binance_aggtrade.py # Binance aggTrade feed (tick-level order flow)
│   ├── binance_feed.py   # Binance WebSocket price feed (BTC/ETH/SOL)
│   ├── book_analyzer.py  # Order book microstructure analysis
│   ├── indexer.py        # Market sync (API → DB)
│   ├── markets.py        # Gamma API fetching
│   ├── orderbook.py      # Order book fetching
│   ├── weather_feed.py   # Open-Meteo + NOAA weather forecast feed
│   └── ws_feed.py        # WebSocket real-time feed
├── execution/
│   ├── engine.py       # Order placement + risk checks
│   └── tracker.py      # P&L tracking
├── risk/
│   ├── kelly.py        # Kelly criterion
│   ├── portfolio.py    # Portfolio-level risk management
│   └── sizing.py       # Position sizing
├── strategies/
│   ├── cheap_hunter.py    # Underpriced tail events (disabled)
│   ├── crypto_sniper.py   # Real-time crypto price feed arbitrage
│   ├── edge_finder.py     # AI-detected mispricings
│   ├── market_maker.py    # Spread capture (WIP)
│   ├── micro_sniper.py    # HF momentum strategy (aggTrade order flow)
│   ├── micro_runner.py    # Persistent loop for micro sniper
│   ├── sniper_runner.py   # Persistent async loop for crypto sniper
│   ├── weather_sniper.py  # Weather forecast vs market price arbitrage
│   └── weather_runner.py  # Persistent loop for weather sniper
├── dashboard/
│   └── live.py         # Rich terminal dashboard
└── cli.py              # Click CLI entry point
```

## Strategies

### Crypto Sniper (Primary — Real-Time)
Exploits latency between Binance spot prices and Polymarket's short-duration (5-min / 15-min) crypto "Up or Down" markets. Connects to Binance WebSocket for real-time BTC, ETH, and SOL prices. When crypto moves significantly with little time remaining in a Polymarket window, the outcome is near-certain but the market hasn't repriced yet. No AI needed — pure math and speed.

The probability model uses a normal CDF (Abramowitz & Stegun approximation) with sqrt(time) volatility scaling, calibrated against 5-minute BTC dynamics. Conservative tuning includes a 0.3% volatility floor, 15-second execution latency buffer, and 8% minimum edge to trade.

Run via `polyedge sniper` with `--dry`, copilot (default), or `--auto` modes.

### Micro Sniper (High-Frequency Momentum — Real-Time)
Reads Binance aggTrade order flow at tick level (~10-50 trades/sec for BTC) and momentum-trades Polymarket's 5-minute crypto "Up or Down" markets. Unlike the crypto sniper which waits for a clear directional move in the final 90 seconds, the micro sniper trades every momentum swing throughout the entire 5-minute window.

The strategy computes a composite momentum signal from -1 (strong sell) to +1 (strong buy) using: short-term order flow imbalance (5s window, 40% weight), medium-term OFI (15s, 30%), VWAP drift (20%), and trade intensity surge (10%). When momentum exceeds the entry threshold (0.40) with sufficient trade activity (10+ trades in 15s window), it enters a position. It exits when momentum dies or reverses, and can flip (close + reopen opposite side) on strong reversals above 0.50 with 25+ confirming trades.

Safety rails prevent buying nearly-dead sides (min price 0.15) or heavily-priced sides (max price 0.80). Gap detection resets stale data on WebSocket reconnect. Window hopping pre-loads 10 upcoming windows for seamless transitions. Binance feeds are narrowed to only matched symbols for minimal latency.

Can produce 10-20+ round-trip trades per 5-minute window. No AI needed — pure microstructure math and speed. Zero API cost.

Run via `polyedge micro --dry --market "btc 5m"` (watch mode), `polyedge micro --market "btc 5m"` (copilot), or `polyedge micro --auto --market "btc 5m"` (autopilot).

### AI Edge Finder
Uses LLMs to estimate true probabilities independent of market price. When `AI_probability - market_price > 10%`, generates a trade signal. The AI analyst operates with a strong market-efficiency prior — it defaults to agreeing with market price and requires concrete evidence to diverge. Two-tier model system:

- **Compute model** (Haiku) — quick-scores candidates on mispricing potential, ~$0.001/market
- **Research model** (Sonnet) — deep analysis on top candidates with news + order book context, ~$0.003/market

Candidate selection uses a scoring system that favors mid-range prices (20-80%), moderate liquidity ($2K-$100K), and filters out meme/celebrity/entertainment categories and short-duration crypto markets (handled by sniper).

### Weather Sniper (Data-Driven)
Trades Polymarket weather markets (temperature buckets, precipitation) by comparing ensemble weather forecasts against market prices. Uses Open-Meteo's GFS ensemble data (free, no API key) for probability estimation and NOAA for US market resolution source matching. Also detects neg-risk arbitrage on multi-bucket temperature events when YES prices don't sum to $1.00. No AI needed — pure data comparison.

Run via `polyedge weather` with `--dry`, copilot (default), or `--auto` modes.

### Cheap Event Hunter (Disabled)
Previously scanned for outcomes priced under $0.15. Disabled after testing showed mechanical heuristic boosts generate false positives without domain-specific awareness. May be re-enabled with smarter filtering in the future.

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
| min_edge_threshold | 10% | Minimum edge to trade (AI strategies) |
| min_confidence | 65% | Minimum AI confidence |
| max_trades_per_day | 20 | Daily trade limit |
| daily_loss_limit_pct | 15% | Stop-loss for the day |
| drawdown_circuit_breaker | 25% | Pause trading if bankroll drops 25% |
| max_analysis_cost_per_day | $5.00 | AI API budget cap |

## Data Pipeline

1. **Market Indexer** syncs all markets from Gamma API to PostgreSQL every 15 minutes (paginated, handles 1000+ markets)
2. **Price History** records snapshots each sync for movement detection
3. **Market Lifecycle** automatically deactivates markets that vanish from API or pass end date
4. **Polymarket WebSocket Feed** provides real-time price/book updates via `wss://ws-subscriptions-clob.polymarket.com`
5. **Binance WebSocket Feed** provides real-time crypto prices (BTC, ETH, SOL) via `wss://stream.binance.com:9443` — no API key needed, combined streams for all symbols
6. **Binance aggTrade Feed** provides tick-level trade data with buy/sell classification for order flow analysis — the core data source for the micro sniper
6. **Order Book Analyzer** extracts microstructure intelligence (imbalance, depth, whales, walls)

## Agent Scan Cycle

Each cycle (every 5 minutes):

1. Load markets from DB (auto-sync if stale)
2. Score and rank top 10 AI candidates (mid-range price, moderate liquidity, recent movers)
3. Filter out blacklisted categories (meme, celebrity, entertainment) and short-duration crypto (handled by sniper)
4. Quick-score with Haiku on mispricing potential ($0.001/market)
5. Deep-analyze top half with Sonnet + news + book context ($0.003/market)
6. AI operates with market-efficiency prior — needs strong evidence to disagree with market price
7. Generate signals when edge > 10% and confidence > 65%, size with quarter-Kelly, execute
8. Record memories and lessons for future context

The crypto sniper, micro sniper, and weather sniper each run as separate persistent loops (`polyedge sniper`, `polyedge micro`, `polyedge weather`), independent of the 5-minute agent cycle.

## Stack

- Python 3.11+ with asyncio
- py-clob-client (Polymarket SDK)
- asyncpg + PostgreSQL
- Anthropic SDK (Claude) + OpenAI SDK
- websockets (Polymarket + Binance real-time feeds)
- Click (CLI) + Rich (terminal UI)
- Pydantic v2 (config + models)

## Testing

```bash
.venv/bin/pytest tests/ -v
```

120+ tests covering models, Kelly criterion, position sizing, portfolio risk, order book analysis, strategy logic, crypto sniper probability model, micro sniper momentum logic, weather sniper ensemble forecasting, neg-risk detection, and market regex matching.

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
