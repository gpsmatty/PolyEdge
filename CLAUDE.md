# PolyEdge — AI Context Document

This document is for AI assistants working on this codebase. It contains everything needed to understand, modify, and extend PolyEdge.

## What This Project Is

PolyEdge is an AI-powered trading bot for Polymarket prediction markets. It is a monolith Python application that scans markets, uses LLMs to estimate true probabilities, detects mispricings, sizes positions with Kelly criterion, and executes real trades via the Polymarket CLOB API. It includes a real-time crypto sniper that exploits latency between Binance spot prices and Polymarket's short-duration crypto markets, a weather sniper that compares ensemble weather forecasts against Polymarket weather market prices, and a high-frequency micro sniper that reads Binance aggTrade order flow to momentum-trade short-duration (5m/15m) crypto up/down markets. The micro sniper is the primary active strategy and the main focus of ongoing development. Starting bankroll is $200 USD.

## Project Root

`/Users/gpsmatty/production/PolyEdge/`

## Stack

- Python 3.11+ (currently 3.13), async throughout
- `asyncpg` for PostgreSQL
- `py-clob-client` — official Polymarket CLOB SDK (REST only, no WebSocket)
- `websockets` — for real-time market data feed (Polymarket + Binance)
- `anthropic` SDK + `openai` SDK — LLM backends
- `pydantic` v2 + `pydantic-settings` — all models and config
- `click` — CLI framework
- `rich` — terminal UI
- `aiohttp` — async HTTP for Gamma API
- `eth-account` / `web3` — wallet management
- Package manager: `uv` (but `pip install -e .` also works)
- Tests: `pytest` + `pytest-asyncio` (run via `.venv/bin/pytest tests/ -v`)

## File Map

### Core (`src/polyedge/core/`)

| File | Purpose | Key exports |
|------|---------|-------------|
| `config.py` | All configuration. Loads from Keychain + `.env` + `config/default.yaml`. | `Settings`, `load_config()`, `AIConfig`, `RiskConfig`, `AgentConfig`, `CryptoSniperConfig`, `WeatherSniperConfig`, `MicroSniperConfig`, `_get_from_keychain()`, `_set_in_keychain()`, `load_keychain_secrets()`, `KEYCHAIN_KEYS` |
| `models.py` | Every data model (Pydantic). | `Market`, `OrderBook`, `OrderBookLevel`, `Signal`, `Order`, `Position`, `Trade`, `AIAnalysis`, `PortfolioSnapshot`, `Side`, `AgentMode` |
| `client.py` | Wrapper around `py-clob-client`. Handles auth, order placement, market data, trade history, balance checks. | `PolyClient` (methods: `get_collateral_balance()`, `get_token_balance()`, `get_trades()`, `get_order()`) |
| `db.py` | PostgreSQL storage. All tables defined in `SCHEMA_SQL` string at top of file. | `Database` (async, uses connection pool) |

### AI (`src/polyedge/ai/`)

| File | Purpose | Key exports |
|------|---------|-------------|
| `llm.py` | LLM abstraction layer. Supports Claude + OpenAI + ensemble. Has tiered model methods. Tracks cost in DB. | `LLMClient` (methods: `analyze()`, `research()`, `compute()`), `LLMResponse` |
| `analyst.py` | Market analysis prompts. Builds context, parses JSON responses. | `analyze_market()`, `quick_score_market()`, `_build_analysis_prompt()` |
| `agent.py` | The autonomous trading agent. Main loop: scan → analyze → trade. | `TradingAgent` |
| `news.py` | News context retrieval for market analysis. | `get_news_context()` |
| `probability.py` | Probability calibration tracking (stub). | — |

### Data (`src/polyedge/data/`)

| File | Purpose | Key exports |
|------|---------|-------------|
| `markets.py` | Fetches markets from Polymarket Gamma API (`https://gamma-api.polymarket.com`). Handles pagination (100 per page, up to 5000 markets). | `fetch_active_markets()`, `fetch_all_markets()`, `search_markets()` |
| `indexer.py` | Syncs all markets from API to PostgreSQL. Deactivates stale/closed markets. Tracks price history. | `MarketIndexer` |
| `orderbook.py` | Fetches raw order book from CLOB API. | `get_order_book()`, `get_prices()` |
| `book_analyzer.py` | Order book microstructure analysis. Computes imbalance, depth, whale detection, wall detection. | `analyze_book()`, `get_book_intelligence()`, `format_book_for_ai()`, `BookIntelligence` |
| `binance_feed.py` | Binance WebSocket feed for real-time crypto prices (BTC, ETH, SOL). No auth needed. Combined streams, auto-reconnect. | `BinanceFeed`, `PriceSnapshot`, `PriceWindow` |
| `binance_aggtrade.py` | Binance aggTrade WebSocket feed — tick-level trade data with buy/sell classification (~10-50 tps for BTC). Maintains rolling flow windows (5s/15s/30s/5m) for OFI, VWAP drift, trade intensity. Flow-price agreement dampener with configurable score-shaping params. Gap detection resets stale data on reconnect. | `BinanceAggTradeFeed`, `AggTrade`, `MicroStructure`, `TradeFlowWindow` |
| `weather_feed.py` | Weather forecast feed from Open-Meteo (ensemble) and NOAA (official). Caches forecasts, supports multiple locations. No API key needed. | `WeatherFeed`, `EnsembleForecast`, `NOAAForecast`, `LOCATIONS`, `find_location()` |
| `ws_feed.py` | WebSocket feed for real-time Polymarket data. Connects to `wss://ws-subscriptions-clob.polymarket.com/ws/market`. No auth needed. Must send `PING` every 10s. | `MarketFeed` |
| `signals.py` | External data sources (stub). | — |
| `research.py` | Research pipeline for micro sniper. Signal snapshots (complete feature vectors every 2-3s + event-driven), regime tagging (deterministic state machine: trend_up/down, chop, vol_expansion, low_vol), no-trade reason logging, candidate-event logging (near-threshold almost-signals), attribution computation (per-component contribution breakdown), and OFI flip tracking. Buffer-based writes flush to DB every 5s. Schema-versioned rows (SCHEMA_VERSION=1). | `ResearchLogger`, `SignalSnapshot`, `Regime`, `NoTradeReason`, `classify_regime()`, `compute_attribution()`, `OFIFlipTracker` |

