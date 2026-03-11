"""Configuration management — loads from Keychain + env + YAML + database.

Priority (highest wins):
  1. Database (risk_config table) — portable across environments
  2. macOS Keychain (secrets only)
  3. Environment variables
  4. .env file
  5. YAML config (defaults / fallback)
  6. Pydantic defaults
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


# --- macOS Keychain integration ---

KEYCHAIN_SERVICE = "polyedge"

# Maps Settings field names to Keychain account names
KEYCHAIN_KEYS = [
    "poly_private_key",
    "poly_wallet_address",
    "poly_proxy_address",
    "poly_api_key",
    "poly_api_secret",
    "poly_api_passphrase",
    "database_url",
    "anthropic_api_key",
    "openai_api_key",
    "news_api_key",
]


def _get_from_keychain(account: str) -> Optional[str]:
    """Retrieve a secret from macOS Keychain. Returns None if not found."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _set_in_keychain(account: str, value: str) -> bool:
    """Store a secret in macOS Keychain. Updates if exists."""
    # Delete existing entry first (ignore errors if not found)
    subprocess.run(
        ["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account],
        capture_output=True,
        timeout=5,
    )
    result = subprocess.run(
        ["security", "add-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w", value],
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.returncode == 0


def load_keychain_secrets() -> dict[str, str]:
    """Load all known secrets from macOS Keychain."""
    secrets = {}
    for key in KEYCHAIN_KEYS:
        value = _get_from_keychain(key)
        if value:
            secrets[key] = value
    return secrets


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

    # Tiered models — research brain (expensive, deep analysis + web search)
    # vs compute brain (cheap/fast for number crunching)
    research_model: str = "claude-sonnet-4-6"  # Deep research, web search, outside info
    compute_model: str = "claude-haiku-4-5-20251001"  # Fast + cheap for EV calcs, scoring


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


class CryptoSniperConfig(BaseModel):
    enabled: bool = True
    min_edge: float = 0.08              # 8% minimum edge to trade
    min_price_move_pct: float = 0.002   # 0.2% minimum price move to consider
    max_seconds_before_entry: float = 90  # Only enter with <90s remaining
    symbols: list[str] = Field(default_factory=lambda: ["btcusdt", "ethusdt", "solusdt", "xrpusdt", "dogeusdt"])
    max_position_per_trade: float = 0.05  # 5% of bankroll per snipe
    min_liquidity: float = 500           # Min market liquidity to trade


class WeatherSniperConfig(BaseModel):
    enabled: bool = True
    min_edge: float = 0.10               # 10% minimum edge to trade
    min_confidence: float = 0.60         # Minimum confidence from ensemble
    min_neg_risk_edge: float = 0.03      # 3% for neg-risk arbitrage
    max_position_per_trade: float = 0.08  # 8% of bankroll per weather trade
    min_liquidity: float = 200           # Min market liquidity
    forecast_interval_minutes: int = 30  # How often to refresh forecasts
    locations: list[str] = Field(
        default_factory=lambda: ["nyc", "london", "seoul", "chicago", "miami"]
    )


class MicroSniperConfig(BaseModel):
    enabled: bool = True
    symbols: list[str] = Field(default_factory=lambda: ["btcusdt"])
    entry_threshold: float = 0.55          # Momentum signal threshold to enter (raised from 0.40 — avoid sideways noise)
    counter_trend_threshold: float = 0.70  # Higher bar for entries against 30s trend (kills counter-trend losers)
    exit_threshold: float = 0.20           # Reverse momentum threshold to exit (raised from 0.15 — give trades room)
    hold_threshold: float = 0.10           # Below this (aligned), exit for weak signal
    enable_flips: bool = False             # Flips disabled — marginal profitability, strong reversals just EXIT
    flip_threshold: float = 0.50           # Reverse momentum threshold to flip (only if enable_flips=True)
    flip_min_confidence: float = 0.50      # Min confidence to flip
    min_confidence: float = 0.40           # Min confidence to enter (raised from 0.30)
    min_trades_in_window: int = 10          # Min trades in 15s window to consider (raised — we get 10+ tps now)
    min_trades_for_flip: int = 25          # Min trades in 15s window to flip (higher bar — flips are costly)
    min_seconds_remaining: float = 45.0    # Don't enter with less than this left (raised from 15 — need time to exit)
    force_exit_seconds: float = 8.0        # Force exit with this many seconds left
    min_entry_price: float = 0.20          # Don't buy a side priced below this (raised from 0.15 — stop fighting the market)
    max_entry_price: float = 0.70          # Don't buy a side priced above this (lowered from 0.80 — less overpaying)
    max_position_per_trade: float = 0.03   # 3% of bankroll per micro trade (used if fixed_position_usd is 0)
    fixed_position_usd: float = 10.0       # Fixed $ per trade — simpler than Kelly for micro. 0 = use Kelly sizing
    max_trades_per_window: int = 3         # Max trades in a single 5-min window (reduced from 50 — stop churning)
    min_liquidity: float = 500             # Min market liquidity to trade
    dead_market_band: float = 0.06         # Skip entry when YES is within this band of 0.50 (raised from 0.02 — skip sideways markets)

    # Momentum signal weights — must sum to 1.0
    # Default shifts weight from noisy 5s OFI to more stable 15s/30s windows
    weight_ofi_5s: float = 0.20            # Short-term OFI (most reactive, most noisy)
    weight_ofi_15s: float = 0.40           # Medium-term OFI (more stable)
    weight_vwap_drift: float = 0.25        # VWAP drift signal
    weight_intensity: float = 0.15         # Trade intensity surge


class MarketMakerConfig(BaseModel):
    enabled: bool = False
    min_spread: float = 0.05
    max_inventory_pct: float = 0.20
    requote_threshold: float = 0.02


class StrategiesConfig(BaseModel):
    cheap_hunter: CheapHunterConfig = CheapHunterConfig()
    edge_finder: EdgeFinderConfig = EdgeFinderConfig()
    crypto_sniper: CryptoSniperConfig = CryptoSniperConfig()
    weather_sniper: WeatherSniperConfig = WeatherSniperConfig()
    micro_sniper: MicroSniperConfig = MicroSniperConfig()
    market_maker: MarketMakerConfig = MarketMakerConfig()


class AgentConfig(BaseModel):
    mode: str = "copilot"
    scan_interval_minutes: int = 5
    sync_interval_minutes: int = 15  # How often to re-fetch markets from API
    max_markets_per_scan: int = 20  # Max markets to AI-analyze per cycle
    reanalyze_price_change_pct: float = 0.05


class Settings(BaseSettings):
    # Wallet
    poly_private_key: str = ""
    poly_wallet_address: str = ""
    poly_proxy_address: str = ""  # Polymarket proxy wallet (funder) — needed if using web UI wallet

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
    """Load settings from Keychain → env vars → .env → YAML.

    Priority (highest wins):
      1. macOS Keychain (secrets)
      2. Environment variables
      3. .env file
      4. YAML config (non-secret params only)
      5. Pydantic defaults
    """
    # Load Keychain secrets into env BEFORE Settings() reads them.
    # This way Keychain values override .env but explicit env vars
    # still win (since os.environ is checked first by pydantic-settings).
    keychain_secrets = load_keychain_secrets()
    for key, value in keychain_secrets.items():
        env_key = key.upper()
        if env_key not in os.environ:
            os.environ[env_key] = value

    settings = Settings()

    # Overlay YAML config (non-secret params: strategies, risk, etc.)
    if config_path is None:
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


# --- Database config (portable across environments) ---

# Maps config sections to their Pydantic model classes
_CONFIG_SECTIONS = {
    "risk": RiskConfig,
    "ai": AIConfig,
    "agent": AgentConfig,
    "strategies.cheap_hunter": CheapHunterConfig,
    "strategies.edge_finder": EdgeFinderConfig,
    "strategies.crypto_sniper": CryptoSniperConfig,
    "strategies.weather_sniper": WeatherSniperConfig,
    "strategies.micro_sniper": MicroSniperConfig,
    "strategies.market_maker": MarketMakerConfig,
}


def settings_to_db_dict(settings: Settings) -> dict[str, any]:
    """Flatten settings into namespaced key-value pairs for DB storage.

    Returns dict like {"risk.kelly_fraction": 0.25, "ai.provider": "claude", ...}
    """
    config = {}

    for field, value in settings.risk.model_dump().items():
        config[f"risk.{field}"] = value
    for field, value in settings.ai.model_dump().items():
        config[f"ai.{field}"] = value
    for field, value in settings.agent.model_dump().items():
        config[f"agent.{field}"] = value
    for field, value in settings.strategies.cheap_hunter.model_dump().items():
        config[f"strategies.cheap_hunter.{field}"] = value
    for field, value in settings.strategies.edge_finder.model_dump().items():
        config[f"strategies.edge_finder.{field}"] = value
    for field, value in settings.strategies.crypto_sniper.model_dump().items():
        config[f"strategies.crypto_sniper.{field}"] = value
    for field, value in settings.strategies.weather_sniper.model_dump().items():
        config[f"strategies.weather_sniper.{field}"] = value
    for field, value in settings.strategies.micro_sniper.model_dump().items():
        config[f"strategies.micro_sniper.{field}"] = value
    for field, value in settings.strategies.market_maker.model_dump().items():
        config[f"strategies.market_maker.{field}"] = value

    return config


async def apply_db_config(settings: Settings, db) -> Settings:
    """Overlay database config onto settings. DB values win over YAML/defaults.

    Reads all keys from polyedge.risk_config and applies them to the
    matching Settings fields. Keys use dot notation: "risk.kelly_fraction",
    "ai.provider", "agent.mode", etc.
    """
    db_config = await db.get_all_config()
    if not db_config:
        return settings

    for key, value in db_config.items():
        parts = key.split(".", 1)
        if len(parts) != 2:
            continue

        section, field = parts[0], parts[1]

        # Handle nested strategies (strategies.cheap_hunter.enabled)
        if section == "strategies" and "." in field:
            sub_parts = field.split(".", 1)
            strategy_name, strategy_field = sub_parts[0], sub_parts[1]
            strategy_obj = getattr(settings.strategies, strategy_name, None)
            if strategy_obj and hasattr(strategy_obj, strategy_field):
                setattr(strategy_obj, strategy_field, value)
            continue

        # Handle top-level sections (risk.*, ai.*, agent.*)
        section_obj = getattr(settings, section, None)
        if section_obj and hasattr(section_obj, field):
            setattr(section_obj, field, value)

    return settings


async def save_config_to_db(settings: Settings, db):
    """Write all non-secret settings to the database for portability."""
    config = settings_to_db_dict(settings)
    await db.set_config_bulk(config)
