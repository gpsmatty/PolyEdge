#!/usr/bin/env python3
"""Test Polymarket WebSocket — verify order book data is flowing.

Uses the same Gamma API strategy as the micro sniper: endDate ASC with
end_date_min=now to find the soonest-expiring crypto up/down markets.

Usage:
    .venv/bin/python test_poly_ws.py                  # Auto-find BTC up/down
    .venv/bin/python test_poly_ws.py --token YES NO   # Specific token IDs
    .venv/bin/python test_poly_ws.py --search "ETH"   # Search keyword
"""

import asyncio
import json
import re
import sys
import time
from datetime import datetime, timezone


async def fetch_live_market(search: str = "Bitcoin"):
    """Find a live crypto up/down market using the same strategy as micro sniper.

    Queries Gamma API sorted by endDate ASC with end_date_min=now — puts the
    soonest-expiring (currently live) windows first, exactly like _quick_sync().
    """
    import aiohttp

    now = datetime.now(timezone.utc)
    gamma_url = "https://gamma-api.polymarket.com/markets"

    params = {
        "limit": 100,
        "offset": 0,
        "active": "true",
        "closed": "false",
        "order": "endDate",
        "ascending": "true",
        "end_date_min": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    print(f"  Querying: endDate ASC, end_date_min={params['end_date_min']}")

    async with aiohttp.ClientSession() as session:
        async with session.get(gamma_url, params=params) as resp:
            if resp.status != 200:
                print(f"Gamma API error: {resp.status}")
                return None
            markets = await resp.json()

    print(f"  Got {len(markets)} markets from Gamma API")

    # Filter for crypto up/down markets (same regex as crypto_sniper.py)
    up_down = re.compile(
        r'(?:Bitcoin|BTC|Ethereum|ETH|Solana|SOL|XRP|Dogecoin|DOGE)\s+.*?[Uu]p\s+or\s+[Dd]own',
        re.IGNORECASE,
    )

    found = []
    for m in markets:
        q = m.get("question", "")
        if not up_down.search(q):
            continue

        tokens_raw = m.get("clobTokenIds", "")
        if not tokens_raw:
            continue
        token_list = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
        if len(token_list) < 2:
            continue

        found.append({
            "question": q,
            "condition_id": m.get("conditionID", m.get("condition_id", "")),
            "yes_token": token_list[0],
            "no_token": token_list[1],
            "end_date": m.get("endDate", ""),
        })

    print(f"  Found {len(found)} crypto up/down markets")

    if not found:
        return None

    # Filter by search keyword
    if search:
        kw = search.lower()
        matching = [f for f in found if kw in f["question"].lower()]
        if matching:
            return matching[0]

    return found[0]


async def main():
    import websockets

    token_ids = []
    search = "Bitcoin"

    # Parse args
    args = sys.argv[1:]
    if "--token" in args:
        idx = args.index("--token")
        token_ids = args[idx+1:]  # Take all remaining args as token IDs
        args = args[:idx]
    if "--search" in args:
        idx = args.index("--search")
        if idx + 1 < len(args):
            search = args[idx + 1]

    if not token_ids:
        print(f"Finding live '{search}' up/down market (same as micro sniper)...\n")
        market = await fetch_live_market(search)
        if not market:
            print("\nNo live crypto up/down market found.")
            print("Markets may not be running right now.")
            return

        token_ids = [market["yes_token"], market["no_token"]]
        print(f"\nMarket: {market['question']}")
        print(f"YES token: {token_ids[0]}")
        print(f"NO token:  {token_ids[1]}")
        print(f"End date:  {market['end_date']}")
    else:
        print(f"Using provided token IDs: {token_ids}")

    print()

    ws_url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    print(f"Connecting to Polymarket WebSocket...")
    print(f"Subscribing to {len(token_ids)} tokens (YES + NO)")
    print("=" * 80)
    print()

    book_count = 0
    price_count = 0
    trade_count = 0
    bba_count = 0
    other_count = 0
    start_time = time.time()

    try:
        async with websockets.connect(ws_url, ping_interval=20) as ws:
            # Subscribe with YES/NO token IDs as assets_ids
            sub_msg = {
                "assets_ids": token_ids,
                "type": "market",
            }
            await ws.send(json.dumps(sub_msg))
            print(f"[{_ts()}] Subscribed to assets_ids:")
            for i, tid in enumerate(token_ids):
                label = "YES" if i == 0 else "NO" if i == 1 else f"#{i}"
                print(f"  {label}: {tid}")
            print()

            # Ping task (Polymarket requires PING every 10s)
            async def ping_loop():
                while True:
                    await asyncio.sleep(10)
                    try:
                        await ws.send("PING")
                    except Exception:
                        break

            ping_task = asyncio.create_task(ping_loop())
            last_summary = time.time()

            try:
                async for raw in ws:
                    if raw == "PONG":
                        continue

                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        print(f"[{_ts()}] Non-JSON: {raw[:200]}")
                        continue

                    events = data if isinstance(data, list) else [data]

                    for event in events:
                        event_type = event.get("event_type", "unknown")
                        asset_id = event.get("asset_id", "")
                        side = "YES" if asset_id == token_ids[0] else ("NO" if len(token_ids) > 1 and asset_id == token_ids[1] else "?")

                        if event_type == "book":
                            book_count += 1
                            bids = event.get("bids", [])
                            asks = event.get("asks", [])
                            total_bid = sum(float(b.get("size", 0)) for b in bids)
                            total_ask = sum(float(a.get("size", 0)) for a in asks)

                            if book_count <= 6:
                                print(f"[{_ts()}] 📖 BOOK #{book_count} ({side})")
                                print(f"  Bids: {len(bids)} levels, total size: {total_bid:.0f}")
                                for b in bids[:5]:
                                    print(f"    ${b.get('price', '?')} x {b.get('size', '?')}")
                                if len(bids) > 5:
                                    print(f"    ... +{len(bids)-5} more")
                                print(f"  Asks: {len(asks)} levels, total size: {total_ask:.0f}")
                                for a in asks[:5]:
                                    print(f"    ${a.get('price', '?')} x {a.get('size', '?')}")
                                if len(asks) > 5:
                                    print(f"    ... +{len(asks)-5} more")
                                print()
                            elif book_count % 20 == 0:
                                print(f"[{_ts()}] 📖 BOOK #{book_count} ({side}) — {len(bids)}b/{len(asks)}a, depth={total_bid:.0f}/{total_ask:.0f}")

                        elif event_type == "price_change":
                            price_count += 1
                            if price_count <= 10:
                                changes = event.get("changes", [])
                                print(f"[{_ts()}] 💰 PRICE ({side}): {json.dumps(changes)[:200]}")
                            elif price_count % 50 == 0:
                                print(f"[{_ts()}] 💰 PRICE #{price_count}")

                        elif event_type == "last_trade_price":
                            trade_count += 1
                            price = event.get("price", "?")
                            if trade_count <= 10:
                                print(f"[{_ts()}] 🔄 TRADE ({side}): price={price}")
                            elif trade_count % 50 == 0:
                                print(f"[{_ts()}] 🔄 TRADE #{trade_count}")

                        elif event_type == "best_bid_ask":
                            bba_count += 1
                            if bba_count <= 10:
                                print(f"[{_ts()}] 📊 BBA ({side}): bid={event.get('best_bid', '?')} ask={event.get('best_ask', '?')}")
                            elif bba_count % 50 == 0:
                                print(f"[{_ts()}] 📊 BBA #{bba_count}")

                        else:
                            other_count += 1
                            print(f"[{_ts()}] ❓ {event_type.upper()}: {json.dumps(event)[:300]}")

                    # Summary every 30 seconds
                    now = time.time()
                    if now - last_summary >= 30:
                        elapsed = now - start_time
                        print(f"\n--- {elapsed:.0f}s: {book_count} books, {price_count} prices, "
                              f"{trade_count} trades, {bba_count} bba ---\n")
                        last_summary = now

            except websockets.exceptions.ConnectionClosed as e:
                print(f"\nConnection closed: {e}")
            except KeyboardInterrupt:
                print("\nStopped by user (Ctrl+C)")
            finally:
                ping_task.cancel()

    except Exception as e:
        print(f"Connection error: {e}")

    elapsed = time.time() - start_time
    print(f"\n{'='*80}")
    print(f"Session: {elapsed:.0f}s | {book_count} books, {price_count} prices, "
          f"{trade_count} trades, {bba_count} bba, {other_count} other")

    if book_count == 0:
        print("\n⚠️  ZERO book snapshots received!")
        print("   The Polymarket WS never sent order book data for these tokens.")
        print("   This means the book override feature CAN'T work — no data to check.")
        print("   The poly_feed.books dict will always be empty.")
    else:
        print(f"\n✅ Book data IS flowing — {book_count} snapshots received.")
        print("   If book override still doesn't fire, the issue is config loading.")


def _ts():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


if __name__ == "__main__":
    asyncio.run(main())
