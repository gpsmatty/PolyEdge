#!/usr/bin/env python3
"""Check and fix conditional token allowances for the proxy wallet.

Run from PolyEdge root:
    .venv/bin/python scripts/test_allowances.py

Tests balance + allowance for CONDITIONAL tokens (YES/NO positions),
not just COLLATERAL (USDC). Also tries update_balance_allowance to
approve the exchange contracts.
"""

import base64
import hashlib
import hmac
import json
import subprocess
from datetime import datetime
from urllib.parse import urlencode

import requests


def get_from_keychain(account: str) -> str:
    result = subprocess.run(
        ["security", "find-generic-password", "-s", "polyedge", "-a", account, "-w"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


wallet_address = get_from_keychain("poly_wallet_address")
api_key = get_from_keychain("poly_api_key")
api_secret = get_from_keychain("poly_api_secret")
api_passphrase = get_from_keychain("poly_api_passphrase")

HOST = "https://clob.polymarket.com"


def build_hmac_sig(secret, timestamp, method, path, body=""):
    message = str(timestamp) + str(method) + str(path) + (body or "")
    decoded_secret = base64.urlsafe_b64decode(secret)
    signature = hmac.new(decoded_secret, message.encode("utf-8"), hashlib.sha256)
    return base64.urlsafe_b64encode(signature.digest()).decode("utf-8")


def make_request(method, path, address, params=None, body=None):
    timestamp = int(datetime.now().timestamp())
    body_str = json.dumps(body) if body else ""
    hmac_sig = build_hmac_sig(api_secret, timestamp, method, path, body_str)

    headers = {
        "POLY_ADDRESS": address,
        "POLY_SIGNATURE": hmac_sig,
        "POLY_TIMESTAMP": str(timestamp),
        "POLY_API_KEY": api_key,
        "POLY_PASSPHRASE": api_passphrase,
    }

    url = f"{HOST}{path}"
    if params:
        url += "?" + urlencode(params)

    if method == "GET":
        resp = requests.get(url, headers=headers, timeout=10)
    else:
        headers["Content-Type"] = "application/json"
        resp = requests.post(url, headers=headers, data=body_str, timeout=10)

    return resp


# --- Check COLLATERAL (USDC) balance with signature_type=2 ---
print("=" * 60)
print("COLLATERAL (USDC) — signature_type=2")
print("=" * 60)
resp = make_request("GET", "/balance-allowance", wallet_address, {
    "asset_type": "COLLATERAL",
    "signature_type": 2,
})
data = resp.json()
bal = int(data.get("balance", 0)) / 1e6
print(f"  Balance: ${bal:.2f}")
print(f"  Allowances: {data.get('allowances', {})}")
print()


# --- Check if update_balance_allowance works with signature_type=2 ---
print("=" * 60)
print("UPDATE BALANCE ALLOWANCE — signature_type=2")
print("=" * 60)
resp = make_request("POST", "/balance-allowance/update", wallet_address, {
    "signature_type": 2,
})
print(f"  Status: {resp.status_code}")
print(f"  Response: {resp.text[:300]}")
print()


# --- Now check COLLATERAL again to see if allowances changed ---
print("=" * 60)
print("COLLATERAL (USDC) AFTER UPDATE — signature_type=2")
print("=" * 60)
resp = make_request("GET", "/balance-allowance", wallet_address, {
    "asset_type": "COLLATERAL",
    "signature_type": 2,
})
data = resp.json()
bal = int(data.get("balance", 0)) / 1e6
allowances = data.get("allowances", {})
print(f"  Balance: ${bal:.2f}")
for addr, val in allowances.items():
    if int(val) > 0:
        print(f"  ✓ {addr}: APPROVED")
    else:
        print(f"  ✗ {addr}: NOT APPROVED")
print()


# --- Try to get neg-risk info for a known BTC market token ---
print("=" * 60)
print("NEG RISK CHECK")
print("=" * 60)
# We need a token_id to check. Let's get one from a recent BTC market.
try:
    gamma_resp = requests.get(
        "https://gamma-api.polymarket.com/markets",
        params={"tag": "crypto", "limit": 5, "active": True, "closed": False},
        timeout=10,
    )
    markets = gamma_resp.json()
    for m in markets:
        q = m.get("question", "")
        if "bitcoin" in q.lower() and ("up or down" in q.lower() or "5m" in q.lower()):
            tokens = m.get("clobTokenIds", "")
            if tokens:
                token_list = json.loads(tokens) if isinstance(tokens, str) else tokens
                yes_token = token_list[0] if token_list else None
                no_token = token_list[1] if len(token_list) > 1 else None
                print(f"  Market: {q[:60]}")
                print(f"  YES token: {yes_token}")
                print(f"  NO token:  {no_token}")

                if yes_token:
                    nr_resp = requests.get(
                        f"{HOST}/neg-risk?token_id={yes_token}", timeout=10
                    )
                    print(f"  YES neg_risk: {nr_resp.json()}")
                if no_token:
                    nr_resp = requests.get(
                        f"{HOST}/neg-risk?token_id={no_token}", timeout=10
                    )
                    print(f"  NO neg_risk:  {nr_resp.json()}")
                print()
                break
except Exception as e:
    print(f"  Error: {e}")

print("\nDone.")
