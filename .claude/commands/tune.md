---
allowed-tools: Bash(*), Read(*), Write(*), Edit(*)
description: Autonomous tuning agent for the PolyEdge micro sniper. Analyzes recent trade performance from the DB and DO logs, then recommends or auto-applies config changes. Use /tune or /tune --hours 8 to specify a data window.
---

# PolyEdge Tuning Agent

You are an autonomous performance analyst and config tuner for the PolyEdge micro sniper trading bot. Your job is to look at recent data, identify what's working and what isn't, then make targeted config adjustments with data-backed reasoning.

## Context

Current working directory: /Users/gpsmatty/production/PolyEdge

Arguments passed: $ARGUMENTS (use as --hours N if provided, default 4)

Current config:
!`.venv/bin/polyedge config show 2>/dev/null | head -60`

Recent trade performance:
!`.venv/bin/python scripts/trade_analysis.py --hours 4 --json 2>/dev/null || .venv/bin/python scripts/trade_analysis.py --hours 4 2>/dev/null | tail -80`

MFE/MAE by regime and entry quality (labeled snapshots, last 4h):
!`DB_URL=$(security find-generic-password -s polyedge -a database_url -w 2>/dev/null); [ -n "$DB_URL" ] && psql "$DB_URL" -c "SELECT (features->>'regime')::text as regime, COUNT(*) as n, ROUND(AVG((features->>'mfe_30s')::float)::numeric, 4) as avg_mfe, ROUND(AVG((features->>'mae_30s')::float)::numeric, 4) as avg_mae FROM polyedge.signal_snapshots WHERE ts > NOW() - INTERVAL '4 hours' AND features->>'mfe_30s' IS NOT NULL GROUP BY regime ORDER BY n DESC;" 2>/dev/null || echo "MFE/MAE query unavailable"`

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
- Overall win rate and avg P&L vs historical context
- Is this better/worse than the 40-50% target?

**Entry quality**
- High-momentum entries (>0.65): win rate and avg P&L. High momentum should be better — if not, signal quality is degraded.
- Counter-trend vs with-trend: which side is carrying performance? (Counter-trend outperforming is normal but requires monitoring)
- Entry timing: trades with >400s remaining vs <400s. Late entries should underperform significantly.

**Exit quality**
- Trailing stop exits: win rate (should be high — these caught winners). If trailing stop is triggering on losers, min_profit_pct may be too low.
- Momentum reversal exits: avg P&L. Large negative avg = exiting too late on losers.
- Force exits (<8s): if frequent, min_seconds_remaining may need raising.
- Take-profit exits: if these are a large fraction, great. If absent, take_profit_price may be too high.

**Blockers & regime**
- Which blocker fires most: low_vol_block, high_intensity_block, chop_filter, trend_bias, acceleration?
- High blocker rate (>60% of evaluations blocked) with poor win rate = filters working correctly.
- Low blocker rate with poor win rate = filters not catching bad signals.
- What regime do winners come from vs losers?

**Adaptive bias**
- FAVORABLE vs UNFAVORABLE vs NEUTRAL: if UNFAVORABLE trades are net negative, consider raising adaptive_bias_min_move.
- NEUTRAL performing well = bias is irrelevant noise.

**Trade intensity buckets**
- Find the "dead zone" TPS range where win rate collapses. Set high_intensity_max_tps to block below that.

**Flip performance** (if enable_flips=true)
- Flip win rate vs normal win rate. If flips are significantly worse, consider disabling again.
- Any exits within flip_min_hold_seconds (sign flip_hold protection isn't working)?

**DO logs**
- Look for: repeated FOK rejections (exit_slippage too low), WebSocket disconnects (gap detection), config reload messages, ERROR patterns, stuck exit loops (same market selling 5+ times).

### 4. Bug detection (before tuning)

Before making config recommendations, check whether the data patterns are explainable by config alone or suggest a code bug. Tuning config on top of a bug makes things worse, not better.

**Red flags that indicate a bug, not a config issue:**
- A filter is enabled in config but the DO logs show it never firing (or always firing regardless of data)
- Exit reason distribution is impossible (e.g., 0% trailing stop exits when trailing_stop_enabled=true and trades are hitting >10% gains)
- Flip protection not working: exits appearing in logs within `flip_min_hold_seconds` of a flip entry
- OFI values flat or constant (feed might be stale, gap detection might not be resetting properly)
- Entry persistence firing but trades still entering on sub-second signals
- Config reloads in DO logs showing wrong values vs what `polyedge config show` reports
- Win rate perfectly 0% or 100% (almost always a logic error, not market conditions)

**If a bug is suspected:**
- Do NOT apply config changes that would mask the bug (e.g., raising a threshold to avoid the bad behavior)
- Read the relevant source file(s) to diagnose: `src/polyedge/strategies/micro_sniper.py` (strategy logic), `src/polyedge/strategies/micro_runner.py` (runner/state management)
- Flag it clearly in the report: **BUG SUSPECTED: [description] — [evidence from data/logs] — [files to check]**
- Suggest the specific code section to review, but do not apply code changes in tune — that requires the user's attention

### 5. Identify config changes (use playbook)

Categorize each potential change. Use `.claude/commands/tune-refs/config-playbook.md` to map observations to keys and safe nudge ranges. Do not recommend changes not grounded in the playbook or clear data evidence.

**AUTO-APPLY** (small parameter nudges, clearly data-backed, ±10-20% of current value, reversible):
- Threshold adjustments based on clear bucket performance data
- Timeout/cooldown tweaks based on log patterns
- Blocker parameter tightening based on dead zones

**FLAG FOR APPROVAL** (structural or larger changes):
- Enabling/disabling entire features (flips, poly book, etc.)
- Changes >30% from current value
- Anything that could fundamentally change trading behavior

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

Keep findings specific and quantitative. Avoid vague statements like "performance seems weak." Say "win rate 31% on with-trend entries vs 75% counter-trend — 30s OFI trend filter is inverting expected direction edge."

After the report, if there are flagged changes, ask: "Apply any of the flagged changes? (list numbers or 'all')"
