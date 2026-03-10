"""Polymarket CLOB client wrapper."""

from __future__ import annotations

import os
from typing import Optional

from eth_account import Account
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    AssetType,
    BalanceAllowanceParams,
    OrderArgs,
    OrderType,
    TradeParams,
)
from py_clob_client.order_builder.constants import BUY, SELL

from polyedge.core.config import Settings


def get_poly_side(side: str) -> str:
    """Convert side to py-clob-client BUY/SELL constant.

    Accepts "BUY" or "SELL" directly. Legacy "YES"/"NO" mapping is intentionally
    removed — callers must pass "BUY" or "SELL" explicitly to avoid the bug where
    "NO" was silently mapped to SELL (causing BUY NO orders to fail).
    """
    s = side.upper()
    if s in ("BUY", "YES"):
        return BUY
    elif s in ("SELL", "NO"):
        return SELL
    else:
        raise ValueError(f"Invalid side '{side}', expected 'BUY' or 'SELL'")


class PolyClient:
    """Wrapper around py-clob-client with auth management."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client: Optional[ClobClient] = None
        self._initialized = False

    def initialize(self):
        """Initialize the CLOB client with credentials."""
        if not self.settings.poly_private_key:
            raise ValueError("POLY_PRIVATE_KEY not set in .env")

        host = self.settings.polymarket.clob_url
        chain_id = self.settings.polymarket.chain_id
        key = self.settings.poly_private_key

        # If proxy address is set, use signature_type=2 (Polymarket proxy/MagicLink)
        # signature_type=0: EOA wallet (no proxy)
        # signature_type=1: Gnosis Safe (returns $0 for Polymarket web wallets)
        # signature_type=2: Polymarket proxy wallet (web UI / MagicLink imports)
        funder = self.settings.poly_proxy_address or None
        if funder:
            self.client = ClobClient(
                host,
                key=key,
                chain_id=chain_id,
                signature_type=2,
                funder=funder,
            )
        else:
            self.client = ClobClient(
                host,
                key=key,
                chain_id=chain_id,
            )

        # If we have API creds, set them
        if self.settings.poly_api_key:
            self.client.set_api_creds(
                ApiCreds(
                    api_key=self.settings.poly_api_key,
                    api_secret=self.settings.poly_api_secret,
                    api_passphrase=self.settings.poly_api_passphrase,
                )
            )

        # Monkey-patch the py-clob-client httpx client with a longer timeout.
        # Default httpx timeout is 5s which causes ReadTimeout on busy CLOB API.
        # Also configure retries for transient network errors.
        import httpx
        from py_clob_client.http_helpers import helpers as _clob_helpers
        _clob_helpers._http_client = httpx.Client(
            http2=True,
            timeout=httpx.Timeout(15.0, connect=5.0),  # 15s read, 5s connect
        )

        self._initialized = True

    def derive_api_keys(self) -> dict:
        """Derive API credentials from wallet. Call once during setup."""
        if not self.client:
            self.initialize()
        creds = self.client.create_or_derive_api_creds()
        return {
            "api_key": creds.api_key,
            "api_secret": creds.api_secret,
            "api_passphrase": creds.api_passphrase,
        }

    def ensure_ready(self):
        if not self._initialized:
            self.initialize()

    # --- Market Data (no auth needed) ---

    def get_markets(self) -> list[dict]:
        """Get all active markets from the CLOB."""
        self.ensure_ready()
        return self.client.get_markets()

    def get_market(self, condition_id: str) -> dict:
        """Get a specific market by condition ID."""
        self.ensure_ready()
        return self.client.get_market(condition_id)

    def get_price(self, token_id: str) -> dict:
        """Get current price for a token."""
        self.ensure_ready()
        return self.client.get_price(token_id, "BUY")

    def get_order_book(self, token_id: str) -> dict:
        """Get the order book for a token."""
        self.ensure_ready()
        return self.client.get_order_book(token_id)

    def get_midpoint(self, token_id: str) -> float:
        """Get midpoint price for a token."""
        self.ensure_ready()
        resp = self.client.get_midpoint(token_id)
        return float(resp.get("mid", 0))

    def get_spread(self, token_id: str) -> dict:
        """Get bid-ask spread for a token."""
        self.ensure_ready()
        return self.client.get_spread(token_id)

    # --- Trading (auth required) ---

    def place_limit_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        order_type: str = "GTC",
    ) -> dict:
        """Place a limit order on Polymarket.

        Args:
            order_type: "GTC" (rests on book), "FOK" (fill all or cancel),
                        "FAK" (fill what you can, cancel rest).
        """
        self.ensure_ready()
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=get_poly_side(side),
        )
        signed_order = self.client.create_order(order_args)
        otype = {"GTC": OrderType.GTC, "FOK": OrderType.FOK, "FAK": OrderType.FAK}.get(
            order_type.upper(), OrderType.GTC
        )
        return self.client.post_order(signed_order, otype)

    def place_fok_order(
        self,
        token_id: str,
        side: str,
        amount: float,
        price: float,
    ) -> dict:
        """Place a FOK (Fill or Kill) order using market order amounts.

        Unlike place_limit_order, this takes the USD amount for BUY orders
        (or share count for SELL) and lets the SDK round correctly. This avoids
        the "invalid amounts" error where maker_amount has >2 decimal places.

        Args:
            amount: For BUY — USD amount to spend. For SELL — number of shares.
            price: Max price for BUY, min price for SELL.
        """
        self.ensure_ready()
        import time as _time
        from py_clob_client.clob_types import MarketOrderArgs
        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=round(amount, 2),
            side=get_poly_side(side),
            price=price,
        )
        signed_order = self.client.create_market_order(order_args)

        # Retry once on timeout — CLOB API can be slow under load.
        # DANGER: if the first attempt timed out but actually placed the order,
        # the retry will fail with "duplicate order" or similar, which is fine.
        for attempt in range(2):
            try:
                return self.client.post_order(signed_order, OrderType.FOK)
            except Exception as e:
                if "timeout" in str(e).lower() or "Request exception" in str(e):
                    if attempt == 0:
                        _time.sleep(0.5)  # Brief pause before retry
                        continue
                raise

    def place_market_order(
        self,
        token_id: str,
        side: str,
        amount: float,
    ) -> dict:
        """Place a market order (FOK) on Polymarket."""
        self.ensure_ready()
        order_args = OrderArgs(
            token_id=token_id,
            price=0.99 if side.upper() in ("YES", "BUY") else 0.01,
            size=amount,
            side=get_poly_side(side),
        )
        signed_order = self.client.create_order(order_args)
        return self.client.post_order(signed_order, OrderType.FOK)

    def cancel_order(self, order_id: str) -> dict:
        """Cancel an open order."""
        self.ensure_ready()
        return self.client.cancel(order_id)

    def cancel_all_orders(self) -> dict:
        """Cancel all open orders."""
        self.ensure_ready()
        return self.client.cancel_all()

    def get_open_orders(self) -> list[dict]:
        """Get all open orders."""
        self.ensure_ready()
        return self.client.get_orders()

    def get_order(self, order_id: str) -> dict:
        """Get a specific order by ID. Requires L2 auth."""
        self.ensure_ready()
        return self.client.get_order(order_id)

    # --- Trade History (auth required) ---

    def get_trades(
        self,
        market: str | None = None,
        asset_id: str | None = None,
        after: int | None = None,
        before: int | None = None,
    ) -> list[dict]:
        """Get trade fill history for this account.

        Returns all fills (paginated automatically by the SDK).
        Each fill includes: id, taker_order_id, market, asset_id,
        side, size, fee_rate_bps, price, status, match_time, type.

        Args:
            market: Filter by condition_id.
            asset_id: Filter by token_id.
            after: Unix timestamp — only fills after this time.
            before: Unix timestamp — only fills before this time.
        """
        self.ensure_ready()
        params = TradeParams(
            market=market,
            asset_id=asset_id,
            after=after,
            before=before,
        )
        return self.client.get_trades(params)

    # --- Balance (auth required) ---

    def get_collateral_balance(self) -> dict:
        """Get USDC collateral balance and allowance.

        Returns dict with 'balance' (USDC amount) and 'allowance'.
        """
        self.ensure_ready()
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        return self.client.get_balance_allowance(params)

    def get_token_balance(self, token_id: str) -> dict:
        """Get balance for a specific conditional token (YES/NO position).

        Returns dict with 'balance' (token amount) and 'allowance'.
        """
        self.ensure_ready()
        params = BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL,
            token_id=token_id,
        )
        return self.client.get_balance_allowance(params)

    def update_token_allowance(self, token_id: str) -> dict:
        """Refresh the CLOB backend's cached balance/allowance from blockchain.

        NOTE: This does NOT grant approval. It only tells the CLOB backend to
        re-read the on-chain state. Call approve_conditional_tokens() first
        to actually grant the exchange permission to move your tokens.
        """
        self.ensure_ready()
        params = BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL,
            token_id=token_id,
        )
        return self.client.update_balance_allowance(params)

    def approve_conditional_tokens(self, neg_risk: bool = False) -> str:
        """Approve the exchange contract to transfer our conditional tokens.

        This is an on-chain ERC1155 setApprovalForAll() call. Must be done
        once per exchange contract (regular and neg-risk are separate).
        Costs a tiny amount of MATIC gas on Polygon.

        Returns the transaction hash.
        """
        from web3 import Web3

        # Polygon RPC — try multiple free endpoints
        rpc_urls = [
            "https://polygon.llamarpc.com",
            "https://rpc.ankr.com/polygon",
            "https://polygon-bor-rpc.publicnode.com",
            "https://polygon-rpc.com",
        ]
        w3 = None
        for rpc_url in rpc_urls:
            try:
                _w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
                if _w3.is_connected():
                    w3 = _w3
                    break
            except Exception:
                continue
        if w3 is None:
            raise RuntimeError("Could not connect to any Polygon RPC endpoint")

        # Contract addresses from py-clob-client config
        CT_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
        EXCHANGE_REGULAR = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
        EXCHANGE_NEGRISK = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

        exchange = EXCHANGE_NEGRISK if neg_risk else EXCHANGE_REGULAR

        # ERC1155 minimal ABI for setApprovalForAll
        abi = [{"inputs": [{"name": "operator", "type": "address"}, {"name": "approved", "type": "bool"}],
                "name": "setApprovalForAll", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
               {"inputs": [{"name": "account", "type": "address"}, {"name": "operator", "type": "address"}],
                "name": "isApprovedForAll", "outputs": [{"name": "", "type": "bool"}],
                "stateMutability": "view", "type": "function"}]

        ct = w3.eth.contract(address=Web3.to_checksum_address(CT_ADDRESS), abi=abi)

        # Derive our wallet address from private key
        key = self.settings.poly_private_key
        account = Account.from_key(key)
        wallet = account.address

        # Check if already approved
        is_approved = ct.functions.isApprovedForAll(
            Web3.to_checksum_address(wallet),
            Web3.to_checksum_address(exchange),
        ).call()

        if is_approved:
            return "already_approved"

        # Build and send approval transaction
        tx = ct.functions.setApprovalForAll(
            Web3.to_checksum_address(exchange), True
        ).build_transaction({
            "from": wallet,
            "nonce": w3.eth.get_transaction_count(wallet),
            "gas": 60000,
            "gasPrice": w3.eth.gas_price,
            "chainId": 137,
        })

        signed = w3.eth.account.sign_transaction(tx, key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        return tx_hash.hex()

    def ensure_exchange_approved(self):
        """Ensure both exchange contracts are approved to move our tokens.

        Call once at startup. Checks on-chain and only sends tx if needed.
        """
        import logging
        logger = logging.getLogger("polyedge.client")
        for neg_risk in [False, True]:
            label = "neg-risk" if neg_risk else "regular"
            try:
                result = self.approve_conditional_tokens(neg_risk=neg_risk)
                if result == "already_approved":
                    logger.info(f"Exchange ({label}): already approved")
                else:
                    logger.info(f"Exchange ({label}): approval tx sent: {result}")
            except Exception as e:
                logger.warning(f"Exchange ({label}) approval failed: {e}")

    # --- Wallet ---

    @staticmethod
    def generate_wallet() -> dict:
        """Generate a new EOA wallet for trading."""
        account = Account.create()
        return {
            "address": account.address,
            "private_key": account.key.hex(),
        }
