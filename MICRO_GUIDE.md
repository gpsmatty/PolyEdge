# PolyEdge Micro Sniper — Strategy & CLI Guide

## What It Does

The micro sniper reads real-time Binance order flow (aggTrade) and trades Polymarket's short-duration (5m/15m) Bitcoin up/down markets. No AI — pure math and speed. It enters when order flow momentum is strong in one direction and exits when momentum fades or reverses.

## Quick Start

```bash
# Watch mode (no trades, just signals)
polyedge micro --dry --market "btc 15m"

# Copilot mode (asks confirmation before each trade)
polyedge micro --market "btc 15m"

# Auto-execute (fully autonomous)
polyedge micro --auto --market "btc 15m"

# Verbose (show every evaluation)
polyedge micro --auto --market "btc 15m" -v

# Quiet (only trades + status, no eval spam)
polyedge micro --auto --market "btc 15m" -q
```

## CLI Commands Reference

### Core Commands

| Command | Description |
|---------|-------------|
| `polyedge micro` | Start micro sniper |
| `polyedge price-logger` | Standalone price logger — keeps DB trend context fresh across restarts |
| `polyedge sniper` | Start crypto sniper (separate strategy) |
| `polyedge weather` | Start weather sniper |
| `polyedge autopilot` | Start AI agent (5-min scan cycle) |
| `polyedge status` | Smoke test all CLOB connectivity |
| `polyedge positions` | Show open positions |
| `polyedge dashboard` | Real-time terminal UI |

### Micro Flags

| Flag | Description |
|------|-------------|
| `--auto` | Auto-execute trades without confirmation |
| `--dry` | Watch only — no trades placed |
| `--market "btc 15m"` | Filter markets by keyword match |
| `-v` / `--verbose` | Show detailed eval output |
| `-q` / `--quiet` | Only show trades and status lines |
| `--no-warmup` | Skip warmup, trade current window immediately |

### P&L Commands

| Command | Description |
|---------|-------------|
| `polyedge pnl` | Show quick P&L summary |
| `polyedge pnl reconcile` | Pull fills from CLOB API and compute real P&L with fees |
| `polyedge pnl history` | Show recent reconciled trades in table format |
| `polyedge pnl history -n 50` | Show last 50 trades |
| `polyedge pnl strategy` | Break down P&L by strategy |
| `polyedge pnl cleanup` | Find orphaned positions/trades (dry run) |
| `polyedge pnl cleanup --fix` | Remove orphaned positions/trades |
| `polyedge pnl debug-fills` | Dump raw CLOB fill data to debug fee format |

### Config Commands

| Command | Description |
|---------|-------------|
| `polyedge config show` | Display all config from DB |
| `polyedge config set <key> <value>` | Change a single config value |
| `polyedge config save` | Push current in-memory settings to DB |

### Other Commands

| Command | Description |
|---------|-------------|
| `polyedge setup` | Generate wallet and derive API creds |
| `polyedge init` | Interactive setup wizard |
| `polyedge initdb` | Run DB schema migrations |
| `polyedge sync` | Sync markets from Polymarket API to DB |
| `polyedge scan` | One-shot market scan |
| `polyedge search "query"` | Search markets by keyword |
| `polyedge price "query"` | Get current price for a market |
| `polyedge book "query"` | Show order book for a market |
| `polyedge feed "query"` | Live WebSocket price feed |
| `polyedge movers` | Show biggest price movers |
| `polyedge costs` | Show AI API costs |
| `polyedge vault store\|list\|remove` | Manage Keychain secrets |

---

## How the Micro Sniper Works

### Signal Generation

Momentum composite score from -1 (strong sell/bearish) to +1 (strong buy/bullish):

| Component | Weight | Source |
|-----------|--------|--------|
| OFI 5s | 10% | 5-second order flow imbalance |
| OFI 15s | 50% | 15-second order flow imbalance (main signal) |
| VWAP drift | 25% | Volume-weighted price movement |
| Trade intensity | 15% | Spike in trade rate vs baseline |

All from Binance aggTrade WebSocket — real-time buy/sell classification at tick level.

### Entry Logic

A position is opened when ALL of these are true:

1. `|momentum| >= entry_threshold` (0.50)
2. Confidence > min_confidence (0.40)
3. At least 10 trades in the 15s flow window
4. Market price between min_entry_price (0.35) and max_entry_price (0.65)
5. Persistence filter: signal sustained for 2 seconds in same direction
6. Counter-trend filter: if 30s OFI disagrees with entry, requires 0.55 momentum instead of 0.50
7. **5-minute trend bias**: if BTC has moved >0.15% in 5 min, blocks entries against the trend. >0.30% = hard block. Between 0.15%-0.30% = threshold boost (+0.10). This is the persistence layer — survives window hops and restarts via DB.
8. Trade cooldown: 30 seconds since last trade on same market
9. Window hop cooldown: 30 seconds after hopping to new window
10. At least 15 seconds remaining in window

