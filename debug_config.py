#!/usr/bin/env python3
"""Debug script: verify poly_book_enabled config loading end-to-end.

Runs the exact same config loading path as `polyedge micro` and prints
every step so we can see where the value gets lost.

Usage:
    .venv/bin/python debug_config.py
"""

import asyncio
import json
import os
import sys

# Add src to path so we can import polyedge
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))


async def main():
    from polyedge.core.config import load_config, apply_db_config
    from polyedge.core.db import Database

    # Step 1: Load settings from YAML/env/Keychain (same as get_settings())
    settings = load_config()
    micro_cfg = settings.strategies.micro_sniper

    print("=" * 70)
    print("STEP 1: After load_config() (YAML + env + Keychain)")
    print(f"  poly_book_enabled = {micro_cfg.poly_book_enabled!r} (type={type(micro_cfg.poly_book_enabled).__name__})")
    print(f"  poly_book_exit_override_depth = {micro_cfg.poly_book_exit_override_depth!r}")
    print(f"  poly_book_exit_override_imbalance = {micro_cfg.poly_book_exit_override_imbalance!r}")
    print(f"  MicroSniperConfig id = {id(micro_cfg)}")
    print()

    # Step 2: Connect to DB and read raw config
    db = Database(settings.database_url)
    await db.connect()

    raw_config = await db.get_all_config()
    print("STEP 2: Raw DB config (get_all_config())")
    book_keys = {k: v for k, v in raw_config.items() if "poly_book" in k}
    if book_keys:
        for k, v in book_keys.items():
            print(f"  {k} = {v!r} (type={type(v).__name__})")
    else:
        print("  ⚠️  NO poly_book keys found in DB!")
        print("  All strategies.micro_sniper keys:")
        for k, v in sorted(raw_config.items()):
            if "micro_sniper" in k:
                print(f"    {k} = {v!r} (type={type(v).__name__})")
    print()

    # Step 3: Apply DB config (same as the micro command does)
    settings = await apply_db_config(settings, db)
    micro_cfg_after = settings.strategies.micro_sniper

    print("STEP 3: After apply_db_config()")
    print(f"  poly_book_enabled = {micro_cfg_after.poly_book_enabled!r} (type={type(micro_cfg_after.poly_book_enabled).__name__})")
    print(f"  poly_book_exit_override_depth = {micro_cfg_after.poly_book_exit_override_depth!r}")
    print(f"  poly_book_exit_override_imbalance = {micro_cfg_after.poly_book_exit_override_imbalance!r}")
    print(f"  MicroSniperConfig id = {id(micro_cfg_after)}")
    print(f"  Same object as step 1? {id(micro_cfg) == id(micro_cfg_after)}")
    print()

    # Step 4: Simulate what the runner does
    from polyedge.strategies.micro_sniper import MicroSniperStrategy
    strategy = MicroSniperStrategy(settings)

    print("STEP 4: Strategy config (after MicroSniperStrategy(settings))")
    print(f"  strategy.config.poly_book_enabled = {strategy.config.poly_book_enabled!r}")
    print(f"  strategy.config id = {id(strategy.config)}")
    print(f"  Same object as settings.strategies.micro_sniper? {id(strategy.config) == id(micro_cfg_after)}")
    print()

    # Step 5: Would the runner's check pass?
    would_check = strategy.config.poly_book_enabled
    print("STEP 5: Runtime check simulation")
    print(f"  if self.config.poly_book_enabled → {would_check}")
    if would_check:
        print("  ✅ Book override WOULD fire (config is True)")
    else:
        print("  ❌ Book override WOULD NOT fire (config is False)")
        print()
        print("  Debugging further:")
        print(f"    bool(strategy.config.poly_book_enabled) = {bool(strategy.config.poly_book_enabled)}")
        print(f"    strategy.config.poly_book_enabled == True → {strategy.config.poly_book_enabled == True}")
        print(f"    type = {type(strategy.config.poly_book_enabled)}")
        # Check if it's a string "true" instead of bool True
        if isinstance(strategy.config.poly_book_enabled, str):
            print(f"    ⚠️  VALUE IS A STRING '{strategy.config.poly_book_enabled}', NOT A BOOL!")
            print(f"    This means the DB returned a string and setattr stored it without type coercion.")
    print()

    # Bonus: check all micro_sniper config values and their types
    print("BONUS: All MicroSniperConfig field values + types")
    for field_name in sorted(micro_cfg_after.model_fields.keys()):
        val = getattr(micro_cfg_after, field_name)
        expected_type = micro_cfg_after.model_fields[field_name].annotation
        print(f"  {field_name}: {val!r} ({type(val).__name__}, expected {expected_type})")

    await db.close()
    print("\n" + "=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
