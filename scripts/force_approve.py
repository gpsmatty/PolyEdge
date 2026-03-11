"""Force approve both exchange contracts for conditional token trading.

Run: .venv/bin/python scripts/force_approve.py
"""
import subprocess
import sys

def kc(account):
    r = subprocess.run(
        ["security", "find-generic-password", "-s", "polyedge", "-a", account, "-w"],
        capture_output=True, text=True,
    )
    return r.stdout.strip()

def main():
    from web3 import Web3
    from eth_account import Account

    key = kc("poly_private_key")
    if not key:
        print("No private key in Keychain")
        sys.exit(1)

    account = Account.from_key(key)
    wallet = account.address
    print(f"Wallet: {wallet}")

    # Try multiple RPCs
    rpc_urls = [
        "https://polygon.llamarpc.com",
        "https://rpc.ankr.com/polygon",
        "https://polygon-bor-rpc.publicnode.com",
    ]

    w3 = None
    for url in rpc_urls:
        try:
            _w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 10}))
            if _w3.is_connected():
                w3 = _w3
                print(f"Connected to {url}")
                break
        except Exception:
            continue

    if not w3:
        print("Could not connect to any RPC")
        sys.exit(1)

    # Contracts
    CT_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
    EXCHANGE_REGULAR = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
    EXCHANGE_NEGRISK = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

    abi = [
        {"inputs": [{"name": "operator", "type": "address"}, {"name": "approved", "type": "bool"}],
         "name": "setApprovalForAll", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
        {"inputs": [{"name": "account", "type": "address"}, {"name": "operator", "type": "address"}],
         "name": "isApprovedForAll", "outputs": [{"name": "", "type": "bool"}],
         "stateMutability": "view", "type": "function"},
    ]

    ct = w3.eth.contract(address=Web3.to_checksum_address(CT_ADDRESS), abi=abi)

    # Check MATIC balance
    matic = w3.eth.get_balance(wallet)
    print(f"MATIC balance: {w3.from_wei(matic, 'ether'):.4f}")

    if matic < Web3.to_wei(0.001, "ether"):
        print("WARNING: Very low MATIC — may not have enough gas")

    for label, exchange in [("Regular", EXCHANGE_REGULAR), ("Neg-Risk", EXCHANGE_NEGRISK)]:
        approved = ct.functions.isApprovedForAll(
            Web3.to_checksum_address(wallet),
            Web3.to_checksum_address(exchange),
        ).call()

        print(f"\n{label} exchange ({exchange[:10]}...):")
        print(f"  Approved: {approved}")

        if approved:
            print(f"  ✓ Already approved — no action needed")
            continue

        print(f"  ✗ NOT approved — sending approval tx...")

        nonce = w3.eth.get_transaction_count(wallet, "pending")
        gas_price = max(w3.eth.gas_price, Web3.to_wei(80, "gwei"))
        print(f"  Nonce: {nonce}, Gas price: {w3.from_wei(gas_price, 'gwei'):.0f} gwei")

        tx = ct.functions.setApprovalForAll(
            Web3.to_checksum_address(exchange), True
        ).build_transaction({
            "from": wallet,
            "nonce": nonce,
            "gas": 60000,
            "gasPrice": gas_price,
            "chainId": 137,
        })

        signed = w3.eth.account.sign_transaction(tx, key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        print(f"  TX sent: {tx_hash.hex()}")

        # Wait for confirmation
        print(f"  Waiting for confirmation...")
        try:
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            print(f"  ✓ Confirmed in block {receipt['blockNumber']} — status: {receipt['status']}")
        except Exception as e:
            print(f"  ⏳ Not confirmed yet: {e}")
            print(f"  Check: https://polygonscan.com/tx/{tx_hash.hex()}")

    print("\nDone! Rebuild and restart the bot.")

if __name__ == "__main__":
    main()
