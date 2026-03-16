# Micro Sniper Config Tuning Playbook

This is the "if you see X, adjust Y" reference for the `/tune` command. Each section maps a data observation to a specific config key, direction, and safe nudge range.

**Signal architecture (as of 2026-03-16):** Pure Binance order book depth (`@depth20@100ms`). `depth_signal_weight=1.0`, `depth_aggtrade_weight=0.0`. aggTrade still runs for trend/price tracking but does NOT drive entries or exits. All OFI weights, dampener, and intensity settings are legacy/inactive.

---

## Entry Thresholds

### `entry_threshold` (current 0.40)
**Raise** when: too many losing trades, false signals in chop, losers with low MFE (signal never had conviction)
**Lower** when: missing too many good moves, win rate is high but trade count is very low, large favorable MFE on blocked signals
**Nudge**: +/-0.02-0.05. Never go below 0.35 or above 0.55.
**Data check**: Compare entry momentum of winners vs losers. If winners avg 0.55+ and losers avg 0.42, threshold is too low.

### `counter_trend_threshold` (current 0.40)
Higher bar for entries against the 30s trend direction.
**Raise** when: counter-trend trades are underperforming with-trend trades
**Lower** when: counter-trend trades are actually outperforming (depth signal sometimes leads the trend flip)
**Nudge**: +/-0.02-0.05. Keep >= entry_threshold.

