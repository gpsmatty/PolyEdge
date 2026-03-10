# PolyEdge: AI-Powered Prediction Market Trading

## The Thesis

Prediction markets are one of the few places where retail traders can compete with institutions. The markets are small enough that sophisticated participants don't bother, yet liquid enough to trade meaningfully. Most participants are retail bettors who trade on gut feeling, creating systematic mispricings that a disciplined, data-driven approach can exploit.

But not all edges are created equal. Research into how profitable Polymarket traders actually operate (on-chain analysis, community strategies, bot reverse-engineering) reveals that only about 7.6% of wallets are consistently profitable. The winners fall into a few categories: latency arbitrage on short-duration markets, information edges in niche domains, market making with real infrastructure, and disciplined neg-risk arbitrage. The losers overfit to headlines, cast too wide a net, or compete on speed without the infrastructure to win.

PolyEdge is built to find and trade specific, defensible mispricings — not to spray AI analysis at every market on the platform.

## How Prediction Markets Work

Polymarket is a binary options exchange. Each market asks a yes/no question (e.g., "Will X happen by Y date?"). You buy YES contracts at some price between $0.01 and $0.99. If the event happens, each YES contract pays $1. If not, it pays $0.

The market price of a YES contract represents the crowd's implied probability. A YES price of $0.40 means the market thinks there's a 40% chance the event happens.

The edge comes when the market is wrong.

## The Strategies

### 1. Crypto Sniper (Primary Strategy — Real-Time)

The highest-conviction edge in the system. Polymarket runs 5-minute and 15-minute "Up or Down" markets on BTC, ETH, and SOL. These ask: "Will BTC be higher or lower at time T than at time T-5min?" The market starts around 50/50 and adjusts as price moves.

The edge: Binance spot price moves faster than Polymarket reprices. If BTC pumps 1% with 60 seconds left, the outcome is roughly 90%+ certain, but Polymarket may still show 65/35. We buy the near-certain outcome at a discount.

**How it works:**

PolyEdge connects to the Binance public WebSocket API (no API key needed) and streams real-time prices for BTC, ETH, and SOL. Simultaneously, it fetches active crypto "Up or Down" markets from Polymarket every 60 seconds. On every Binance price tick, the sniper evaluates all tracked markets.

**The probability model** computes the likelihood that the current price direction holds through expiry:

1. Track the price change since the market window opened (e.g., BTC up 0.8%)
2. Estimate remaining volatility using sqrt(time) scaling from 5-minute BTC dynamics (typical 5-min std dev is 0.15-0.25%)
3. Compute a z-score: `z = price_change / remaining_volatility`
4. Convert to probability via the normal CDF (Abramowitz & Stegun approximation, accurate within 0.0005)

**Conservative tuning:**

- Volatility floor of 0.3% per 5-min window (prevents overconfidence in calm markets)
- 15-second execution latency buffer added to remaining time (accounts for order placement delay)
- Minimum 0.2% price move to consider (filters noise)
- Entry only in last 90 seconds of window (when direction is most established)
- Minimum 8% edge to trade (implied_prob - market_price must exceed 0.08)
- Max 5% of bankroll per snipe ($10 on $200)

**Calibration benchmarks:**

- A 0.5% move with 30s left: ~85% implied probability
- A 1.0% move with 30s left: ~95% implied probability
- A 0.2% move with 120s left: ~60% implied probability (filtered out — not enough edge)

No AI needed. Zero API cost per trade. Pure math and speed.

### 2. AI Edge Finder (Secondary — 5-Minute Cycle)

Uses LLMs to estimate true probabilities on longer-duration markets (politics, sports, economics, etc.) where the market price may diverge from reality.

**Key design principle: the market-efficiency prior.** Prediction markets are generally well-calibrated. The AI analyst is instructed to default to agreeing with the market price and only diverge when it has strong, specific evidence. This prevents the LLM from hallucinating edges that don't exist — a problem that was burning real money in early testing.

