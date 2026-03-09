"""Polymarket CLOB client wrapper."""

from __future__ import annotations

import os
from typing import Optional

from eth_account import Account
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

from polyedge.core.config import Settings


def get_poly_side(side: str) -> str:
    """Convert our side enum to py-clob-client side constant."""
    return BUY if side.upper() in ("YES", "BUY") else SELL


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
    ) -> dict:
        """Place a limit order on Polymarket."""
        self.ensure_ready()
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=get_poly_side(side),
        )
        signed_order = self.client.create_order(order_args)
        return self.client.post_order(signed_order, OrderType.GTC)

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

    # --- Wallet ---

    @staticmethod
    def generate_wallet() -> dict:
        """Generate a new EOA wallet for trading."""
        account = Account.create()
        return {
            "address": account.address,
            "private_key": account.key.hex(),
        }
