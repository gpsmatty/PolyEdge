# Micro Sniper Config Tuning Playbook

This is the "if you see X, adjust Y" reference for the `/tune` command. Each section maps a data observation to a specific config key, direction, and safe nudge range.

---

## Entry Thresholds

### `entry_threshold` (default 0.50)
**Raise** when: too many losing trades, false signals in chop, losers with low MFE (signal never had conviction)
**Lower** when: missing too many good moves, win rate is high but trade count is very low, large favorable MFE on blocked signals
**Nudge**: ±0.02–0.05. Never go below 0.45 or above 0.70.

### `counter_trend_threshold` (default 0.55)
**Raise** when: counter-trend (against 30s OFI) trades are underperforming, 30s trend alignment shows clear edge
**Lower** when: counter-trend trades are actually outperforming with-trend trades (currently common — the 30s OFI is often contrarian)
**Note**: Counter-trend outperforming with-trend is normal in current data. If with-trend 75% WR, raise counter to match. If both bad, entry_threshold is the problem.
**Nudge**: ±0.02–0.05. Keep above entry_threshold.

### `exit_threshold` (default 0.20)
**Raise** when: exiting winners too early on noise, trades exit quickly then price continues in our direction
**Lower** when: losers not being cut fast enough, large negative avg P&L on momentum-reversal exits
**Nudge**: ±0.02–0.05. Never below 0.15 (won't exit fast enough on real reversals).

### `flip_threshold` (default 0.50)
**Raise** when: flip entries are underperforming (too many marginal flips), flip win rate < 40%
**Lower** when: missing strong reversal opportunities
**Only relevant if enable_flips=true.**

---

## Timing & Cooldowns

### `min_seconds_remaining` (default varies, currently 400 via DB)
**Raise** when: trades with <N seconds remaining are consistent losers (check time-remaining buckets in trade_analysis)
**Lower** when: late entries are actually fine and we're missing the end-of-window volatility
**Note**: Was 120 (default), raised to 400 after data showed trades entering <400s remaining were net negative. This is a high-impact setting — each 60s change excludes/includes significant trade volume.
**Nudge**: ±30–60s. Watch trade_count impact carefully.

### `trade_cooldown` (default 30.0s)
**Raise** when: seeing buy-sell-rebuy patterns at worse prices (whipsaw), multiple trades on same market in <60s
**Lower** when: cooldown is blocking profitable re-entries after clean exits
**Nudge**: ±5–10s.

### `force_exit_seconds` (default 8.0s)
**Raise** when: force exits are consistent losers (stuck at bad prices, market illiquid at expiry)
**Lower** when: exits with 8-15s left are leaving money on table (price continues favorably after force exit)
**Nudge**: ±2s.

### `flip_min_hold_seconds` (default 45.0s)
**Raise** when: flip exits are happening <45s post-flip and losing (protection not holding long enough)
**Lower** when: flips are being held too long against clear reversals
**Nudge**: ±10s. Don't go below 20s.

---

## Blocker Tuning

### `high_intensity_max_tps` (default 50.0)
**Raise** when: blocking too many trades, high-intensity trades are actually profitable, dead zone is above 50 tps
**Lower** when: losers cluster in a tps bucket that's currently allowed through
**Key data**: Check intensity bucket breakdown. Find the tps level where win rate collapses. Set `high_intensity_max_tps` to the bottom of that bucket.
**Current note**: Data showed 40-50 tps is profitable, so avoid blocking below 50. Losers avg ~61 tps, winners ~40 tps.
**Nudge**: ±3–5 tps.

### `low_vol_max_intensity` (default 5.0 tps)
**Raise** when: low-vol block fires rarely but low-vol regime still has bad win rate (regime classifier missing slow markets)
**Lower** when: low-vol block fires constantly and is blocking decent trades
**Nudge**: ±1–2 tps.

### `low_vol_max_price_change` (default 0.0005)
**Raise** when: missing trades in slow but valid market conditions
**Lower** when: low-vol block isn't catching flat markets
**Nudge**: ±0.0001.

### `chop_threshold` (default 3.0) — chop index (range/net_move ratio)
**Raise** when: chop filter too aggressive, blocking good directional trades (high range but real momentum)
**Lower** when: choppy markets still getting through (many entries, low win rate, price oscillating)
**Nudge**: ±0.3–0.5.

### `chop_max_boost` (default 0.10)
**Raise** when: chop filter boost isn't strong enough to block bad chop signals (chop regime win rate still poor)
**Lower** when: chop regime has acceptable win rate but threshold boost is making it miss good entries
**Nudge**: ±0.02–0.05.

### `acceleration_tolerance` (default 0.05)
**Raise** (more tolerant) when: acceleration filter too strict, blocking signals that plateau but are still valid
**Lower** (stricter) when: fading momentum signals are still getting through and losing
**Note**: 0.05 = strict (must be accelerating), 0.15 = loose (allows plateau)
**Nudge**: ±0.02.

---

## Entry Filters

### `dead_market_band` (default 0.02)
**Raise** when: trades near 0.50 YES price are consistently losing (market not reacting to BTC moves)
**Lower** when: some 0.48–0.52 entries are profitable and we're blocking them
**Nudge**: ±0.005.

### `min_entry_price` (default 0.35) / `max_entry_price` (default 0.65)
**Tighten** when: deep OTM/ITM entries are consistently losing
**Widen** when: good signals appearing outside the band
**Nudge**: ±0.02–0.05.

### `min_trades_in_window` (default 10)
**Raise** when: sparse-data entries (barely 10 trades) are losing — OFI unreliable with few data points
**Lower** when: valid signals being blocked at low-volume times
**Nudge**: ±2.

### `entry_persistence_seconds` (default 2.0)
**Raise** when: sub-second spikes getting through (flash signals that immediately reverse)
**Lower** when: valid slow-building momentum being filtered out
**Nudge**: ±0.5s.

---

## Trailing Stop

### `trailing_stop_pct` (default 0.12)
**Raise** when: trailing stop firing on winners that would have recovered (exiting at $0.58 when final price is $0.80)
**Lower** when: winners giving back too much from high water mark before stop triggers
**Note**: Breakeven protection is EXPERIMENTAL. Monitor if it reduces P&L on legitimate drawdown recoveries.
**Nudge**: ±0.02.

### `trailing_stop_min_profit_pct` (default 0.10)
**Raise** when: trailing stop arming too early (price barely up 10% then stops out on normal noise)
**Lower** when: not protecting enough gains on small winners
**Nudge**: ±0.02.

### `take_profit_price` (default 0.90)
**Lower** when: price is touching 0.85+ but never reaching 0.90, leaving gains on table
**Raise** when: take-profit exits are capturing the top but price was going higher (rare)
**Note**: Currently catching good exits at 0.90. Would lower to 0.85 if consistent data shows 0.90 being missed.
**Nudge**: ±0.02–0.05.

### `max_loss_pct` (default 0.35, currently 0.45) ⚠️ NEW — under evaluation
Exits position if token drops X% from entry price, regardless of momentum or time remaining.
Fills the gap that trailing stop doesn't cover: trailing stop only arms after min_profit_pct gain.
Without this, a trade that immediately goes wrong rides all the way to window close (floor_exit).
**Raise** when: max_loss is triggering on trades that would have recovered (check post-exit MFE)
**Lower** when: losses are still too large before max_loss fires, or a -$4+ loss occurs again
**Set to 0** to disable entirely.
**Do not tune aggressively** — new feature, needs a full session of data before conclusions.
**Nudge**: ±0.05.

---

## Adaptive Bias

### `adaptive_bias_min_move` (default 0.003 = 0.30%)
**Raise** when: UNFAVORABLE trades are net negative (bias is firing on noise, punishing trades that would have been fine). Current data showed UNFAVORABLE net -$2.30.
**Lower** when: big macro moves aren't being caught by bias (trend continuing past 0.30% before bias kicks in)
**Nudge**: ±0.001.

### `adaptive_bias_spread` (default 0.10)
**Raise** when: bias adjustment isn't impacting trade selection enough (FAVORABLE and UNFAVORABLE performing similarly)
**Lower** when: bias spread is blocking too many UNFAVORABLE entries that are actually profitable
**Nudge**: ±0.02.

---

## Signal Weights (weights must sum to ~1.0)

### `weight_ofi_15s` (default 0.50)
Primary signal. **Raise** when: 15s OFI is reliably predicting direction. **Lower** when: it's generating noise.

### `weight_vwap_drift` (default 0.25)
Price movement weighted by volume. **Raise** when: price confirmation is key to accuracy. **Lower** when: VWAP is lagging and adding noise.

### `weight_intensity` (default 0.15)
Trade rate spike. **Raise** when: intensity spikes reliably precede moves. **Lower** when: high intensity = noise (chaotic book).

### `weight_ofi_5s` (default 0.10)
Short burst, noisy. Keep low. Only raise if 5s OFI is highly predictive in current market.

### `vwap_drift_scale` (default 2000.0)
Normalizes BTC dollar moves to [-1,1]. At 2000, ~$35 BTC move maxes drift signal.
**Raise** when: VWAP drift component is saturating (maxing out at ±1.0 on normal moves)
**Lower** when: VWAP drift component is too weak (small moves not registering)
**Note**: Was 5000 (too sensitive, $14 maxed it). 2000 is well-calibrated for current BTC volatility.

---

## Dampener

### `dampener_disagree_factor` (default 0.40)
Heavy penalty when OFI and price move in opposite directions (flow absorbed without price displacement).
**Raise** (less penalty) when: dampener is blocking too many valid signals where price lags OFI
**Lower** (more penalty) when: signals passing dampener despite price opposing flow still losing
**Nudge**: ±0.05.

### `dampener_flat_factor` (default 0.65)
Moderate penalty when price flat despite OFI.
**Raise** when: flat-price entries are profitable (OFI precedes price move)
**Lower** when: flat-price entries are consistent losers
**Nudge**: ±0.05.

### `dampener_price_deadzone` (default 0.05)
Below this abs(drift_signal) = "flat price."
**Raise** when: small price moves are being treated as "agree" when they're really noise
**Lower** when: valid small price moves are being treated as flat
**Nudge**: ±0.01.

---

## Execution

### `exit_slippage` (default 0.05)
5 cents below market floor for FOK exit. Was 0.02 — caused repeated rejections.
**Raise** when: seeing 3+ FOK rejection attempts on same exit in DO logs
**Lower** when: consistently exiting far below mid (paying too much slippage)
**Nudge**: ±0.01.

### `entry_slippage` (default 0.02, currently 0.04)
Base slippage above market for first FOK entry attempt.
**Raise** when: first-attempt FOK rejections still common after retry escalation
**Lower** when: consistently overpaying on entry (fill price far above signal price)
**Nudge**: ±0.01.

### `entry_slippage_retry_step` (default 0.02) ⚠️ NEW — under evaluation
Added to slippage on each FOK retry. Attempt 1=base, attempt 2=base+step, attempt 3=base+2×step, then resets. Motivation: strong OFI signals (0.99) were getting FOK'd on thin books during fast BTC moves, then the 30s cooldown locked out the entire move. Now retries with escalating slippage, signal threshold is the only gate.
**Raise** when: still missing fills after 3 retries on strong signals
**Lower** when: overpaying on retries, entering at bad prices
**Set to 0** to disable retry escalation entirely.
**Do not tune aggressively** — this feature is new and needs a full session of data before drawing conclusions.

### `entry_slippage_max` (default 0.10)
Hard cap on entry slippage across all retries. Prevents runaway slippage on repeated rejections.
**Raise** when: retries hitting the cap but signal is still strong and book has repriced to a level worth entering
**Lower** when: paying too much on retried entries
**Nudge**: ±0.02.

---

## Structural Changes (always FLAG, never auto-apply)

- `enable_flips` — enables/disables flip logic entirely
- `poly_book_enabled` — enables Polymarket order book integration
- `trend_bias_enabled` — enables/disables 5-min trend block
- `adaptive_bias_enabled` — enables/disables 30-min macro bias
- `chop_filter_enabled` — enables/disables chop detection
- `high_intensity_block_enabled` — enables/disables intensity cap
- Any weight change >0.05 from current
- Any threshold change >0.10 from current
- `fixed_position_usd` or `max_position_per_trade` changes
- `max_trades_per_window` changes
