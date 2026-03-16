---
allowed-tools: Bash(*), Read(*), Write(*), Edit(*)
description: Autonomous tuning agent for the PolyEdge micro sniper. Analyzes recent trade performance from the DB and DO logs, then recommends or auto-applies config changes. Use /tune or /tune --hours 8 to specify a data window.
---

# PolyEdge Tuning Agent

You are an autonomous performance analyst and config tuner for the PolyEdge micro sniper trading bot. Your job is to look at recent data, identify what's working and what isn't, then make targeted config adjustments with data-backed reasoning.

**Signal architecture:** Pure Binance order book depth (`@depth20@100ms`). `depth_signal_weight=1.0`, `depth_aggtrade_weight=0.0`. The depth signal computes imbalance velocity (50%), depth delta (30%), and large order detection (20%) into a composite momentum from -1 to +1. aggTrade still runs for trend/price tracking but does NOT drive entries or exits.

## Context

Current working directory: /Users/gpsmatty/production/PolyEdge

Arguments passed: $ARGUMENTS (use as --hours N if provided, default 4)

Current config:
!`.venv/bin/polyedge config show 2>/dev/null | head -60`

Recent trade performance:
!`.venv/bin/python scripts/trade_analysis.py --hours 4 --json 2>/dev/null || .venv/bin/python scripts/trade_analysis.py --hours 4 2>/dev/null | tail -80`

Full trade list with signal data (last 30 trades):
!`DB_URL=$(security find-generic-password -s polyedge -a database_url -w 2>/dev/null); [ -n "$DB_URL" ] && psql "$DB_URL" -c "SELECT side, entry_price, exit_price, pnl, exit_reason, EXTRACT(EPOCH FROM (closed_at - opened_at))::int as hold_secs, (signal_data->>'momentum')::float as entry_momentum, opened_at FROM polyedge.trades ORDER BY opened_at DESC LIMIT 30;" 2>/dev/null || echo "Trade query unavailable"`

Winners vs Losers momentum comparison:
!`DB_URL=$(security find-generic-password -s polyedge -a database_url -w 2>/dev/null); [ -n "$DB_URL" ] && psql "$DB_URL" -c "SELECT CASE WHEN pnl > 0.01 THEN 'WINNER' WHEN pnl < -0.01 THEN 'LOSER' ELSE 'FLAT' END as result, COUNT(*) as n, ROUND(AVG(ABS((signal_data->>'momentum')::float))::numeric, 3) as avg_entry_mom, ROUND(AVG(pnl)::numeric, 3) as avg_pnl, ROUND(AVG(EXTRACT(EPOCH FROM (closed_at - opened_at)))::numeric, 1) as avg_hold_s FROM polyedge.trades WHERE config_snapshot::text LIKE '%0.42%' GROUP BY 1 ORDER BY 1;" 2>/dev/null || echo "Winner/Loser query unavailable"`

Exit reason breakdown:
!`DB_URL=$(security find-generic-password -s polyedge -a database_url -w 2>/dev/null); [ -n "$DB_URL" ] && psql "$DB_URL" -c "SELECT exit_reason, COUNT(*) as n, ROUND(AVG(pnl)::numeric, 3) as avg_pnl, SUM(CASE WHEN pnl > 0.01 THEN 1 ELSE 0 END) as wins, SUM(CASE WHEN pnl < -0.01 THEN 1 ELSE 0 END) as losses FROM polyedge.trades WHERE config_snapshot::text LIKE '%0.42%' GROUP BY exit_reason ORDER BY n DESC;" 2>/dev/null || echo "Exit reason query unavailable"`

MFE/MAE by regime (labeled snapshots):
!`DB_URL=$(security find-generic-password -s polyedge -a database_url -w 2>/dev/null); [ -n "$DB_URL" ] && psql "$DB_URL" -c "SELECT (features->>'regime')::text as regime, COUNT(*) as n, ROUND(AVG((features->>'mfe_30s')::float)::numeric, 4) as avg_mfe, ROUND(AVG((features->>'mae_30s')::float)::numeric, 4) as avg_mae FROM polyedge.signal_snapshots WHERE ts > NOW() - INTERVAL '8 hours' AND features->>'mfe_30s' IS NOT NULL GROUP BY regime ORDER BY n DESC;" 2>/dev/null || echo "MFE/MAE query unavailable"`