**The pipeline:**

1. **Candidate scoring** — Don't analyze everything. Score the top 10 markets on a 0-9 scale based on: mid-range prices (20-80% get highest scores), moderate liquidity ($2K-$100K sweet spot), recent volume, and time to resolution. Markets in blacklisted categories (meme, celebrity, entertainment) are excluded. Short-duration crypto markets are excluded (handled by the sniper).

2. **Quick scoring** — Haiku (cheap model) scores each candidate 0-100 on mispricing potential (not "trading potential" — the distinction matters). This costs ~$0.001 per market and filters out efficiently-priced markets.

3. **Deep analysis** — Sonnet (research model) gets the top half. Each analysis includes market question, resolution criteria, current price/volume, order book microstructure, news context (when API key is configured), and agent memory. The model outputs a calibrated probability estimate with explicit reasoning about what evidence justifies disagreeing with the market.

4. **Edge calculation** — `|AI_probability - market_price| > 10%` AND `confidence > 65%` to generate a signal. These thresholds are deliberately high — a 10% edge on a prediction market is a strong claim.

5. **Position sizing** — Quarter Kelly (25% of optimal) to protect against estimation errors.

**Cost control:** ~$0.03-0.05 per scan cycle. With 5-minute cycles, that's ~$0.36-0.60/hour, well under the $5/day budget cap.

### 3. Cheap Event Hunter (Disabled)

Previously scanned for outcomes priced under $0.15 using heuristic boosts (liquidity discount, cheap event bias, time factor). Disabled after live testing revealed the heuristics generate false positives — the system flagged 27 cheap events as opportunities when none had genuine edge. Without domain-specific awareness, mechanical price heuristics can't distinguish between "underpriced" and "correctly priced at near-zero." May be re-enabled with smarter filtering or an AI overlay.

### 4. Market Maker (Work in Progress)

For liquid markets with wide spreads (>5 cents), quote both sides — buy YES below midpoint, sell YES above. Capture the spread while managing inventory risk. This strategy requires more active order management and will be built on top of the WebSocket feed for real-time book monitoring.

### 5. Weather Sniper (Data-Driven — Polling Loop)

Polymarket has 300+ active weather markets covering daily city temperatures, precipitation, and global weather anomalies. These resolve against objective data sources (NOAA for US, Weather Underground for international). The edge: free professional-grade forecast APIs provide the same data, and crowd pricing often disagrees with professional models.

**Two edge types:**

**Forecast edge** — Open-Meteo's GFS ensemble API provides multiple model runs for each forecast day (up to 50 ensemble members). If 35 out of 50 ensemble members predict a high temperature of 45-50°F but the market prices that bucket at 40%, we have a 30% edge. No AI needed — just count ensemble members in range.

**Neg-risk arbitrage** — Multi-bucket temperature events (e.g., 5 temperature ranges covering all possibilities) should have YES prices summing to $1.00. When they don't, there's a risk-free profit. If the sum exceeds $1.00, selling all buckets locks in guaranteed profit. If it's below $1.00, buying all buckets does the same.

**Data sources:**

- Open-Meteo (open-meteo.com): free, no API key, GFS ensemble forecasts. Provides natural probability distributions via ensemble spread. 10,000 calls/day on free tier.
- NOAA (api.weather.gov): free, no API key, official US government data that directly matches Polymarket's resolution source for US weather markets.

**Supported cities:** NYC, London, Seoul, Chicago, Miami, LA, Seattle — expandable via location registry.

**Conservative tuning:**

- 10% minimum edge to trade (forecast probability must diverge from market by at least 10%)
- 60% minimum ensemble confidence
- 3% minimum neg-risk edge (to account for spread and fees)
- 8% of bankroll max per weather trade ($16 on $200)
- Forecasts refresh every 30 minutes; markets refresh every 5 minutes

