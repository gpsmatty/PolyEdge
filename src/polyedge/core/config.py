"""Configuration management — loads from YAML + env + database."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class PolymarketConfig(BaseModel):
    clob_url: str = "https://clob.polymarket.com"
    gamma_url: str = "https://gamma-api.polymarket.com"
    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    chain_id: int = 137


class AIConfig(BaseModel):
    provider: str = "claude"
    claude_model: str = "claude-sonnet-4-6"
    openai_model: str = "gpt-4o"
    ensemble: bool = False
    temperature: float = 0.2
    max_analysis_cost_per_day: float = 5.00


class RiskConfig(BaseModel):
    max_position_pct: float = 0.10
    max_exposure_pct: float = 0.50
    max_positions: int = 10
    drawdown_circuit_breaker: float = 0.25
    kelly_fraction: float = 0.25
    min_edge_threshold: float = 0.05
    min_confidence: float = 0.60
    max_trades_per_day: int = 20
    daily_loss_limit_pct: float = 0.15
    confirm_trades: bool = True
    categories_blacklist: list[str] = Field(default_factory=list)
    min_liquidity: float = 1000
    min_time_to_resolution_hours: float = 24


class CheapHunterConfig(BaseModel):
    enabled: bool = True
    max_price: float = 0.15
    min_volume: float = 500
    min_ev: float = 0.02


class EdgeFinderConfig(BaseModel):
    enabled: bool = True
    min_edge: float = 0.05
    use_ai: bool = True
    use_news: bool = True


class MarketMakerConfig(BaseModel):
    enabled: bool = False
    min_spread: float = 0.05
    max_inventory_pct: float = 0.20
    requote_threshold: float = 0.02


class StrategiesConfig(BaseModel):
    cheap_hunter: CheapHunterConfig = CheapHunterConfig()
    edge_finder: EdgeFinderConfig = EdgeFinderConfig()
    market_maker: MarketMakerConfig = MarketMakerConfig()


class AgentConfig(BaseModel):
    mode: str = "copilot"
    scan_interval_minutes: int = 5
    max_markets_per_scan: int = 50
    reanalyze_price_change_pct: float = 0.05


class Settings(BaseSettings):
    # Wallet
    poly_private_key: str = ""
    poly_wallet_address: str = ""

    # API credentials
    poly_api_key: str = ""
    poly_api_secret: str = ""
    poly_api_passphrase: str = ""

    # Database
    database_url: str = "postgresql://localhost:5432/polyedge"

    # AI
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # External APIs
    news_api_key: str = ""

    # Nested configs (loaded from YAML)
    polymarket: PolymarketConfig = PolymarketConfig()
    ai: AIConfig = AIConfig()
    risk: RiskConfig = RiskConfig()
    strategies: StrategiesConfig = StrategiesConfig()
    agent: AgentConfig = AgentConfig()

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


def load_config(config_path: Optional[str] = None) -> Settings:
    """Load settings from .env + YAML config file."""
    # First load env vars
    settings = Settings()

    # Then overlay YAML config
    if config_path is None:
        # Look for config relative to project root
        candidates = [
            Path("config/default.yaml"),
            Path(__file__).parent.parent.parent.parent / "config" / "default.yaml",
        ]
        for p in candidates:
            if p.exists():
                config_path = str(p)
                break

    if config_path and Path(config_path).exists():
        with open(config_path) as f:
            yaml_config = yaml.safe_load(f) or {}

        if "polymarket" in yaml_config:
            settings.polymarket = PolymarketConfig(**yaml_config["polymarket"])
        if "ai" in yaml_config:
            settings.ai = AIConfig(**yaml_config["ai"])
        if "risk" in yaml_config:
            settings.risk = RiskConfig(**yaml_config["risk"])
        if "strategies" in yaml_config:
            settings.strategies = StrategiesConfig(**yaml_config["strategies"])
        if "agent" in yaml_config:
            settings.agent = AgentConfig(**yaml_config["agent"])

    return settings