### Exit Logic

Three exit triggers (first one that fires wins):

1. **Reversal exit**: Momentum reverses past exit_threshold (0.20) against position. If 30s trend still agrees with position, uses higher counter_trend_exit_threshold (0.45) instead.
2. **Trailing stop**: When profit exceeds ~10% from entry, arms a trailing stop. Triggers when price drops 12% from high water mark. Locks in profits on winners.
3. **Force exit**: Dumps position with <8 seconds remaining in window regardless of signals.

Hold threshold is disabled (set to 0) — the trailing stop and reversal exit handle all exits.

### Exit Execution

- FOK (Fill or Kill) orders sweep the book at best available prices
- Price floor = market price - exit_slippage (0.05)
- Exit escalation: each failed FOK attempt adds 3 cents to floor slippage, preventing stuck exit loops
- Fast path: uses locally-tracked position size (~200ms)
- If "not enough balance": instantly re-queries CLOB and retries with real size

### Entry Execution

- FOK order at market price + entry_slippage (0.02)
- Size: fixed $5.00 per trade (fixed_position_usd)
- If FOK rejected (thin book), applies trade cooldown and moves on

---

## Config Reference

All configs are under `strategies.micro_sniper.*` namespace. Set via:
```bash
polyedge config set strategies.micro_sniper.<key> <value>
```

### Current Recommended Settings (March 2026)

#### Entry Settings
| Key | Value | Why |
|-----|-------|-----|
| `entry_threshold` | 0.50 | Only enter on strong momentum. 0.40 lets in too much noise |
| `counter_trend_threshold` | 0.55 | Higher bar for entries against the 30s trend |
| `min_entry_price` | 0.35 | Avoid deep OTM positions with huge % swings |
| `max_entry_price` | 0.65 | Avoid overpaying near certainty |
| `entry_persistence_enabled` | true | Filter out momentum spikes |
| `entry_persistence_seconds` | 2.0 | Signal must sustain for 2 seconds (count-based was too fast at ~450ms) |
| `entry_slippage` | 0.02 | 2 cents above market for instant FOK fill |
| `min_confidence` | 0.40 | Minimum confidence score |
| `min_trades_in_window` | 10 | Need enough data for OFI to be meaningful |

#### Exit Settings
| Key | Value | Why |
|-----|-------|-----|
| `exit_threshold` | 0.20 | Exit when momentum reverses moderately, don't wait for full reversal |
| `counter_trend_exit_threshold` | 0.45 | When 30s trend agrees with us, tolerate more reversal before exiting |
| `hold_threshold` | 0 | Disabled — trailing stop and exit_threshold handle exits. Was causing premature sells on momentum pauses |
| `exit_slippage` | 0.05 | 5 cents below market floor for FOK. Was 0.02, caused repeated FOK rejections |
| `trailing_stop_enabled` | true | Locks in profits on winners |
| `trailing_stop_pct` | 0.12 | Exit when price drops 12% from HWM. Was 0.25 (too loose, exited below entry) |
| `force_exit_seconds` | 8.0 | Dump everything with <8s left in window |

#### Rate Limiting
| Key | Value | Why |
|-----|-------|-----|
| `trade_cooldown` | 30 | 30 seconds between trades on same market. Was 10 — too fast, caused buy-sell-rebuy at worse price |
| `window_hop_cooldown` | 30 | 30 seconds after window hop before trading. Was 15 — flow windows need more warmup |
| `max_trades_per_window` | 8 | Cap trades per 15-min window. Was 20 — too many in chop, fee death |
| `min_seconds_remaining` | 15 | Don't enter with <15s left |

#### Position Sizing
| Key | Value | Why |
|-----|-------|-----|
| `fixed_position_usd` | 5.0 | Fixed $5 per trade. Set to 0 to use Kelly sizing instead |
| `max_position_per_trade` | 0.03 | 3% of bankroll max (only used if fixed_position_usd is 0) |
| `min_liquidity` | 500 | Skip markets with <$500 liquidity |

#### Signal Weights
| Key | Value | Why |
|-----|-------|-----|
| `weight_ofi_5s` | 0.10 | 5s OFI — short burst, noisy |
| `weight_ofi_15s` | 0.50 | 15s OFI — main signal, most reliable |
| `weight_vwap_drift` | 0.25 | Price movement weighted by volume |
| `weight_intensity` | 0.15 | Trade rate spike detection |

#### Persistent Trend Context (NEW)
| Key | Value | Why |
|-----|-------|-----|
| `trend_bias_enabled` | true | Uses 5-minute rolling window + DB price history to block counter-trend entries |
| `trend_bias_min_pct` | 0.0015 | 0.15% move over 5 min to consider "trending" — boosts entry threshold |
| `trend_bias_strong_pct` | 0.003 | 0.30% move = strong trend — hard blocks counter-trend entries entirely |
| `trend_bias_counter_boost` | 0.10 | Added to entry_threshold for moderate counter-trend entries |
| `trend_log_interval` | 30 | Seconds between DB price log snapshots |
| `trend_warmup_seconds` | 60 | Seconds of live data needed before trend is trusted |

