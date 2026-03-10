#!/usr/bin/env python3
"""Direct CLOB API balance test — bypasses py-clob-client SDK.

Run from PolyEdge root:
    .venv/bin/python scripts/test_balance.py

Tests the balance endpoint with different address + signature_type combinations
to find out which one returns the $150 USDC balance.
"""

import hashlib
import hmac
import base64
import json
import subprocess
import time
from datetime import datetime
from urllib.parse import urlencode

import requests


def get_from_keychain(account: str) -> str:
    result = subprocess.run(
        ["security", "find-generic-password", "-s", "polyedge", "-a", account, "-w"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


# Load secrets
wallet_address = get_from_keychain("poly_wallet_address")  # EOA
proxy_address = get_from_keychain("poly_proxy_address")     # Proxy
api_key = get_from_keychain("poly_api_key")
api_secret = get_from_keychain("poly_api_secret")
api_passphrase = get_from_keychain("poly_api_passphrase")

print(f"EOA:   {wallet_address}")
print(f"Proxy: {proxy_address}")
print(f"API Key: {api_key[:8]}...")
print()

HOST = "https://clob.polymarket.com"


def build_hmac_sig(secret: str, timestamp: int, method: str, path: str, body: str = ""):
    message = str(timestamp) + str(method) + str(path) + (body or "")
    decoded_secret = base64.urlsafe_b64decode(secret)
    signature = hmac.new(decoded_secret, message.encode("utf-8"), hashlib.sha256)
    return base64.urlsafe_b64encode(signature.digest()).decode("utf-8")


def test_balance(label: str, address: str, params: dict):
    """Make an authenticated L2 GET to /balance-allowance."""
    path = "/balance-allowance"
    timestamp = int(datetime.now().timestamp())
    hmac_sig = build_hmac_sig(api_secret, timestamp, "GET", path)

    headers = {
        "POLY_ADDRESS": address,
        "POLY_SIGNATURE": hmac_sig,
        "POLY_TIMESTAMP": str(timestamp),
        "POLY_API_KEY": api_key,
        "POLY_PASSPHRASE": api_passphrase,
    }

    url = f"{HOST}{path}?" + urlencode(params)

    print(f"{'=' * 60}")
    print(f"{label}")
    print(f"{'=' * 60}")
    print(f"  URL:    {url}")
    print(f"  POLY_ADDRESS: {address}")

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        print(f"  Status: {resp.status_code}")
        try:
            data = resp.json()
            bal = data.get("balance", "?")
            # Balance is in wei (6 decimals for USDC)
            if bal != "?" and bal != "0":
                usdc = int(bal) / 1_000_000
                print(f"  Balance: {bal} wei = ${usdc:.2f} USDC  <--- FOUND IT!")
            else:
                print(f"  Balance: {bal}")
            if "allowances" in data:
                print(f"  Allowances: {data['allowances']}")
        except:
            print(f"  Raw: {resp.text[:300]}")
    except Exception as e:
        print(f"  Error: {e}")
    print()


# --- Run all combinations ---

# Test 1: SDK default behavior — EOA + signature_type from URL param
test_balance(
    "TEST 1: EOA address, signature_type=0 (default EOA mode)",
    wallet_address,
    {"asset_type": "COLLATERAL", "signature_type": 0},
)

# Test 2: What the current code does — EOA + signature_type=1
test_balance(
    "TEST 2: EOA address, signature_type=1 (proxy mode, current code)",
    wallet_address,
    {"asset_type": "COLLATERAL", "signature_type": 1},
)

# Test 3: Try proxy address itself, no signature_type
test_balance(
    "TEST 3: PROXY address, signature_type=0",
    proxy_address,
    {"asset_type": "COLLATERAL", "signature_type": 0},
)

# Test 4: Proxy address + signature_type=1
test_balance(
    "TEST 4: PROXY address, signature_type=1",
    proxy_address,
    {"asset_type": "COLLATERAL", "signature_type": 1},
)

# Test 5: Proxy address + signature_type=2 (just in case)
test_balance(
    "TEST 5: PROXY address, signature_type=2",
    proxy_address,
    {"asset_type": "COLLATERAL", "signature_type": 2},
)

# Test 6: EOA address + signature_type=2
test_balance(
    "TEST 6: EOA address, signature_type=2",
    wallet_address,
    {"asset_type": "COLLATERAL", "signature_type": 2},
)

print("\nDone. Look for '<--- FOUND IT!' above to see which combo returns your balance.")