Run via `polyedge weather` with `--dry`, copilot (default), or `--auto` modes.

## Order Book Intelligence

Most prediction market bots only look at price. PolyEdge also reads the order book to extract microstructure signals:

- **Imbalance** — When buy-side depth significantly outweighs sell-side (or vice versa), it signals directional pressure that hasn't yet moved the price. A +0.30 imbalance ratio means 30% more buy pressure than sell pressure.

- **Whale detection** — Orders larger than 2x the average size at a level are flagged as whale orders. Whale bias (are the big players buying or selling?) is a strong signal of informed trading.

- **Wall detection** — Single levels with 5x+ average size act as support/resistance. Price often bounces off these walls, informing entry and exit timing.

- **Spread analysis** — Wide spreads (>200 basis points) indicate thin liquidity where limit orders can capture value. Tight spreads mean the market is efficient and harder to trade.

This intelligence is distilled into ~100 tokens of text and fed to the AI alongside price data, keeping prompt costs minimal while giving the model richer context than price alone.

## Risk Management Philosophy

With a $200 starting bankroll, capital preservation is existential. One bad day shouldn't end the experiment. The risk framework is layered:

**Position level:**
- Quarter Kelly sizing (dramatically reduces variance vs full Kelly)
- Max 10% of bankroll per position (5% for crypto sniper)
- Minimum 10% edge and 65% confidence for AI trades; 8% edge for crypto sniper