#### Poly Book (Disabled)
| Key | Value | Why |
|-----|-------|-----|
| `poly_book_enabled` | false | Polymarket order book integration. Entry veto + exit override. Disabled while core execution is being tuned |

#### Other
| Key | Value | Why |
|-----|-------|-----|
| `dead_market_band` | 0.02 | Skip entry when YES is within 2c of 0.50 (market not reacting) |
| `enable_flips` | false | Strong reversals just EXIT instead of flip. Flips double the risk |

---

## Lessons Learned

### What Works
- **5-minute persistent trend**: Cross-window, cross-restart awareness via DB price log + 300s flow window. Prevents the #1 loss pattern: shorting brief pullbacks in a 5-minute rally.
- **Trailing stop at 12%**: Locks in profits on real moves. The +$1.36 and +$1.18 winners were both trailing stop exits.
- **Exit escalation**: Each failed FOK adds 3c to floor. Prevents the 7-rejection loop that turned winners into losers.
- **Persistence filter (2s)**: Filters out sub-second momentum spikes that immediately reverse.
- **Entry threshold 0.50**: Winners consistently have momentum >0.55. Entries at 0.40-0.48 are coin flips that lose to fees.
- **FOK sweep**: Already sweeps the book like the Polymarket UI. No improvement needed there.

### What Doesn't Work
- **Hold threshold > 0**: Causes premature exits on momentum pauses. Mom goes from -0.60 to -0.02 (still bearish) and the bot dumps. Then momentum resumes and it re-enters at a worse price. Disabled (set to 0).
- **Entry persistence count-based**: 3 evals at 30+ tps = ~450ms. Way too fast. Time-based (2 seconds) is correct.
- **Trailing stop at 25%**: By the time price drops 25% from HWM, exit is below entry price for mid-range entries (0.30-0.60). 12% is tight enough to lock in profit.
- **Exit slippage 0.02**: Too tight. FOK rejected repeatedly as bids evaporated. 0.05 gives enough floor.
- **Trade cooldown 10s**: Too fast in chop. Sells NO at loss, re-enters NO 10 seconds later at worse price. 30s prevents whipsaw re-entries.
- **Low entry threshold (0.40)**: Every brief momentum spike triggers an entry. Most don't follow through. Churns fees.
- **Trading in tight chop**: When BTC ranges $30-50 with no direction, every signal is a fakeout. The bot trades 16+ times and loses on most. The max_trades_per_window cap (8) helps limit the damage.

### Market Conditions
- **Best**: Strong directional moves with pullbacks. BTC drops $100+ or rallies $100+. Momentum signals have follow-through.
- **Worst**: Tight range chop. BTC oscillates $30-50 with no direction. Every entry immediately reverses. Persistence filter helps but can't fully prevent it.
- **Watch out**: Slow grinds up/down. The bot keeps entering against the trend on brief pullbacks. The 30s counter-trend filter catches some but not all.

### CLOB Quirks
- **Balance returns 0 after fill**: CLOB takes 2-3 seconds to settle. Don't query balance right after a buy. Use local tracking instead.
- **fee_rate_bps is 1000 (10%) on all fills**: This is a max/cap, not the actual fee. Real taker fee is 2% (200 bps). The reconciler uses hardcoded 200 bps.
- **Raw balance format is inconsistent**: Sometimes micro-units (divide by 1e6), sometimes direct. The exit retry tries multiple interpretations and picks closest to expected.

---

## DB Schema (Trade Logging)

Every micro trade logs `config_snapshot` and `signal_data` as JSONB columns in the `trades` table for backtesting:

**config_snapshot**: entry_threshold, counter_trend_threshold, exit_threshold, hold_threshold, trailing_stop_enabled/pct, exit_slippage, entry_slippage, trade_cooldown, all signal weights, persistence settings.

**signal_data**: momentum, confidence, ofi_5s, ofi_15s, vwap_drift, trade_intensity, binance_price, market_price, price_change_pct, seconds_remaining.

Query example:
```sql
SELECT trade_id, side, entry_price, exit_price, pnl,
  config_snapshot->>'entry_threshold' as entry_thresh,
  signal_data->>'momentum' as momentum,
  signal_data->>'ofi_15s' as ofi_15s
FROM polyedge.trades
ORDER BY opened_at DESC LIMIT 10;
```

---

## Config Priority

Highest wins:
1. **Database** (`risk_config` table) — `polyedge config set/show`
2. **macOS Keychain** — secrets only (`polyedge vault`)
3. **Environment variables**
4. **`.env` file**
5. **YAML** (`config/default.yaml`)
6. **Pydantic defaults** in `config.py`

DB values override code defaults. If you change a default in code but the DB has an old value, the DB wins. Always use `polyedge config set` to change trading params.