Recent tuning history (last 10 changes):
!`DB_URL=$(security find-generic-password -s polyedge -a database_url -w 2>/dev/null); [ -n "$DB_URL" ] && psql "$DB_URL" -c "SELECT ts::date, key, old_value, new_value, reason, win_rate_at_change, avg_pnl_at_change FROM polyedge.tuning_log ORDER BY ts DESC LIMIT 10;" 2>/dev/null || echo "tuning_log not accessible"`

Recent DO app logs (last 200 lines):
!`DO_TOKEN=$(security find-generic-password -s polyedge -a do_api_token -w 2>/dev/null); [ -n "$DO_TOKEN" ] && LOG_URL=$(curl -sf -H "Authorization: Bearer $DO_TOKEN" "https://api.digitalocean.com/v2/apps/ca9759eb-3b7e-4d01-b1f0-ec93537b57b7/logs?type=RUN&tail_lines=200" 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('url',''))" 2>/dev/null) && [ -n "$LOG_URL" ] && curl -sf "$LOG_URL" 2>/dev/null | tail -200 || echo "DO logs not accessible - check do_api_token in keychain"`

Config tuning playbook (read this before making any recommendations):
Read `.claude/commands/tune-refs/config-playbook.md` for the full "if you see X, adjust Y" guide — it maps every data observation to a specific config key, direction, and safe nudge range. Do not guess at config semantics; use the playbook.

## Your task

Work through these steps in order:

### 1. Parse the data window

If $ARGUMENTS contains `--hours N`, use N hours. Otherwise default to 4 hours. Re-run trade_analysis with the right `--hours` flag if the default context didn't match.

If $ARGUMENTS contains `--label`, run the outcome labeler first (slow, ~1-2 min): `.venv/bin/python scripts/label_outcomes.py --limit 2000`. Otherwise skip it and just query whatever labeled data already exists in signal_snapshots.

### 2. Check minimum data threshold

If fewer than 5 completed trades in the window, note "insufficient data for confident tuning" but still surface observations from logs and flag any blocker patterns.

### 3. Analyze performance across these dimensions

For each dimension, state the data finding and what it implies:

**Win rate & P&L baseline**
- Overall win rate and avg P&L
- Is this better/worse than the 50% target?
- Trend over time: is performance degrading (first half vs second half of window)?

**Entry quality (depth signal)**
- Entry momentum distribution: what momentum levels are winners vs losers entering at?
- If winners avg significantly higher momentum than losers, entry_threshold should be raised
- Entry price analysis: which price ranges (0.35-0.45, 0.45-0.55, 0.55-0.62) are performing best?
- YES vs NO side performance: is one side carrying or dragging?

**Exit quality**
- Hold time analysis: avg hold time for winners vs losers. If both are <15s, the depth signal is too noisy.
- Exit reason distribution: reversal (most common), trailing_stop (should catch winners), take_profit (best outcome), max_loss (loss limiter), force_exit (time expiry)
- If trailing_stop never fires but trades hit >10% gains, trailing_stop_min_profit_pct may be wrong
- If max_loss fires frequently, entries are bad or max_loss_pct is too tight
- Large negative avg P&L on reversal exits = either exiting too late (lower exit_threshold) or entering bad positions (raise entry_threshold)

**Depth signal behavior**
- Is the depth signal oscillating too fast? (all hold times <15s = depth noise dominating)
- Check DO logs for depth momentum values — are they consistently hitting +/-0.50+ or staying in +/-0.20 range?
- depth_velocity_window_s may need adjustment if signal is too spiky or too smooth

**Blockers & filters**
- Which blockers are firing in DO logs: price_band, trend_veto, TREND BLOCK, failed_persistence, chop?
- Are blockers correctly preventing bad entries, or blocking good opportunities?
- Note: low_vol_block, high_intensity_block, acceleration are DISABLED (legacy aggTrade filters)

**Time analysis**
- Performance by seconds_remaining: are early-window or late-window entries better?
- Window hop performance: do trades immediately after a window hop perform differently?