### Strategies (`src/polyedge/strategies/`)

| File | Purpose | Key exports |
|------|---------|-------------|
| `base.py` | Strategy ABC with `evaluate(market) -> Signal`. | `Strategy` |
| `crypto_sniper.py` | All crypto market arbitrage. Handles three types: Up/Down (direction via CDF), Threshold ("above X" via log-normal), Bucket ("what price" via range CDF). Supports BTC, ETH, SOL, XRP, DOGE. | `CryptoSniperStrategy`, `CryptoMarketType`, `ParsedCryptoMarket`, `SniperOpportunity`, `find_crypto_markets()`, `match_market_to_symbol()` |
| `sniper_runner.py` | Persistent async loop connecting Binance prices to all Polymarket crypto markets. Up/down evaluated on every tick, threshold/bucket evaluated every 30s. Fetches up to 1000 markets. | `SniperRunner` |
| `weather_sniper.py` | Weather forecast arbitrage. Compares Open-Meteo ensemble probabilities to Polymarket weather market prices. Detects neg-risk on multi-bucket events. | `WeatherSniperStrategy`, `WeatherOpportunity`, `NegRiskOpportunity`, `find_weather_markets()`, `group_weather_events()` |
| `weather_runner.py` | Persistent async loop for weather sniper. Refreshes markets (5 min) and forecasts (30 min), evaluates all weather markets. | `WeatherRunner` |
| `micro_sniper.py` | High-frequency momentum strategy for short-duration (5m/15m) crypto up/down markets. Reads Binance aggTrade order flow (OFI, VWAP drift, intensity) with flow-price agreement dampener. Entry/exit/flip logic with configurable thresholds. Zero AI cost. | `MicroSniperStrategy`, `MicroAction`, `MicroOpportunity` |
| `micro_runner.py` | Persistent async loop for micro sniper. Connects Binance aggTrade + Polymarket WS. Evaluates on every 5th trade tick. Handles window hopping (pre-loads 10 windows), position tracking, sell orders. Narrows Binance feeds to only matched symbols. Hot-reload config from DB every 30s. | `MicroRunner` |
| `cheap_hunter.py` | Finds underpriced tail events (<$0.15). Zero AI cost. **Disabled** — heuristic boosts generate false positives. | `CheapHunterStrategy` |
| `edge_finder.py` | Converts AI analysis into trade signals when edge > 10% threshold. | `EdgeFinderStrategy` |
| `market_maker.py` | Spread capture strategy (WIP, not fully implemented). | `MarketMakerStrategy` |

### Scripts (`scripts/`)

| File | Purpose | Key exports |
|------|---------|-------------|
| `trade_analysis.py` | Comprehensive post-trade attribution analysis. Breaks down performance by entry quality, direction, trend alignment, time remaining, exit reason, signal components, FOK rejections, and execution quality. Usage: `python scripts/trade_analysis.py --hours 4` | — |
| `label_outcomes.py` | Offline outcome labeling for signal snapshots. Computes future BTC/token price moves at 5s/10s/20s/30s horizons + max favorable/adverse excursion (MFE/MAE) within 30s. Uses signal_snapshots table as price source (snapshots every 2-3s provide the timeline). Usage: `python scripts/label_outcomes.py --limit 5000` or `--stats`. | — |

### Risk (`src/polyedge/risk/`)

| File | Purpose | Key exports |
|------|---------|-------------|
| `kelly.py` | Kelly criterion: `f* = (bp - q) / b`. Supports fractional Kelly. | `kelly_fraction()`, `fractional_kelly()`, `kelly_from_market_price()` |
| `sizing.py` | Bankroll-aware position sizing. Combines Kelly with max position limits. | `calculate_position_size()` |
| `portfolio.py` | Portfolio-level risk checks: max positions, max exposure, daily limits, drawdown circuit breaker. | `PortfolioRiskManager`, `PortfolioSnapshot` |

### Execution (`src/polyedge/execution/`)