**Portfolio level:**
- Max 50% total exposure (always keep half in reserve)
- Max 10 concurrent positions
- Category diversification (don't go all-in on one type of event)

**Daily level:**
- Max 20 trades per day
- 15% daily loss limit (stop trading if down $30)
- $5 AI budget cap

**Emergency:**
- 25% drawdown circuit breaker (if bankroll drops to $150, pause everything)
- Manual override via Ctrl+C in any mode

Every parameter lives in the database — portable across any environment. Change anything at runtime with `polyedge config set`, no restart needed.

## Security: Secrets in the Vault

Trading bots handle sensitive credentials — wallet private keys, API keys, database URLs. PolyEdge stores all secrets in macOS Keychain rather than plaintext `.env` files.

The Keychain is encrypted at rest by macOS and unlocks when you log into your Mac. No Touch ID prompts during normal operation — the bot reads secrets silently as long as you're logged in. Lock your Mac and the secrets are locked too.

A built-in `polyedge vault` CLI manages the Keychain entries. The config loader checks Keychain first, then falls back to environment variables and `.env` files, so you can migrate gradually or use different approaches per environment.

## Agent Memory

The agent maintains persistent memory across sessions in PostgreSQL:

- **Trade decisions** — What was traded, why, at what price. Expires after 30 days.
- **Skip reasons** — Why a market was passed on. Expires after 6 hours (prevents re-analyzing bad candidates).
- **Lessons** — What happened when positions resolved. Never expires. These are the most valuable memories — they calibrate the agent's future decisions.

Before analyzing a market, the agent retrieves its memory context and feeds it to the AI. This means the agent gets smarter over time as it accumulates experience about which categories it trades well, which market types are traps, and how its confidence correlates with actual outcomes.

## Data Architecture

**Market Indexer:** The Polymarket Gamma API returns ~100 markets per page. The indexer paginates through all of them (up to 5,000), upserts to PostgreSQL, records price snapshots for historical analysis, and deactivates markets that have vanished from the API or passed their end date. This runs every 15 minutes by default.

**Polymarket WebSocket Feed:** For real-time market data, PolyEdge connects to Polymarket's market WebSocket channel. No authentication needed. Receives order book snapshots, incremental price changes, trade executions, and best bid/ask updates.

**Binance WebSocket Feed:** For real-time crypto price data, PolyEdge connects to Binance's public combined streams endpoint. Tracks BTC, ETH, and SOL via 24hr ticker updates. Maintains latest price snapshots and rolling price windows per symbol. Automatic reconnection with exponential backoff (2s base, 30s max). No API key needed — fully public data.

**PostgreSQL Schema:** 10 tables across markets, orders, trades, positions, AI analyses, portfolio snapshots, risk config, price history, AI cost log, and agent memory. All data persists across restarts.

## Operating Modes

**General Agent** (`polyedge autopilot`):

| Mode | Use When | Behavior |
|------|----------|----------|
| **Signals** | Learning / testing | AI generates signals, displays them, doesn't trade |
| **Copilot** | Building confidence | AI recommends trades with reasoning, you approve each one |
| **Autopilot** | Proven edge | AI scans, analyzes, sizes, and executes autonomously |

**Crypto Sniper** (`polyedge sniper`):

| Mode | Flag | Behavior |
|------|------|----------|
| **Dry run** | `--dry` | Shows opportunities in real time, doesn't trade |
| **Copilot** | (default) | Shows opportunities, asks for confirmation on each snipe |
| **Autopilot** | `--auto` | Auto-executes snipes when edge exceeds threshold |

The sniper runs as a separate persistent process from the general agent. You can run both simultaneously — the general agent handles long-duration markets on a 5-minute cycle, while the sniper handles crypto markets tick-by-tick.

## What Makes This Different

**Monolith over microservices.** Previous attempt (PredictMcap) was over-engineered with WebSocket feeds, RabbitMQ, separate services. Never got to actual trading. PolyEdge is one Python process that does everything.

**Trading from day one.** No paper trading mode. Real money, real consequences, real learning. The $200 bankroll is small enough to be educational tuition if things go wrong.

**Cost-conscious AI.** Every LLM call is budget-tracked, two-tier models keep costs under $1/hour, and the system degrades gracefully when budget runs out. The crypto sniper operates at zero AI cost — pure math.

**Persistent learning.** Agent memory means the system gets better over time without manual intervention. Calibration tracking shows whether the AI is actually good at probability estimation, not just confident.

**Portable config.** All trading config lives in PostgreSQL, not local files. Move from Mac to VPS to App Platform — your risk limits, AI settings, and strategy config follow the database.

## Lessons Learned

Early testing with real money revealed several things:

**Casting too wide a net burns capital.** The original system analyzed 20+ markets per cycle with a 5% edge threshold and 60% confidence minimum. It spent $0.37 analyzing markets like "Will Jesus Christ return before GTA VI?" and Czech hockey league outcomes, finding zero actionable edges. The AI was generating probabilities that agreed with the market within 2-3% on almost everything — as it should, since prediction markets are generally well-calibrated.

**Heuristic strategies without domain awareness are noise generators.** The cheap event hunter flagged 27 markets as opportunities. None had genuine edge. Mechanical boosts (liquidity discount, cheap event bias) can't tell the difference between "correctly priced at 5%" and "underpriced at 5%."

**The real edges are specific and fast.** Research into profitable Polymarket strategies (on-chain analysis, community discussion, bot behavior) consistently points to: latency arbitrage on short-duration markets, information edges in narrow domains with objective data, and neg-risk arbitrage on multi-bucket events. The common thread is specificity — knowing exactly where the mispricing comes from and why.

**Market-efficiency prior is essential for AI strategies.** LLMs are confidently wrong about event probabilities unless they're given strong grounding. The AI analyst now starts from the assumption that the market is correct and needs concrete evidence to disagree by more than a few percent.

## The Bet

If prediction markets are inefficient in specific, identifiable ways (and the evidence suggests they are — particularly in short-duration crypto markets where price feeds lag, and in niche categories where crowd knowledge is thin), then a system that targets those specific inefficiencies with proper risk management should generate positive expected value over time.

The starting bankroll is $200. The goal isn't to get rich — it's to prove the edge exists, measure it, and then decide whether to scale.