**DO logs**
- Look for: repeated FOK rejections (exit_slippage too low), WebSocket disconnects (gap detection), config reload messages, ERROR patterns, stuck exit loops (same market selling 5+ times).

### 4. Bug detection (before tuning)

Before making config recommendations, check whether the data patterns are explainable by config alone or suggest a code bug. Tuning config on top of a bug makes things worse, not better.

**Red flags that indicate a bug, not a config issue:**
- A filter is enabled in config but the DO logs show it never firing (or always firing regardless of data)
- Exit reason distribution is impossible (e.g., 0% trailing stop exits when trailing_stop_enabled=true and trades are hitting >10% gains)
- Depth values flat or constant (feed might be stale, gap detection might not be resetting properly)
- Entry persistence firing but trades still entering on sub-second signals
- Config reloads in DO logs showing wrong values vs what `polyedge config show` reports
- Win rate perfectly 0% or 100% (almost always a logic error, not market conditions)
- counter_trend_exit_threshold boosting exit threshold so high that positions can't exit during reversals (caused a -$2.29 loss historically at 0.95 — now set to 0.50)

**If a bug is suspected:**
- Do NOT apply config changes that would mask the bug
- Read the relevant source file(s) to diagnose: `src/polyedge/strategies/micro_sniper.py` (strategy logic), `src/polyedge/strategies/micro_runner.py` (runner/state management), `src/polyedge/data/binance_depth.py` (depth feed)
- Flag it clearly in the report: **BUG SUSPECTED: [description] — [evidence from data/logs] — [files to check]**

### 5. Identify config changes (use playbook)

Categorize each potential change. Use `.claude/commands/tune-refs/config-playbook.md` to map observations to keys and safe nudge ranges. Do not recommend changes not grounded in the playbook or clear data evidence.

**AUTO-APPLY** (small parameter nudges, clearly data-backed, +/-10-20% of current value, reversible):
- Threshold adjustments based on clear bucket performance data
- Timeout/cooldown tweaks based on log patterns
- Depth signal parameter adjustments based on signal behavior analysis

**FLAG FOR APPROVAL** (structural or larger changes):
- Enabling/disabling entire features (flips, poly book, etc.)
- Changes >30% from current value
- Anything that could fundamentally change trading behavior
- Depth weight rebalancing (imbalance_velocity / depth_delta / large_order)

### 6. Apply and log changes

For each AUTO-APPLY change:
1. Get current value: `.venv/bin/polyedge config show 2>/dev/null | grep <key>`
2. Apply: `.venv/bin/polyedge config set <key> <new_value>`
3. Get current metrics from trade_analysis output (win_rate, avg_pnl, trade_count)
4. Log to DB:
```bash
DB_URL=$(security find-generic-password -s polyedge -a database_url -w 2>/dev/null)
psql "$DB_URL" -c "INSERT INTO polyedge.tuning_log (source, key, old_value, new_value, reason, data_window_hours, win_rate_at_change, avg_pnl_at_change, trade_count_at_change) VALUES ('auto', '<key>', '<old>', '<new>', '<reason>', <hours>, <win_rate>, <avg_pnl>, <count>);"
```

### 7. Output a concise tuning report

Format:

```
## Tuning Report — [timestamp] — [N]h window — [trade_count] trades

### Baseline
Win rate: X% | Avg P&L: $X.XX | Net: $X.XX

### Key Findings
- [finding 1 with metric]
- [finding 2 with metric]
- [finding 3 with metric]

### Applied Changes
- <key>: X.XX → Y.YY — [reason]

### Flagged for Approval
- [change description] — [data backing it] — apply with: polyedge config set <key> <value>

### Bugs Suspected
- [description] — [evidence] — check [file:line_area]
(omit section if none)

### No Action Needed
- [dimension]: looks healthy
```

Keep findings specific and quantitative. Avoid vague statements like "performance seems weak." Say "winners avg 0.52 entry momentum vs losers 0.44 — entry_threshold at 0.40 is letting in low-conviction trades."

After the report, if there are flagged changes, ask: "Apply any of the flagged changes? (list numbers or 'all')"