| File | Purpose | Key exports |
|------|---------|-------------|
| `engine.py` | Places orders via CLOB API. Runs risk checks before every trade. Requires user confirmation in copilot mode. | `ExecutionEngine` |
| `tracker.py` | P&L tracking and display (internal, today's trades). | `PnLTracker` |
| `reconciler.py` | P&L reconciliation against CLOB API fills. Pulls actual fill prices and fees (hardcoded 2% taker fee — CLOB's `fee_rate_bps` field returns 1000/10% cap, not real fee), matches buy/sell pairs (FIFO), computes gross/net/true-net P&L. Also checks for resolved markets. | `PnLReconciler` |

### CLI (`src/polyedge/cli.py`)

Click-based CLI. Commands: `setup`, `init`, `scan`, `search`, `price`, `hunt`, `edges`, `analyze`, `trade`, `positions`, `pnl`, `pnl reconcile`, `pnl history`, `pnl strategy`, `pnl cleanup`, `pnl debug-fills`, `status`, `autopilot`, `sniper`, `weather`, `micro`, `price-logger`, `dashboard`, `initdb`, `sync`, `costs`, `movers`, `book`, `feed`, `config`, `vault`.

The `pnl cleanup` command finds orphaned positions/trades (dry run by default, `--fix` to remove). The `pnl debug-fills` command dumps raw CLOB fill data including fee_rate_bps distribution — useful for verifying fee calculations.

The `status` command smoke tests all CLOB connectivity: wallet balance, API creds, open orders, trade history access, and order signing.

The `sniper` command runs the crypto sniper as a persistent real-time loop (separate from the 5-minute `autopilot` agent). Flags: `--auto` (auto-execute), `--dry` (watch only).

The `weather` command runs the weather sniper as a persistent polling loop. Flags: `--auto` (auto-execute), `--dry` (watch only). Default is copilot mode.

The `micro` command runs the micro sniper as a persistent real-time loop (separate from both the `sniper` and `autopilot` commands). Reads Binance aggTrade tick data for order flow momentum. Flags: `--auto` (auto-execute), `--dry` (watch only), `--market "btc 5m"` (filter by keyword), `-v` (verbose), `-q` (quiet). Default is copilot mode.

## Database Schema

All tables in the `polyedge` schema (PostgreSQL). Defined in `core/db.py` as the `SCHEMA_SQL` string.

| Table | Purpose |
|-------|---------|
| `markets` | All known markets. Upserted from Gamma API. PK: `condition_id`. |
| `orders` | Order log. PK: `order_id` (UUID). |
| `trades` | Trade log with entry/exit/P&L. PK: `trade_id` (UUID). |
| `positions` | Open positions. Unique on `(market_id, token_id, side)`. |
| `ai_analyses` | Every AI analysis result with probability, confidence, reasoning, cost. |
| `portfolio_snapshots` | Point-in-time portfolio state. |
| `risk_config` | All trading config (risk, AI, agent, strategies). Key-value JSONB. Portable across environments. |
| `price_history` | Price snapshots per market per sync. Used for price mover detection. |
| `ai_cost_log` | Every AI API call with tokens, cost, purpose. Used for budget tracking. |
| `agent_memory` | Persistent agent memory: trade decisions, skip reasons, lessons. |
| `pnl_ledger` | Reconciled P&L entries: each row is a completed buy/sell pair or resolution with gross P&L, fees, net P&L, gas estimate. |
| `reconcile_state` | Tracks last CLOB API sync cursor for incremental reconciliation. |
| `micro_price_log` | Persistent price snapshots for micro sniper trend context. Logged every ~30s per symbol. Loaded on startup for cross-restart awareness. Auto-pruned to 60 min. |
| `signal_snapshots` | Research pipeline data. Complete feature vector snapshots (~50 fields as JSONB) logged every 2-3s + event-driven (trades, candidates, threshold crossings). Denormalized columns for fast queries (regime, dampened_momentum, btc_price, yes_price, seconds_remaining, trade_fired, no_trade_reason, near_threshold). Outcome labels (btc/token moves at 5/10/20/30s + MFE/MAE) added offline by `label_outcomes.py`. Schema-versioned (version=1). Pruned: periodic snapshots 72h, trade/candidate 7 days. |

## Polymarket API Details

**Gamma API** (`https://gamma-api.polymarket.com`):
- `/markets` — Returns market metadata. Paginated, 100 per page, use `offset` parameter.
- `/events` — Event groups.
- No auth needed.

**CLOB API** (`https://clob.polymarket.com`):
- Order placement, cancellation, order books, prices.
- Auth via API key/secret/passphrase derived from wallet private key (EIP-712 signing).
- Accessed through `py-clob-client` SDK.

**WebSocket** (`wss://ws-subscriptions-clob.polymarket.com/ws/market`):
- Subscribe with `assets_ids` (token IDs, NOT condition IDs), `type: "market"`, `custom_feature_enabled: true`.
- Events: `book` (full snapshot), `price_change` (delta), `last_trade_price`, `best_bid_ask`, `tick_size_change`, `new_market`, `market_resolved`.
- Must send `PING` text every 10 seconds, receive `PONG`.
- Dynamic subscribe/unsubscribe with `operation` field.
- No auth needed for market channel.

**Key distinction:** `condition_id` identifies a market. `clob_token_ids` are the numeric token IDs for YES (index 0) and NO (index 1) outcomes. The CLOB and WebSocket APIs use token IDs, not condition IDs.

## Binance API Details

**WebSocket** (`wss://stream.binance.com:9443`):
- Combined streams: `/stream?streams=btcusdt@ticker/ethusdt@ticker/solusdt@ticker`
- Format: `{"stream": "btcusdt@ticker", "data": {...}}`
- Key fields in ticker data: `c` (last price), `b` (best bid), `a` (best ask), `v` (24h volume), `P` (24h change %)
- No authentication needed — fully public API
- Ping interval: 20 seconds (handled by websockets library)
- Auto-reconnect with exponential backoff (2s base, 30s max)

Used exclusively by the crypto sniper strategy as a price oracle to compare against Polymarket's short-duration crypto markets.

## Agent Scan Cycle (How It Works)

Defined in `agent.py` `_scan_cycle()`. Every 5 minutes:

1. **Housekeeping** — Review resolved positions for lessons, clean expired memories.
2. **Circuit breaker check** — Daily trade limit, daily loss limit.
3. **Load markets** — From DB via `MarketIndexer` (auto-syncs from API if stale, every 15 min).
4. **Pick AI candidates** — `_pick_ai_candidates()`: scores markets 0-9 via `_candidate_score()` based on price range (mid-range 20-80% scores highest), liquidity ($2K-$100K sweet spot), volume, and time to resolution. Filters out blacklisted categories (meme, celebrity, entertainment) via `_is_blacklisted()` and short-duration crypto markets via `_is_short_duration_crypto()` (handled by sniper). Capped at `max_markets_per_scan` (default 10).
5. **Quick score** — `quick_score_market()` via compute model (Haiku). Scores 0-100 on **mispricing potential** (not trading potential). ~$0.001 each.
6. **Deep analysis** — Top half of scored candidates get `analyze_market()` via research model (Sonnet). AI operates with **market-efficiency prior** — defaults to agreeing with market price, needs strong evidence to diverge. Includes order book context, news context (only when API key configured), agent memory context. ~$0.003 each.
7. **Signal generation** — If `|AI_prob - market_price| > 10%` (min_edge_threshold) and `confidence > 65%` (min_confidence), create a `Signal`.
8. **Execution** — Based on mode: autopilot (auto-execute), copilot (recommend + confirm), signals (display only).
9. **Memory** — Record trade decisions and skip reasons.

## Crypto Sniper Loop (How It Works)

Defined in `sniper_runner.py` `SniperRunner.run()`. Runs as a persistent async process, separate from the agent scan cycle. Handles three market types:

1. **Connect** — Opens WebSocket to Binance.US combined streams for BTC, ETH, SOL, XRP, DOGE tickers.
2. **Market refresh** — Every 60 seconds, fetches up to 1000 active markets from Polymarket Gamma API. Classifies into three types: Up/Down, Threshold ("above X"), Bucket ("what price will X hit"). Groups up/down by symbol for tick evaluation; threshold/bucket go to slow eval.
3. **Up/Down evaluation** (every price tick) — On every Binance price tick (~1/sec per symbol), evaluates up/down markets. Computes P(direction holds) via normal CDF with sqrt(time) volatility scaling. Trades when edge > 8% in last 90 seconds of window.
4. **Threshold evaluation** (every 30s) — For "above X" markets, computes P(price > strike at expiry) using log-normal model (zero-drift GBM). Uses per-symbol annualized volatility estimates.
5. **Bucket evaluation** (every 30s) — For "what price" markets, computes P(price in [low, high]) = CDF(high) - CDF(low). Handles arrow-style buckets (↑ above, ↓ below) and range buckets.
6. **Edge check** — All types: if `implied_prob - market_price > 8%` (min_edge), generate a `SniperOpportunity`.
7. **Execution** — Size with Kelly (max 5% of bankroll per snipe). In `--dry` mode, display only. In copilot, ask for confirmation. In `--auto`, execute immediately. Tracks `_traded_markets` set to prevent double-entry.
8. **Status** — Prints price/stats summary every 30 seconds showing up/down and threshold/bucket market counts separately.

## Weather Sniper Loop (How It Works)

Defined in `weather_runner.py` `WeatherRunner.run()`. Runs as a persistent async process, separate from the agent and crypto sniper:

1. **Start** — Launches three concurrent async tasks: market refresh, forecast refresh, and status display.
2. **Market refresh** (every 5 min) — Fetches weather markets from Polymarket Gamma API via `find_weather_markets()`. Filters to configured locations. Groups multi-bucket events via `group_weather_events()` (uses `groupSlug` from raw Gamma data, falls back to `groupItemTitle`/category).
3. **Forecast refresh** (every 30 min) — For each tracked weather market, fetches ensemble forecast from Open-Meteo. Parses market question to extract location, target date, and bucket range via `WeatherSniperStrategy.parse_market()`.
4. **Evaluation** — For each market with a forecast, computes forecast probability (fraction of ensemble members in bucket range) and compares to market price. If `forecast_prob - market_price > 10%` (min_edge) and ensemble confidence > 60%, generates a `WeatherOpportunity`.
5. **Neg-risk check** — For each event group (multi-bucket temperature events), checks if YES prices sum != $1.00. If sum > $1.00 by more than 3% (min_neg_risk_edge), signals sell-all arbitrage. If sum < $1.00 by same margin, signals buy-all.
6. **Execution** — Size with Kelly (max 8% of bankroll per weather trade). In `--dry` mode, display only. In copilot, ask for confirmation. In `--auto`, execute immediately.
7. **Status** — Prints tracked market count and opportunity stats every 60 seconds.

## Micro Sniper Loop (How It Works)

Defined in `micro_runner.py` `MicroRunner.run()`. Runs as a persistent async process, separate from the agent, crypto sniper, and weather sniper. Reads tick-level Binance aggTrade data to momentum-trade Polymarket's short-duration (5m/15m) crypto up/down markets.

1. **Start** — Loads markets from DB/API, narrows Binance feeds to only matched symbols (e.g., only `btcusdt@aggTrade` when `--market btc 5m`), connects Binance aggTrade + Polymarket WebSocket. Loads persistent price context from `micro_price_log` DB table (last 30 min) so the bot immediately knows the macro trend on startup. Skips the first partial window to warm up microstructure data — waits for the next fresh window before trading.
2. **aggTrade callback** (every 5th trade tick, ~2-10 evals/sec) — On each Binance aggTrade, updates `MicroStructure` rolling flow windows (5s/15s/30s/5m OFI, VWAP drift, trade intensity). The 5m window is persistent — does NOT reset on window hops, providing cross-window context. Evaluates only the current (first) window — rest are pre-loaded for seamless hopping.
3. **Momentum signal** — Composite score from -1 (strong sell) to +1 (strong buy): 10% OFI_5s + 50% OFI_15s + 25% VWAP drift + 15% intensity surge. The raw composite is then multiplied by a **flow-price agreement dampener** — a continuous factor from 0.4 (flow opposed price movement) through 0.65 (price flat despite flow) to 1.0 (flow confirmed by price). This kills false signals where aggressive order flow gets absorbed by the book without displacing price. VWAP drift is scaled by `vwap_drift_scale` (default 2000) to normalize BTC dollar moves into the [-1, 1] signal range.
4. **Entry** — When `|momentum| > 0.50` (entry_threshold), confidence > 0.40, at least 10 trades in the 15s window, and market price between 0.35-0.65, enter a position (BUY YES if bullish, BUY NO if bearish). **Entry persistence filter**: signal must sustain for 2 seconds in the same direction (time-based, not count-based). **5-minute trend bias**: if BTC has moved >0.15% in 5 min (from persistent 5m flow window or DB history), blocks or penalizes counter-trend entries. >0.30% = hard block, 0.15%-0.30% = boosts entry_threshold by 0.10. Survives window hops and restarts. **30s trend filter**: if entry direction disagrees with the 30-second OFI trend, requires higher threshold of 0.55 (counter_trend_threshold) instead of 0.50. This kills counter-trend entries that are the #1 source of losses.
5. **Exit** — Three triggers: (a) momentum reverses past exit_threshold (0.20) against our position, (b) trailing stop triggers at 12% drawdown from high water mark (locks in profits on winners), (c) force exit with <8s remaining. Hold threshold is disabled (set to 0) — caused premature exits on momentum pauses.
5a. **Exit escalation** — Each failed FOK sell attempt adds 3 cents to the floor price slippage, preventing stuck exit loops where the bot tries to sell 7+ times while the price drops.
6. **Flip** — Disabled by default (`enable_flips=false`). Strong reversals trigger EXIT instead. When enabled: momentum reverses past flip_threshold (0.50) with confidence > 0.50 and at least 25 trades → close + open opposite side.
7. **Window hopping** — Pre-loads 10 upcoming windows. When current window expires, instantly promotes next window. Refetches from API when ≤3 windows remain. Uses `asyncio.Lock` to prevent concurrent API fetches.
8. **Gap detection** — If >5 seconds pass between aggTrade ticks (WebSocket disconnect), resets all flow windows to avoid trading on stale momentum.
9. **Trade cooldown** — 30 seconds between trades on the same market to prevent whipsaw re-entries (sell at loss then re-buy at worse price).
10. **Config/signal logging** — Every trade logs `config_snapshot` (all thresholds, weights, persistence settings) and `signal_data` (momentum, OFI, VWAP drift, intensity, prices, seconds remaining) as JSONB columns for backtesting.
11. **Hot-reload config** — `_config_refresh_loop` reads DB every 30 seconds, pushes updated values to Settings, MicroSniperConfig, and all MicroStructure instances (weights + dampener params). `polyedge config set` takes effect within 30s without restart. Logs detected changes as `CONFIG RELOADED: key: old → new`.
12. **Fill price accuracy** — `_get_fill_info()` fetches actual fill price from CLOB trade history (prefers trade-level fills over order-level). After entry, waits 5 seconds for CLOB balance to settle before allowing exit attempts.
13. **Status** — Prints microstructure state every 15 seconds. Includes research pipeline counters (total snapshots, trade events, candidate events).
14. **Research pipeline** — `ResearchLogger` from `data/research.py` runs as a background task. Builds `SignalSnapshot` feature vectors (~50 fields) on every periodic tick (2s) and on every trade event. Snapshots include: all OFI windows, raw/dampened momentum, dampener factor, regime tag, trend context, position state, attribution breakdown. Candidates (momentum within 80% of threshold) and no-trade reasons (which filter blocked an entry) are tagged on the snapshots. Buffer-based writes flush every 5s. Old periodic snapshots auto-pruned at 72h, trade/candidate snapshots at 7 days.

The `micro` CLI command runs the micro sniper: `polyedge micro --dry --market "btc 5m"`. Flags: `--auto` (auto-execute), `--dry` (watch only), `--market` (filter by keyword), `-v` (verbose), `-q` (quiet).

**Micro sniper config keys** (all under `strategies.micro_sniper.*`):
- `enabled` (bool, default true)
- `symbols` (list[str], default ["btcusdt"])
- `entry_threshold` (float, default 0.50) — only enter on strong momentum; 0.40 lets in too much noise, winners consistently >0.55
- `counter_trend_threshold` (float, default 0.55) — higher bar for entries against the 30s trend
- `exit_threshold` (float, default 0.20) — exit when momentum reverses moderately, don't wait for full reversal
- `hold_threshold` (float, default 0, DISABLED) — was causing premature exits on momentum pauses. Trailing stop + exit_threshold handle all exits.
- `entry_persistence_enabled` (bool, default true) — filter out sub-second momentum spikes
- `entry_persistence_seconds` (float, default 2.0) — signal must sustain for 2 seconds in same direction (count-based was too fast at ~450ms)
- `enable_flips` (bool, default false) — disabled by default, strong reversals just EXIT
- `flip_threshold` (float, default 0.50) — only used if enable_flips=true
- `flip_min_confidence` (float, default 0.50)
- `min_confidence` (float, default 0.40)
- `min_trades_in_window` (int, default 10) — need enough data for OFI to be meaningful
- `min_trades_for_flip` (int, default 25)
- `min_seconds_remaining` (float, default 15.0) — don't enter with <15s left. Typically set to 120 via DB for 15m windows to avoid late entries
- `force_exit_seconds` (float, default 8.0) — dump everything with <8s left in window
- `trailing_stop_enabled` (bool, default true) — locks in profits on winners
- `trailing_stop_pct` (float, default 0.12) — exit when price drops 12% from HWM. Was 0.25 (too loose, exited below entry)
- `min_entry_price` (float, default 0.35) — avoid deep OTM positions with huge % swings
- `max_entry_price` (float, default 0.65) — avoid overpaying near certainty
- `max_position_per_trade` (float, default 0.03 = 3% of bankroll) — used if fixed_position_usd is 0
- `fixed_position_usd` (float, default 5.0) — fixed $5 per trade, 0 = use Kelly sizing
- `max_trades_per_window` (int, default 8) — cap trades per 15-min window. Was 20 — too many in chop, fee death
- `min_liquidity` (float, default 500) — skip markets with <$500 liquidity
- `dead_market_band` (float, default 0.02) — skip entry when YES is within this band of 0.50 (market not reacting to price moves)
- `weight_ofi_5s` (float, default 0.10) — 5s OFI — short burst, noisy
- `weight_ofi_15s` (float, default 0.50) — 15s OFI — main signal, most reliable
- `weight_vwap_drift` (float, default 0.25) — price movement weighted by volume
- `weight_intensity` (float, default 0.15) — trade rate spike detection
- `vwap_drift_scale` (float, default 2000.0) — normalizes BTC dollar moves to [-1,1] signal range. At 2000, ~$35 BTC move maxes drift. Was 5000 (too sensitive, $14 maxed it)
- `dampener_agree_factor` (float, default 1.0) — dampener multiplier when OFI and price move in same direction (no penalty)
- `dampener_disagree_factor` (float, default 0.4) — dampener multiplier when OFI and price move in opposite directions (heavy penalty)
- `dampener_flat_factor` (float, default 0.65) — dampener multiplier when price is flat despite OFI (moderate penalty)
- `dampener_price_deadzone` (float, default 0.05) — abs(drift_signal) below this is considered "flat price" for dampener
- `entry_slippage` (float, default 0.02) — 2 cents above market for instant FOK fill on entry
- `exit_slippage` (float, default 0.05) — 5 cents below market floor for FOK. Was 0.02, caused repeated FOK rejections
- `trade_cooldown` (float, default 30.0) — 30 seconds between trades on same market. Was 10 — too fast, caused buy-sell-rebuy at worse price
- `window_hop_cooldown` (float, default 30.0) — 30 seconds after window hop before trading
- `counter_trend_exit_threshold` (float, default 0.45) — when 30s trend agrees with us, tolerate more reversal before exiting
- `poly_book_enabled` (bool, default false) — master toggle for Polymarket order book integration
- `poly_book_min_exit_depth` (float, default 20.0) — min bid depth (contracts within 5c) to enter; ensures exit path exists
- `poly_book_imbalance_weight` (float, default 0.10) — weight of Poly book imbalance in momentum composite
- `poly_book_imbalance_veto` (float, default -0.40) — block entry if Poly book imbalance is this negative against our direction
- `poly_book_exit_override_depth` (float, default 25.0) — hold instead of exit if our token's bid depth exceeds this
- `poly_book_exit_override_imbalance` (float, default 0.15) — hold instead of exit if directional imbalance exceeds this
- `trend_bias_enabled` (bool, default true) — uses 5-minute rolling window + DB price history to block counter-trend entries
- `trend_bias_min_pct` (float, default 0.0015) — 0.15% move over 5 min to consider "trending" (boosts entry threshold)
- `trend_bias_strong_pct` (float, default 0.003) — 0.30% move = strong trend, hard blocks counter-trend entries
- `trend_bias_counter_boost` (float, default 0.10) — added to entry_threshold for moderate counter-trend entries
- `trend_log_interval` (float, default 30.0) — seconds between DB price log snapshots
- `trend_warmup_seconds` (float, default 60.0) — seconds of live data needed before trend is trusted

### Polymarket Order Book Integration (Micro Sniper)

Behind the `poly_book_enabled` toggle. Reads live Polymarket order book via WebSocket (`ws_feed.py`) and runs `analyze_book()` from `book_analyzer.py` to produce `BookIntelligence` for both YES and NO tokens.

**Entry-side features:**
1. **Exit liquidity check** — Before entering, checks bid depth on the token we're about to buy (bids = our exit path). If bid depth within 5 cents < `poly_book_min_exit_depth`, blocks entry entirely. Prevents entering positions we can't cleanly exit.
2. **Book imbalance veto** — Uses YES token's near-touch imbalance as market sentiment. If imbalance is strongly against our direction (< `poly_book_imbalance_veto`), blocks entry. Polymarket participants are voting against us.

**Exit-side features:**
3. **Book exit override** — When momentum triggers an exit (reversal or fade), checks if the Poly book disagrees. If our token's bid depth > `poly_book_exit_override_depth` AND directional imbalance > `poly_book_exit_override_imbalance`, overrides the exit and holds. Both conditions must be true. Prevents momentum noise from dumping a position that the Polymarket book still supports. Logs `BOOK OVERRIDE: Holding {side}` when it kicks in.

**Data flow:** `MicroRunner._get_book_intel(market)` → calls `analyze_book()` on each token's WebSocket book snapshot → passes `dict[str, BookIntelligence]` to `MicroSniperStrategy.evaluate()` → used in `_evaluate_entry()` for veto checks and `_evaluate_with_position()` for exit override.

## Tiered AI Model System

| Tier | Config Key | Default Model | Cost | Used For |
|------|-----------|---------------|------|----------|
| Research | `ai.research_model` | `claude-sonnet-4-6` | ~$0.003/market | Deep probability estimation, news interpretation, complex reasoning |
| Compute | `ai.compute_model` | `claude-haiku-4-5-20251001` | ~$0.001/market | Quick scoring, EV calculations, pre-filtering |

Called via `llm.research(prompt, system)` and `llm.compute(prompt, system)`. Both route through `llm.analyze()` which handles budget checks and cost logging.

**Budget control:** Checked before every API call and mid-loop during scan cycles. Tracked in `ai_cost_log` table. Default limit: $5/day.

## Order Book Intelligence

`book_analyzer.py` produces a `BookIntelligence` dataclass from raw `OrderBook` data. Key metrics:

- `imbalance_ratio` — `(bid_depth - ask_depth) / total`. Range [-1, 1]. >+0.3 = buy pressure.
- `imbalance_5c` / `imbalance_10c` — Same but only for levels within 5/10 cents of best price.
- `whale_bids` / `whale_asks` — Orders >2x average size at their side.
- `bid_wall_price` / `ask_wall_price` — Levels with >5x average size (support/resistance).
- `spread_bps` — Spread in basis points.

The AI sees a ~100 token text summary via `format_book_for_ai()`, not the raw order book.

## Configuration

**Priority order** (highest wins):
1. **Database** (`risk_config` table) — All trading config. Portable across environments (Mac, VPS, App Platform). Managed via `polyedge config` CLI or `polyedge init` wizard.
2. **macOS Keychain** — Secrets only (wallet key, API keys, DB URL). Managed via `polyedge vault` CLI.
3. **Environment variables** — Explicit `export` in shell overrides Keychain.
4. **`.env` file** — Legacy fallback. Not needed if using Keychain.
5. **YAML** (`config/default.yaml`) — Defaults/fallback when DB has no value.
6. **Pydantic defaults** — Hardcoded fallbacks in `config.py`.

**DB config keys** use dot notation: `risk.kelly_fraction`, `ai.max_analysis_cost_per_day`, `agent.mode`, `strategies.cheap_hunter.enabled`, etc. All non-secret config lives in the `risk_config` table as JSONB key-value pairs.

**Config CLI:**
- `polyedge config show` — display all config from DB
- `polyedge config set <key> <value>` — change a single value
- `polyedge config save` — push current in-memory settings to DB

## Secrets / Keychain Integration

Secrets are stored in macOS Keychain under the service name `polyedge`. The `config.py` module handles this:

- `KEYCHAIN_KEYS` — List of all secret field names: `poly_private_key`, `poly_wallet_address`, `poly_api_key`, `poly_api_secret`, `poly_api_passphrase`, `database_url`, `anthropic_api_key`, `openai_api_key`, `news_api_key`.
- `_get_from_keychain(account)` — Reads a secret via `security find-generic-password`.
- `_set_in_keychain(account, value)` — Writes a secret via `security add-generic-password`.
- `load_keychain_secrets()` — Loads all known secrets from Keychain into a dict.
- `load_config()` — Injects Keychain secrets into `os.environ` before `Settings()` reads them.
- `apply_db_config(settings, db)` — Async. Overlays DB config values onto a `Settings` object. DB wins over YAML.
- `save_config_to_db(settings, db)` — Async. Writes all non-secret settings to DB for portability.
- `settings_to_db_dict(settings)` — Flattens `Settings` into namespaced key-value pairs (`risk.kelly_fraction`, etc).

CLI: `polyedge vault store|list|remove [key] [value]` — manages Keychain entries. `store` without a value prompts with hidden input.

**Important:** Never log, print, or expose secret values. The `vault list` command masks values (shows first/last 4 chars only).

## Key Design Decisions

- **Monolith** — Previous project (PredictMcap) was over-engineered microservices. Never shipped.
- **No paper trading** — Real money from day one. $200 bankroll.
- **Quarter Kelly** — Full Kelly is mathematically optimal but assumes perfect probability estimates. Quarter Kelly dramatically reduces variance.
- **Two-tier AI** — Cheap model filters before expensive model analyzes. Keeps costs under $1/hour.
- **Market-efficiency prior** — AI analyst defaults to agreeing with market price. Needs strong specific evidence to disagree by >5%. Prevents LLM from hallucinating edges.
- **Crypto sniper** — Separate persistent loop from the 5-minute agent. Evaluates on every Binance price tick. Zero AI cost — pure math.
- **Weather sniper** — Separate polling loop. Compares free ensemble weather forecasts against Polymarket prices. Also detects neg-risk arbitrage on multi-bucket temperature events. Zero AI cost — pure data comparison.
- **Agent memory** — Persistent across sessions. Lessons never expire. Trade decisions expire after 30 days. Skip reasons expire after 6 hours.
- **Market indexer** — Don't hit the Gamma API every scan cycle. Sync to DB periodically, read from DB always.
- **Keychain over .env** — Secrets encrypted at rest by macOS. No plaintext keys in the repo directory.
- **Config in DB** — Trading config stored in PostgreSQL, not local YAML files. Portable across Mac, VPS, App Platform, etc.
- **Hot-reload config** — Micro sniper polls DB every 30 seconds for config changes. No restart needed to tune thresholds, weights, or dampener params during live trading.
- **Flow-price agreement dampener** — Core signal quality innovation. Aggressive order flow that doesn't displace price was absorbed by the book — not tradeable edge. Continuous dampener scales momentum by how well OFI direction is confirmed by actual VWAP movement. Biggest single improvement to signal quality.
- **FOK as natural filter** — Fill-or-Kill orders on thin Polymarket liquidity act as an accidental risk filter. When the market moves fast, FOK rejections prevent entries into adverse conditions.
- **Micro sniper focus** — Project started broader (AI agent, crypto sniper, weather sniper, cheap hunter) but the micro sniper is now the primary active strategy and main development focus.

## Testing

```bash
.venv/bin/pytest tests/ -v
```

120+ tests. Pure logic tests (no DB, no API mocks needed for most). Test files:

| File | Tests |
|------|-------|
| `test_models.py` | Pydantic model validation, properties, edge cases |
| `test_kelly.py` | Kelly criterion math, fractional Kelly, market price conversion |
| `test_sizing.py` | Position sizing with bankroll limits |
| `test_cheap_hunter.py` | Cheap event detection, filtering, batch ranking |
| `test_portfolio_risk.py` | Risk checks: max positions, exposure, daily limits, circuit breaker |
| `test_book_analyzer.py` | Order book analysis: spread, imbalance, whales, walls, depth |
| `test_crypto_sniper.py` | Normal CDF accuracy, market type classification (up/down, threshold, bucket), symbol extraction (BTC/ETH/SOL/XRP/DOGE), threshold probability (log-normal), bucket probability (range CDF), direction probability, regex patterns, strike extraction, arrow/range bucket parsing |
| `test_weather_sniper.py` | Ensemble probability (in-range, above, below, boundary), location matching, market parsing (bucket ranges, below/above, precipitation), weather identification, event grouping, evaluate with forecast, neg-risk detection, confidence scoring, config defaults, signal conversion |
| `test_micro_sniper.py` | Momentum entry/exit/flip logic, threshold validation, sparse data guard, min_entry_price guard, confidence filtering, force exit, config defaults, trade count requirements for flips |

## Common Modification Patterns

**Adding a new strategy:**
1. Create `strategies/new_strategy.py` inheriting from `Strategy` (in `base.py`)
2. Implement `evaluate(market) -> Signal | None`
3. Add config model in `config.py`, add to `StrategiesConfig`
4. Add to `default.yaml`
5. Wire into agent's `_scan_cycle()` in `agent.py`

**Adding a new CLI command:**
1. Add function in `cli.py` with `@cli.command()` decorator
2. Use `async def _inner():` pattern with `run_async(_inner())`

**Adding a new DB table:**
1. Add CREATE TABLE to `SCHEMA_SQL` in `db.py`
2. Add index if needed
3. Add async methods on `Database` class
4. Run `polyedge initdb` to apply

**Changing AI models:**
- DB: `polyedge config set ai.research_model claude-sonnet-4-6`
- Or YAML fallback: `config/default.yaml` under `ai.research_model` and `ai.compute_model`
- Cost table: `COST_TABLE` dict in `llm.py`

**Changing risk params at runtime:**
- `polyedge config set risk.kelly_fraction 0.5`
- Agent: takes effect on next scan cycle (no restart needed)
- Micro sniper: hot-reload every 30s via `_config_refresh_loop` (no restart needed)

**Adding a new data source to AI analysis:**
1. Build the data fetcher in `data/`
2. Format as text context string
3. Pass to `analyze_market()` via `additional_context` parameter
4. Or add a new dedicated parameter to `_build_analysis_prompt()` in `analyst.py`

**Adding a real-time strategy (like crypto sniper):**
1. Create `strategies/new_strategy.py` with strategy logic
2. Create `strategies/new_runner.py` with persistent async loop
3. Add config model in `config.py` (see `CryptoSniperConfig` as template), add to `StrategiesConfig`
4. Add to `default.yaml`
5. Add CLI command in `cli.py` that wires up the runner
6. This runs separately from the 5-minute agent cycle

**Crypto sniper config keys** (all under `strategies.crypto_sniper.*`):
- `enabled` (bool, default true)
- `min_edge` (float, default 0.08 = 8%)
- `min_price_move_pct` (float, default 0.002 = 0.2%)
- `max_seconds_before_entry` (float, default 90)
- `symbols` (list[str], default ["btcusdt", "ethusdt", "solusdt", "xrpusdt", "dogeusdt"])
- `max_position_per_trade` (float, default 0.05 = 5% of bankroll)
- `min_liquidity` (float, default 500)

**Weather sniper config keys** (all under `strategies.weather_sniper.*`):
- `enabled` (bool, default true)
- `min_edge` (float, default 0.10 = 10%)
- `min_confidence` (float, default 0.60 = 60% ensemble agreement)
- `min_neg_risk_edge` (float, default 0.03 = 3% for multi-bucket arbitrage)
- `max_position_per_trade` (float, default 0.08 = 8% of bankroll)
- `min_liquidity` (float, default 200)
- `forecast_interval_minutes` (int, default 30)
- `locations` (list[str], default ["nyc", "london", "seoul", "chicago", "miami"])

## Weather API Details

**Open-Meteo Ensemble API** (`https://ensemble-api.open-meteo.com/v1/ensemble`):
- Free, no API key needed. 10,000 calls/day on free tier.
- GFS ensemble forecasts with multiple model runs (up to 50 ensemble members).
- Query params: `latitude`, `longitude`, `daily` (e.g., `temperature_2m_max`), `models=gfs_seamless`, `forecast_days`.
- Response includes `temperature_2m_max_member01`, `_member02`, etc. — each member is one model run.
- Natural probability distribution: count members in range / total members = probability estimate.

**NOAA API** (`https://api.weather.gov`):
- Free, no API key needed. Requires `User-Agent` header.
- Two-step process: `/points/{lat},{lon}` → get grid coordinates → `/gridpoints/{office}/{gridX},{gridY}/forecast`.
- Official US government data that directly matches Polymarket resolution source for US weather markets.
- Used as cross-reference for US locations alongside Open-Meteo ensemble data.

## Owner Preferences

- No `Co-Authored-By` in commit messages
- Handles credentials/wallet privately
- Wants live trading (strongly dislikes paper trading)
- Cost-conscious on AI API usage — always consider token cost
- Starting bankroll: $200 USDC on Polygon
- Wants MetaMask wallet for web UI access alongside the bot's hot wallet
- Prefers practical over perfect — ship it, iterate
