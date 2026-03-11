#!/usr/bin/env python3
"""Quick overnight trade analysis — run from your PolyEdge venv."""
import asyncio
import asyncpg
import ssl

async def main():
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    # Load DB URL from keychain
    import subprocess
    result = subprocess.run(
        ["security", "find-generic-password", "-s", "polyedge", "-a", "database_url", "-w"],
        capture_output=True, text=True, timeout=5
    )
    db_url = result.stdout.strip()

    conn = await asyncpg.connect(db_url, ssl=ssl_ctx)

    rows = await conn.fetch("""
        SELECT
          opened_at AT TIME ZONE 'America/New_York' as time_et,
          side,
          entry_price,
          exit_price,
          size,
          pnl,
          status,
          reasoning
        FROM polyedge.trades
        WHERE strategy = 'micro_sniper'
          AND opened_at > NOW() - INTERVAL '12 hours'
        ORDER BY opened_at ASC;
    """)

    print(f"\n{'='*80}")
    print(f"  MICRO SNIPER OVERNIGHT ANALYSIS — {len(rows)} trades")
    print(f"{'='*80}\n")

    print(f"{'#':<4} {'Time ET':<20} {'Side':<5} {'Entry':>6} {'Exit':>6} {'Size':>7} {'P&L':>8} {'Status':<7}")
    print("-" * 75)

    total_pnl = 0
    wins = 0
    losses = 0
    breakeven = 0
    win_amts = []
    loss_amts = []
    bought_high_sold_low = 0
    bought_low_sold_high = 0

    for i, r in enumerate(rows):
        pnl = float(r['pnl']) if r['pnl'] else 0
        entry = float(r['entry_price'])
        exit_p = float(r['exit_price']) if r['exit_price'] else 0
        size = float(r['size'])
        status = r['status'] or ''
        total_pnl += pnl

        if pnl > 0.01:
            wins += 1
            win_amts.append(pnl)
        elif pnl < -0.01:
            losses += 1
            loss_amts.append(pnl)
        else:
            breakeven += 1

        if entry > 0.60 and exit_p > 0 and exit_p < entry:
            bought_high_sold_low += 1
        if entry < 0.50 and exit_p > entry:
            bought_low_sold_high += 1

        pnl_str = f"${pnl:+.2f}" if r['exit_price'] else "OPEN"
        marker = "✓" if pnl > 0.01 else "✗" if pnl < -0.01 else "~"
        print(f"{i+1:<4} {str(r['time_et'])[:19]:<20} {r['side']:<5} ${entry:.3f}  ${exit_p:.3f}  {size:>6.1f}  {pnl_str:>8} {status:<7} {marker}")

    print("-" * 75)
    total = wins + losses + breakeven
    if total > 0:
        print(f"\nWin/Loss/BE: {wins}W / {losses}L / {breakeven}BE ({wins/total*100:.0f}% win rate)")
    print(f"Total P&L: ${total_pnl:+.2f}")

    if win_amts:
        print(f"Avg win:   ${sum(win_amts)/len(win_amts):+.2f}  (best: ${max(win_amts):+.2f})")
    if loss_amts:
        print(f"Avg loss:  ${sum(loss_amts)/len(loss_amts):+.2f}  (worst: ${min(loss_amts):+.2f})")

    if win_amts and loss_amts:
        avg_win = sum(win_amts)/len(win_amts)
        avg_loss = abs(sum(loss_amts)/len(loss_amts))
        print(f"Win/loss ratio: {avg_win/avg_loss:.2f}x")

    print(f"\nPatterns:")
    print(f"  Bought high (>$0.60) sold lower: {bought_high_sold_low} trades")
    print(f"  Bought low (<$0.50) sold higher: {bought_low_sold_high} trades")

    # P&L by hour
    hour_rows = await conn.fetch("""
        SELECT
          date_trunc('hour', opened_at AT TIME ZONE 'America/New_York') as hour_et,
          COUNT(*) as trades,
          SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)::int as wins,
          SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END)::int as losses,
          SUM(pnl) as total_pnl
        FROM polyedge.trades
        WHERE strategy = 'micro_sniper'
          AND opened_at > NOW() - INTERVAL '12 hours'
          AND exit_price IS NOT NULL
        GROUP BY 1
        ORDER BY 1;
    """)

    print(f"\n--- P&L by Hour ---")
    for r in hour_rows:
        pnl = float(r['total_pnl'])
        bar = "█" * min(20, int(abs(pnl) / 0.5))
        sign = "+" if pnl >= 0 else "-"
        print(f"  {str(r['hour_et'])[11:16]} ET: {r['trades']:>3} trades, {r['wins']}W/{r['losses']}L, ${pnl:>+7.2f}  {sign}{bar}")

    # Entry price distribution
    print(f"\n--- Entry Price Distribution (where are we making/losing money?) ---")
    buckets = [
        ("$0.20-0.35", 0.20, 0.35),
        ("$0.35-0.50", 0.35, 0.50),
        ("$0.50-0.65", 0.50, 0.65),
        ("$0.65-0.80", 0.65, 0.80),
    ]
    for label, lo, hi in buckets:
        bucket_rows = [r for r in rows if lo <= float(r['entry_price']) < hi and r['exit_price']]
        if not bucket_rows:
            continue
        count = len(bucket_rows)
        pnl = sum(float(r['pnl']) for r in bucket_rows)
        w = sum(1 for r in bucket_rows if float(r['pnl']) > 0.01)
        l = sum(1 for r in bucket_rows if float(r['pnl']) < -0.01)
        print(f"  {label}: {count:>3} trades ({w}W/{l}L), P&L: ${pnl:>+7.2f} (avg ${pnl/count:>+.2f})")

    # Side analysis
    print(f"\n--- P&L by Side ---")
    for side_val in ["YES", "NO"]:
        side_rows = [r for r in rows if r['side'] == side_val and r['exit_price']]
        if not side_rows:
            continue
        count = len(side_rows)
        pnl = sum(float(r['pnl']) for r in side_rows)
        w = sum(1 for r in side_rows if float(r['pnl']) > 0.01)
        l = sum(1 for r in side_rows if float(r['pnl']) < -0.01)
        print(f"  {side_val}: {count:>3} trades ({w}W/{l}L), P&L: ${pnl:>+7.2f} (avg ${pnl/count:>+.2f})")

    # Biggest winners and losers
    closed = [r for r in rows if r['exit_price']]
    if closed:
        sorted_by_pnl = sorted(closed, key=lambda r: float(r['pnl']))
        print(f"\n--- Top 5 Losers ---")
        for r in sorted_by_pnl[:5]:
            print(f"  {str(r['time_et'])[:19]} {r['side']} entry=${float(r['entry_price']):.3f} exit=${float(r['exit_price']):.3f} P&L=${float(r['pnl']):+.2f}")
        print(f"\n--- Top 5 Winners ---")
        for r in sorted_by_pnl[-5:]:
            print(f"  {str(r['time_et'])[:19]} {r['side']} entry=${float(r['entry_price']):.3f} exit=${float(r['exit_price']):.3f} P&L=${float(r['pnl']):+.2f}")

    # Current config
    config_rows = await conn.fetch("""
        SELECT key, value FROM polyedge.risk_config
        WHERE key LIKE 'strategies.micro_sniper.%'
        ORDER BY key;
    """)
    print(f"\n--- Current Micro Sniper Config ---")
    for r in config_rows:
        val = r['value']
        # JSONB values come as strings, parse them
        if isinstance(val, str):
            print(f"  {r['key']}: {val}")
        else:
            print(f"  {r['key']}: {val}")

    # Current balance
    balance_row = await conn.fetchrow("""
        SELECT balance, total_pnl, trade_count
        FROM polyedge.portfolio_snapshots
        ORDER BY timestamp DESC LIMIT 1;
    """)
    if balance_row:
        print(f"\nLatest snapshot — Balance: ${float(balance_row['balance']):.2f}, "
              f"Total P&L: ${float(balance_row['total_pnl']):.2f}, "
              f"Trades: {balance_row['trade_count']}")

    # USDC balance from orders table or just show what we have
    await conn.close()

asyncio.run(main())