### `exit_threshold` (current 0.42)
Depth momentum must reverse past this against our position to trigger exit.
**Raise** when: exiting winners too early on depth noise, trades exit in <10s then price continues favorably, hold times under 10s on most trades
**Lower** when: losers not being cut fast enough, large negative avg P&L on reversal exits, hold times >60s on losers
**Nudge**: +/-0.02-0.05. Never below 0.30 (depth is noisy) or above 0.55 (won't exit fast enough).
**Key insight**: Depth momentum swings +/-0.50 regularly as market makers reposition. Setting this too low causes jitter exits, too high traps you in losers.

### `counter_trend_exit_threshold` (current 0.50)
When 30s trend agrees with our position, exit threshold is boosted to this value (more patience).
**Raise** when: winners are exiting too early while trend is still favorable
**Lower** when: losers are being held too long because trend agreement prevents exit. CHECK THIS FIRST on big losses.
**DANGER**: Setting this above 0.60 can trap you in positions during sustained reversals where the 30s trend hasn't flipped yet.
**Nudge**: +/-0.05. Never above 0.60, never below exit_threshold.

---

## Depth Signal Parameters

### `depth_velocity_window_s` (current 6.0)
Lookback window for computing imbalance velocity (how fast the book is tilting).
**Raise** when: signal is too noisy/spiky, entering on individual market maker moves, lots of 7-10s hold times
**Lower** when: signal is too slow, missing moves, entering after price already moved
**Nudge**: +/-1.0s. Range 4.0-12.0. Lower = faster/noisier, higher = smoother/slower.

### `depth_imbalance_levels` (current 5)
Number of price levels used for near-touch imbalance calculation.
**Raise** when: signal is too sensitive to top-of-book noise (single level changes causing signals)
**Lower** when: signal is too slow to react, deep book changes diluting near-touch moves
**Nudge**: +/-1-2. Range 3-10.

### `depth_velocity_scale` (current 5.0)
Multiplier to normalize velocity into [-1,1] range.
**Raise** when: velocity component is saturating (frequently at +/-1.0)
**Lower** when: velocity component is too weak (rarely above +/-0.3)
**Nudge**: +/-0.5-1.0.

### `depth_weight_imbalance_velocity` (current 0.50)
Weight of imbalance velocity in depth momentum composite. This is the primary LEADING signal.
**Note**: Three depth weights should sum to ~1.0: imbalance_velocity + depth_delta + large_order.

### `depth_weight_depth_delta` (current 0.30)
Weight of bid/ask depth growth rate. Measures whether bids or asks are growing in absolute terms.

### `depth_weight_large_order` (current 0.20)
Weight of large order detection. Catches sudden big orders (>3x mean level size).

### `depth_confidence_min_snapshots` (current 10)
Minimum depth snapshots (at 100ms each = 1 second) before confidence > 0.
**Raise** when: entering on insufficient depth data after gaps/reconnects
**Nudge**: +/-5.

### `depth_gap_clear_seconds` (current 2.0)
Seconds of no depth data before clearing history (WebSocket disconnect detection).
**Raise** when: brief network hiccups are wiping depth state
**Lower** when: stale depth data persisting after real disconnects
**Nudge**: +/-0.5s.

### `depth_max_snapshots` (current 200)
Max depth history (200 = 20 seconds at 100ms each).
**Raise** when: need longer lookback for velocity calculations
**Lower** when: memory usage concerns
**Nudge**: +/-50.

---

## Entry Filters

### `entry_persistence_seconds` (current 1.2)
Signal must sustain above threshold for this long before entering.
**Raise** when: many trades entering on sub-2s depth spikes that immediately reverse (check hold times - if most exits are <10s, persistence may be too low)
**Lower** when: valid sustained signals being blocked (check DO logs for failed_persistence on signals that would have been winners)
**CRITICAL**: Depth signals are inherently spiky. At 2.0s, almost NOTHING passes. 1.0-1.5s is the practical range for depth.
**Nudge**: +/-0.2s. Range 0.8-1.8.

### `min_entry_price` (current 0.35) / `max_entry_price` (current 0.62)
Price band for entries. Prevents buying deep OTM/ITM.
**Tighten** when: entries near the edges (0.35-0.38 or 0.58-0.62) are consistently losing
**Widen** when: good signals appearing outside the band (check DO logs for price_band blocks with strong momentum)
**Nudge**: +/-0.02-0.03.
**Note**: Best winners historically enter in the 0.40-0.55 range. Entries above 0.60 have limited upside.

### `dead_market_band` (current 0.02)
Skip entry when YES is within this band of 0.50 (market not reacting to BTC moves).
**Raise** when: trades near 0.50 are consistently losing
**Lower** when: some 0.48-0.52 entries are profitable
**Nudge**: +/-0.005.

### `min_trades_in_window` (current 3)
Minimum aggTrade trades in the 15s window. With depth as primary signal, this is less critical but still provides a data quality floor.
**Nudge**: +/-2.

---

## Trend Filters

### `trend_bias_strong_pct` (current 0.003 = 0.30%)
5-minute BTC price change that triggers hard block on counter-trend entries.
**Raise** when: trend blocks are too aggressive, blocking valid counter-trend entries that would win
**Lower** when: entering against strong trends and losing
**Nudge**: +/-0.001.

### `trend_bias_counter_boost` (current 0.05)
Added to entry threshold for moderate counter-trend entries (5m trend between min_pct and strong_pct).
**Raise** when: moderate counter-trend entries underperforming
**Lower** when: counter-trend boost blocking too many valid entries
**Nudge**: +/-0.02.

### Chop filter (`chop_filter_enabled`, current true)
Raises entry threshold in choppy conditions (high price range, no net direction).
- `chop_threshold` (current 6.0) — chop index above this triggers boost
- `chop_max_boost` (current 0.05) — max threshold increase in extreme chop
- `chop_scale` (current 8.0) — chop index at which max_boost fully applies
**Raise chop_threshold** when: chop filter too aggressive in ranging markets
**Lower chop_threshold** when: choppy entries still getting through and losing
**Note**: Chop filter also affects EXIT threshold in same-direction chop. This can prevent premature exits during noisy but favorable windows.

---

## Risk Management / Exits

### `trailing_stop_pct` (current 0.18)
Exit when price drops X% from high water mark.
**Raise** when: trailing stop firing on winners that would have recovered
**Lower** when: winners giving back too much from peak before stop fires
**Nudge**: +/-0.02.

### `trailing_stop_min_profit_pct` (current 0.12)
Trailing stop only arms after price is X% above entry.
**Raise** when: trailing stop arming too early, stopping out on normal dips
**Lower** when: not protecting enough gains on small winners
**Nudge**: +/-0.02.

### `trailing_stop_late_pct` (current 0.15) / `trailing_stop_late_seconds` (current 90.0)
Tighter trailing stop in the last N seconds of window.
**Nudge**: pct +/-0.02, seconds +/-15.

### `take_profit_price` (current 0.90)
Exit immediately when token price hits this.
**Lower** when: price touching 0.80-0.85 but rarely reaching 0.90
**Raise** when: consistently hitting 0.90 and price would have gone higher
**Nudge**: +/-0.02-0.05.

### `max_loss_pct` (current 0.30)
Hard stop: exit if token drops X% from entry regardless of anything else.
**Raise** when: max_loss triggering on trades that would have recovered (check post-exit price)
**Lower** when: losses still too large before max_loss fires
**Nudge**: +/-0.05. Never above 0.40 (that's a $2+ loss on $5 trades).

---

## Timing

### `min_seconds_remaining` (current 300)
Don't enter with fewer than this many seconds left in window.
**Raise** when: late entries are consistent losers
**Lower** when: missing good late-window signals
**Nudge**: +/-30-60s.

### `trade_cooldown` (current 30.0s)
Seconds between trades on same market.
**Raise** when: seeing buy-sell-rebuy at worse prices
**Lower** when: cooldown blocking profitable re-entries after clean exits
**Nudge**: +/-5-10s.

### `window_hop_cooldown` (current 30.0s)
Seconds after window hop before trading.
**Raise** when: first trade after window hop is consistently losing
**Lower** when: missing the opening move on fresh windows
**Nudge**: +/-5-10s. Depth feed is continuous so this can be shorter than with aggTrade.

---

## Execution

### `exit_slippage` (current 0.05)
Cents below market floor for FOK exit.
**Raise** when: 3+ FOK rejection attempts on same exit in DO logs
**Lower** when: consistently exiting far below mid
**Nudge**: +/-0.01.

### `entry_slippage` (current 0.04)
Base slippage above market for first FOK entry.
**Raise** when: first-attempt FOK rejections common
**Lower** when: consistently overpaying on entry
**Nudge**: +/-0.01.

### `fixed_position_usd` (current 5.0)
Fixed dollar amount per trade. 0 = use Kelly sizing.
**Note**: Scale up only after proving consistent edge over multiple sessions.

---

## Legacy / Inactive (aggTrade-era, currently disabled)

These settings exist but are NOT active with `depth_aggtrade_weight=0.0`:
- `weight_ofi_5s`, `weight_ofi_15s`, `weight_vwap_drift`, `weight_intensity` — aggTrade momentum weights
- `dampener_agree_factor`, `dampener_disagree_factor`, `dampener_flat_factor`, `dampener_price_deadzone` — flow-price dampener
- `vwap_drift_scale` — VWAP normalization
- `low_vol_block_enabled` (false) — designed for aggTrade intensity
- `high_intensity_block_enabled` (false) — designed for aggTrade TPS
- `acceleration_enabled` (false) — designed for aggTrade momentum acceleration
- `adaptive_bias_enabled` (false) — macro trend bias on entry thresholds

**Do NOT tune these unless re-enabling aggTrade signal blending.**

---

## Structural Changes (always FLAG, never auto-apply)

- `enable_flips` — enables/disables flip logic entirely
- `poly_book_enabled` — enables Polymarket order book integration
- `trend_bias_enabled` — enables/disables 5-min trend block
- `depth_signal_weight` / `depth_aggtrade_weight` — signal source blend
- `depth_enabled` — master toggle for depth feed
- Any depth weight change >0.05
- Any threshold change >0.10 from current
- `fixed_position_usd` or `max_position_per_trade` changes
- `max_trades_per_window` changes
