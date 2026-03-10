# PolyEdge: AI-Powered Prediction Market Trading

## The Thesis

Prediction markets are one of the few places where retail traders can compete with institutions. The markets are small enough that sophisticated participants don't bother, yet liquid enough to trade meaningfully. Most participants are retail bettors who trade on gut feeling, creating systematic mispricings that a disciplined, data-driven approach can exploit.

PolyEdge is built to find and trade those mispricings automatically.

## How Prediction Markets Work

Polymarket is a binary options exchange. Each market asks a yes/no question (e.g., "Will X happen by Y date?"). You buy YES contracts at some price between $0.01 and $0.99. If the event happens, each YES contract pays $1. If not, it pays $0.

The market price of a YES contract represents the crowd's implied probability. A YES price of $0.40 means the market thinks there's a 40% chance the event happens.

The edge comes when the market is wrong.

## The Three Strategies

### 1. Cheap Event Hunter (Zero AI Cost)

The simplest strategy, runs on pure math. Scans all active markets for outcomes priced under $0.15 (less than 15% implied probability). At these prices, even a small probability bump creates massive expected value.

**The math:** If you buy YES at $0.05 and the true probability is 8%, your expected value per dollar is `(0.08 - 0.05) / 0.05 = 0.60` — that's 60 cents of expected profit per dollar risked.

**Why cheap events get mispriced:**
- Retail traders exhibit "longshot bias" — they overpay for flashy long shots but underprice boring ones
- Illiquid markets have wider mispricings (fewer participants = less efficiency)
- Events far from resolution get less attention, creating opportunities

The Cheap Hunter uses heuristics (liquidity discount, cheap event bias, time factor) to estimate which cheap events are genuinely underpriced. No AI needed, no API costs, and it catches opportunities the fancier strategies might miss.

### 2. AI Edge Finder (The Main Strategy)

This is where the real alpha lives. Uses large language models to estimate the true probability of events, then trades when the AI's estimate diverges significantly from the market price.

**The pipeline:**

1. **Candidate selection** — Don't analyze everything. Pick the top 20 markets by: price movement in the last hour (something happened) and volume (most liquid = best for trading).

2. **Quick scoring** — Haiku (cheap model) scores each candidate 0-100 on trading potential. This costs ~$0.001 per market and filters out efficiently-priced markets before we spend real money.

3. **Deep analysis** — Sonnet (research model) gets the top half of scored candidates. Each analysis includes:
   - The market question and resolution criteria
   - Current price and volume data
   - Order book microstructure (imbalance, whale activity, support/resistance walls)
   - Recent news context
   - Agent memory (past trades and lessons on this market)

   The model outputs a calibrated probability estimate with reasoning chain, confidence score, and risk factors.

4. **Edge calculation** — `AI_probability - market_price = edge`. If the absolute edge exceeds 5% and confidence exceeds 60%, generate a trade signal.

5. **Position sizing** — Kelly criterion determines how much to bet. We use quarter-Kelly (25% of the mathematically optimal bet) to protect the bankroll from estimation errors.

**Cost control:** The two-tier model system keeps AI costs around $0.05 per scan cycle. With 5-minute cycles, that's ~$0.60/hour, well under the $5/day budget cap. Budget is tracked persistently in the database with mid-loop kill switches.

### 3. Market Maker (Work in Progress)

For liquid markets with wide spreads (>5 cents), quote both sides — buy YES below midpoint, sell YES above. Capture the spread while managing inventory risk. This strategy requires more active order management and will be built on top of the WebSocket feed for real-time book monitoring.

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
- Max 10% of bankroll per position
- Minimum 5% edge and 60% confidence to trade

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

Every parameter is configurable in YAML and overridable via database at runtime.

## Agent Memory

The agent maintains persistent memory across sessions in PostgreSQL:

- **Trade decisions** — What was traded, why, at what price. Expires after 30 days.
- **Skip reasons** — Why a market was passed on. Expires after 6 hours (prevents re-analyzing bad candidates).
- **Lessons** — What happened when positions resolved. Never expires. These are the most valuable memories — they calibrate the agent's future decisions.

Before analyzing a market, the agent retrieves its memory context and feeds it to the AI. This means the agent gets smarter over time as it accumulates experience about which categories it trades well, which market types are traps, and how its confidence correlates with actual outcomes.

## Data Architecture

**Market Indexer:** The Polymarket Gamma API returns ~100 markets per page. The indexer paginates through all of them (up to 5,000), upserts to PostgreSQL, records price snapshots for historical analysis, and deactivates markets that have vanished from the API or passed their end date. This runs every 15 minutes by default.

**WebSocket Feed:** For real-time data, PolyEdge connects to Polymarket's market WebSocket channel. No authentication needed. Receives order book snapshots, incremental price changes, trade executions, and best bid/ask updates. Maintains local book state with automatic reconnection and exponential backoff.

**PostgreSQL Schema:** 10 tables across markets, orders, trades, positions, AI analyses, portfolio snapshots, risk config, price history, AI cost log, and agent memory. All data persists across restarts.

## Operating Modes

| Mode | Use When | Behavior |
|------|----------|----------|
| **Signals** | Learning / testing | AI generates signals, displays them, doesn't trade |
| **Copilot** | Building confidence | AI recommends trades with reasoning, you approve each one |
| **Autopilot** | Proven edge | AI scans, analyzes, sizes, and executes autonomously |

Start in signals mode, graduate to copilot once you trust the signals, then autopilot once you've verified the edge is real.

## What Makes This Different

**Monolith over microservices.** Previous attempt (PredictMcap) was over-engineered with WebSocket feeds, RabbitMQ, separate services. Never got to actual trading. PolyEdge is one Python process that does everything.

**Trading from day one.** No paper trading mode. Real money, real consequences, real learning. The $200 bankroll is small enough to be educational tuition if things go wrong.

**Cost-conscious AI.** Every LLM call is budget-tracked, two-tier models keep costs under $1/hour, and the system degrades gracefully when budget runs out (cheap hunter still works, AI just pauses).

**Persistent learning.** Agent memory means the system gets better over time without manual intervention. Calibration tracking shows whether the AI is actually good at probability estimation, not just confident.

## The Bet

If prediction markets are inefficient (and the evidence suggests they are, especially in the long tail), then a disciplined system that combines AI probability estimation with proper risk management should generate positive expected value over time.

The starting bankroll is $200. The goal isn't to get rich — it's to prove the edge exists, measure it, and then decide whether to scale.
