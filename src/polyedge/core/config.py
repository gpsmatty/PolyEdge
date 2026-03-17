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
    "do_api_token",
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
    counter_trend_threshold: float = 0.58  # Higher bar for entries against 30s trend (kills counter-trend losers)
    exit_threshold: float = 0.30           # Reverse momentum threshold to exit
    counter_trend_exit_threshold: float = 0.45  # Higher bar for exits when 30s trend still agrees with position
    hold_threshold: float = 0.0            # Disabled — trailing stop + exit_threshold handle exits
    enable_flips: bool = False             # Flips disabled — marginal profitability, strong reversals just EXIT
    flip_threshold: float = 0.50           # Reverse momentum threshold to flip (only if enable_flips=True)
    flip_min_confidence: float = 0.50      # Min confidence to flip
    flip_min_hold_seconds: float = 45.0    # After a flip, require flip_threshold momentum to exit for this many seconds
    min_confidence: float = 0.40           # Min confidence to enter (raised from 0.30)
    min_trades_in_window: int = 10          # Min trades in 15s window to consider (raised — we get 10+ tps now)
    min_trades_for_flip: int = 25          # Min trades in 15s window to flip (higher bar — flips are costly)
    min_seconds_remaining: float = 200.0   # Don't enter with less than this left in 15m windows
    force_exit_seconds: float = 8.0        # Force exit with this many seconds left
    min_entry_price: float = 0.35          # Don't buy a side priced below this
    max_entry_price: float = 0.58          # Don't buy a side priced above this
    max_position_per_trade: float = 0.03   # 3% of bankroll per micro trade (used if fixed_position_usd is 0)
    fixed_position_usd: float = 5.0        # Fixed $ per trade — simpler than Kelly for micro. 0 = use Kelly sizing
    max_trades_per_window: int = 10        # Max trades in a single window
    min_liquidity: float = 500             # Min market liquidity to trade
    dead_market_band: float = 0.02         # Skip entry when YES is within this band of 0.50

    # Momentum signal weights — must sum to 1.0
    # Default shifts weight from noisy 5s OFI to more stable 15s/30s windows
    weight_ofi_5s: float = 0.10            # Short-term OFI (most reactive, most noisy)
    weight_ofi_15s: float = 0.50           # Medium-term OFI (most reliable signal)
    weight_vwap_drift: float = 0.25        # VWAP drift signal
    weight_intensity: float = 0.15         # Trade intensity surge

    # --- Score shaping (previously hardcoded) ---
    # VWAP drift scaling: raw drift is tiny (0.0001 = $7 on BTC). This multiplier
    # maps it to [-1, 1]. Higher = more sensitive to small moves.
    # At 2000: ~$35 BTC move maxes the signal. At 5000: ~$14 maxes it (too noisy).
    vwap_drift_scale: float = 2000.0

    # Flow-price agreement dampener: penalizes the momentum score when OFI
    # direction and actual price movement don't align. "Aggressive flow that
    # doesn't move price was absorbed by the book — not a real signal."
    # Continuous: factor = disagree + (agree - disagree) * alignment^2
    # where alignment is 0 (fully opposed) to 1 (fully aligned).
    dampener_agree_factor: float = 1.0     # Max factor when flow and price fully agree
    dampener_disagree_factor: float = 0.4  # Min factor when flow and price fully oppose
    dampener_flat_factor: float = 0.65     # Factor when OFI present but price flat (no drift)
    dampener_price_deadzone: float = 0.05  # Scaled drift_signal below this = "price flat"

    # Slippage: how many cents above market to bid for instant fill
    entry_slippage: float = 0.02           # Pay up to 2c more for entry FOK fill
    entry_slippage_retry_step: float = 0.02  # Each FOK retry adds this much slippage (0 = no escalation)
    entry_slippage_max: float = 0.10       # Max slippage cap across all retries
    exit_slippage: float = 0.05            # Sell up to 5c below market for exit FOK fill (wider = fills on first try)
    exit_slippage_retry_step: float = 0.03  # Each failed FOK sell adds this to the floor (was hardcoded 0.03)
    exit_slippage_max: float = 0.15        # Hard cap on total exit slippage — prevents runaway floor on fast crashes

    # Trade cooldown: seconds between trades on the same market.
    # Prevents whipsaw — trading the same market twice during noisy bounces.
    trade_cooldown: float = 30.0           # 30s between trades on same condition_id

    # Window hop cooldown: seconds to wait after hopping to a new window
    # before allowing entries. Lets stale cross-window momentum flush out
    # and fresh OFI data build up for the new window.
    window_hop_cooldown: float = 30.0      # Wait 30s after hop before trading

    # --- Polymarket order book integration ---
    # When enabled, reads the live Polymarket order book via WebSocket
    # for two features:
    # 1. Exit liquidity check — blocks entry if bid depth is too thin to exit
    # 2. Book imbalance signal — Polymarket bid/ask imbalance as a tiebreaker
    poly_book_enabled: bool = False         # Master toggle — OFF by default
    poly_book_min_exit_depth: float = 20.0  # Min bid depth (contracts within 5c) to enter — ensures we can exit
    poly_book_imbalance_weight: float = 0.15  # Weight of Poly book imbalance in momentum composite (steals from OFI 5s)
    poly_book_imbalance_veto: float = -0.40   # If Poly book imbalance is this negative against our direction, block entry

    # Book-based exit override — when momentum says "exit" but the Poly book
    # disagrees, hold the position. Requires BOTH depth AND imbalance to override.
    poly_book_exit_override_depth: float = 25.0   # Hold if our token's bid depth > this (strong exit liquidity = market believes in our side)
    poly_book_exit_override_imbalance: float = -0.05  # Hold if directional imbalance > this (book sentiment favors our position)

    # --- Entry persistence filter ---
    # Requires momentum to stay above entry threshold for a continuous duration
    # before actually entering. Kills single-spike noise entries.
    # Time-based: momentum must persist for N seconds (not just N evals).
    entry_persistence_enabled: bool = True        # Filter out sub-second momentum spikes
    entry_persistence_count: int = 3              # DEPRECATED: use entry_persistence_seconds
    entry_persistence_seconds: float = 2.0        # Momentum must hold for 2s before entry

    # --- Persistent trend context ---
    # Uses a 5-minute rolling window + DB price history to know the macro
    # trend. Blocks entries against the trend (e.g., don't short into a rally).
    # This is the "persistence" feature: cross-window, cross-restart awareness.
    trend_bias_enabled: bool = True              # Master toggle for trend bias veto
    trend_bias_min_pct: float = 0.001            # 0.10% move over 5 min to consider "trending"
    trend_bias_strong_pct: float = 0.002         # 0.20% move = strong trend, block counter-trend entirely
    trend_bias_counter_boost: float = 0.10       # Add this to entry_threshold for counter-trend entries (when between min and strong)
    trend_log_interval: float = 30.0             # Seconds between DB price log snapshots
    trend_warmup_seconds: float = 60.0           # Seconds of live data needed before trend is trusted

    # --- Adaptive directional bias ---
    # Shifts entry thresholds based on 30m macro trend. In a bearish market,
    # YES entries need a stronger signal (threshold + spread/2), while NO
    # entries get an easier bar (threshold - spread/2). Flips for bullish.
    # Uses price_history from micro_price_log DB table (30min of snapshots).
    adaptive_bias_enabled: bool = True            # Master toggle for adaptive per-side bias
    adaptive_bias_spread: float = 0.10            # Total spread: favorable side gets -spread/2, unfavorable +spread/2
    adaptive_bias_lookback_minutes: float = 15.0  # How far back to compute the macro trend
    adaptive_bias_min_move: float = 0.003         # 0.30% min move to trigger bias (below = neutral, no adjustment)

    # --- Low volatility regime blocker ---
    # When market is dead (low trade intensity + tight price range), momentum
    # signals are pure noise. Data shows 33% win rate, -18.68% avg move.
    # Block entries entirely when the regime classifier tags "low_vol".
    low_vol_block_enabled: bool = True            # Master toggle for low_vol entry block
    low_vol_max_intensity: float = 3.5            # Max trades/sec (30s window) to be "low vol"
    low_vol_max_price_change: float = 0.0005      # Max abs price change (fractional) to be "low vol"

    # --- High intensity blocker ---
    # Data shows losers avg ~61 tps vs winners ~40 tps. High intensity =
    # chaotic price action where momentum signals are unreliable. Block entries
    # when 30s trade intensity exceeds the cap.
    high_intensity_block_enabled: bool = True     # Master toggle
    high_intensity_max_tps: float = 50.0          # Block entries above this tps (30s window). Data: winners avg 40, losers avg 61

    # --- Chop filter (volatility-scaled threshold) ---
    # Auto-raises entry threshold when market is choppy (big range, no direction).
    # Uses 5-minute price range vs net movement. Adapts automatically — no manual
    # config changes needed across market conditions.
    chop_filter_enabled: bool = True              # Master toggle
    chop_threshold: float = 5.0                   # Chop index above this = choppy (range/net_move ratio)
    chop_max_boost: float = 0.10                  # Max threshold boost in extreme chop
    chop_scale: float = 5.0                       # Chop index at which max_boost is fully applied

    # --- Trailing stop loss ---
    # Tracks the high water mark (HWM) of our side's price since entry.
    # If price drops trailing_stop_pct from the HWM, trigger an exit.
    # Time-scaled: wider early in the window, tighter late.
    trailing_stop_enabled: bool = True             # Locks in profits on winners
    trailing_stop_pct: float = 0.18                # Base trailing stop: exit if price drops 18% from HWM
    trailing_stop_min_profit_pct: float = 0.10     # Only arm the stop after price is 10% above entry (don't stop out on noise)
    trailing_stop_late_pct: float = 0.15           # Tighter stop in last 90s of window
    trailing_stop_late_seconds: float = 90.0       # When to switch to the tighter stop

    # --- Acceleration filter ---
    # Only enter when momentum is still building, not fading from a spike.
    # Tolerance = how much momentum can fade between ticks and still pass.
    # 0.05 = strict (must be accelerating), 0.15 = loose (allows sustained signals)
    acceleration_enabled: bool = True              # Master toggle for acceleration filter
    acceleration_tolerance: float = 0.15           # Noise tolerance for acceleration check

    # Take-profit: exit immediately when market price hits target
    take_profit_enabled: bool = True               # Master toggle
    take_profit_price: float = 0.90                # Exit when our token price >= this

    # Max loss stop: exit if position loses X% from entry, regardless of momentum.
    # Protects against trades that immediately go wrong and never recover.
    max_loss_pct: float = 0.35                     # Exit if token drops 35% from entry price (0 = disabled)

    # Sell-into-strength: only sell when profitable AND momentum agrees with position.
    # Holds through reversals (no panic selling). Only exits on: take_profit, max_loss,
    # force_exit (window end), or sell_into_strength conditions met.
    sell_into_strength_enabled: bool = True         # Master toggle (False = old reversal-exit mode)
    sell_min_profit_pct: float = 0.05              # Min profit above entry to allow sell (5%)
    sell_momentum_agreement: float = 0.20          # Min momentum in our direction to trigger sell

    # --- Binance order book depth integration ---
    # Uses @depth20@100ms stream for LEADING indicators (limit order changes).
    # Unlike aggTrade (lagging — past trades), depth shows intent (orders being
    # placed/pulled) BEFORE price moves. The key metric is imbalance velocity:
    # how fast the book is tilting toward one side.
    depth_enabled: bool = True                     # Master toggle — uses order book depth (leading indicator)
    depth_weight_imbalance_velocity: float = 0.50  # Imbalance velocity (the leading signal)
    depth_weight_depth_delta: float = 0.30         # Bid/ask depth growth rate
    depth_weight_large_order: float = 0.20         # Sudden large order detection
    depth_imbalance_levels: int = 5                # Number of levels for near-touch imbalance
    depth_velocity_window_s: float = 3.0           # Primary velocity window in seconds
    depth_large_order_threshold: float = 3.0       # Multiple of mean level size to flag as "large"
    depth_signal_weight: float = 1.0               # Weight of depth_momentum in final composite (1.0 = depth only)
    depth_aggtrade_weight: float = 0.0             # Weight of existing momentum_signal (0 = disabled)
    depth_velocity_scale: float = 2.0              # Multiplier to normalize velocity into [-1,1] range
    depth_pull_scale: float = 5.0                  # Multiplier to scale pull signal (pulls are small %)
    depth_confidence_weight_agreement: float = 0.4 # Confidence: weight of sub-signal agreement
    depth_confidence_weight_strength: float = 0.4  # Confidence: weight of signal magnitude
    depth_confidence_weight_data: float = 0.2      # Confidence: weight of data sufficiency
    depth_confidence_min_snapshots: int = 10        # Min snapshots before confidence > 0
    depth_confidence_data_ok_snapshots: int = 30    # Snapshots at which data_ok = 1.0
    depth_gap_clear_seconds: float = 2.0           # Seconds of gap before clearing depth history
    depth_max_snapshots: int = 200                  # Max snapshot history (100ms each, 200 = 20s)

    # --- Per-timeframe overrides ---
    # Maps timeframe keys ("5m", "15m", "1h", "1d") to partial config dicts.
    # Only override fields that differ from the base config above.
    # Example: {"15m": {"entry_threshold": 0.55, "max_trades_per_window": 8}}
    # At runtime, the runner merges base + timeframe overrides for the active window.
    # Set via DB: strategies.micro_sniper.timeframes.15m.entry_threshold = 0.55
    timeframes: dict[str, dict] = Field(default_factory=dict)

    def for_timeframe(self, duration_minutes: int | None) -> "MicroSniperConfig":
        """Return a merged config for the given timeframe.

        If duration_minutes matches a key in self.timeframes (e.g., 15 -> "15m",
        60 -> "1h", 1440 -> "1d"), overlay those overrides onto the base config.
        If no match or no overrides, returns self unchanged.
        """
        if not duration_minutes or not self.timeframes:
            return self

        # Build timeframe key: 5->5m, 15->15m, 60->1h, 1440->1d
        if duration_minutes >= 1440:
            tf_key = f"{duration_minutes // 1440}d"
        elif duration_minutes >= 60:
            tf_key = f"{duration_minutes // 60}h"
        else:
            tf_key = f"{duration_minutes}m"

        overrides = self.timeframes.get(tf_key)
        if not overrides:
            return self

        # Merge: base config dict + overrides (overrides win)
        base = self.model_dump()
        base.pop("timeframes", None)  # Don't nest timeframes inside the merged config
        base.update(overrides)
        merged = MicroSniperConfig(**base)
        # Preserve the timeframes dict so hot-reload keeps working
        merged.timeframes = self.timeframes
        return merged


class MarketMakerConfig(BaseModel):
    """Market maker strategy config — post-only limit orders, zero taker fees.

    Pure spread capture on ANY Polymarket market. Fair value from Poly book
    midpoint. Defense from book dynamics (imbalance velocity, whale detection).
    Optional Binance depth defense for crypto markets.
    """

    enabled: bool = False

    # --- Market selection ---
    symbols: list[str] = ["btcusdt"]  # Crypto mode: Binance symbols
    condition_ids: list[str] = []  # Static mode: direct condition_id targeting
    min_liquidity: float = 500.0
    min_entry_price: float = 0.35  # Don't buy below 35c — deep OTM likely resolves to 0
    max_entry_price: float = 0.65  # Don't buy above 65c — overpaying near certainty
    min_seconds_remaining: float = 120.0  # Suppress new bids with <N seconds left

    # --- Warmup ---
    warmup_seconds: float = 5.0  # Wait for book data before first quote

    # --- Spread & pricing ---
    min_spread: float = 0.04  # Minimum full spread (bid-ask gap)
    base_spread: float = 0.06  # Normal calm-market spread
    max_spread: float = 0.12  # Widest allowed spread
    time_decay_widen_seconds: float = 60.0  # Widen in last N seconds of window
    time_decay_spread_mult: float = 1.5  # Spread multiplier during time decay

    # --- Sizing ---
    quote_size_usd: float = 3.0  # USD per side per quote level
    max_inventory_usd: float = 15.0  # Max total inventory both sides
    max_inventory_imbalance: float = 0.70  # Max fraction on one side (0.5=balanced)
    inventory_skew_factor: float = 0.02  # Price offset per unit of net exposure

    # --- Quote management ---
    requote_threshold: float = 0.01  # Requote when fair value moves 1 cent
    min_requote_interval: float = 3.0  # Min seconds between requotes
    cancel_before_requote: bool = True
    use_gtd: bool = True  # Auto-expire orders at window end
    gtd_buffer_seconds: float = 10.0  # Expire orders N seconds before window end

    # --- Heartbeat (dead-man switch) ---
    heartbeat_enabled: bool = True
    heartbeat_interval_seconds: float = 5.0  # Must be <10s

    # --- Poly book defense ---
    min_profitable_spread_bps: float = 100.0  # Don't quote if market spread < 1c
    adverse_selection_threshold: float = 0.70  # Pull if |imbalance_5c| > this
    imbalance_velocity_pull_threshold: float = 0.15  # Pull when imbalance changes this fast/sec
    whale_widen_factor: float = 1.5  # Widen spread when whale near our price

    # --- Binance depth defense (crypto only) ---
    depth_defense_enabled: bool = False  # Only for crypto window markets
    depth_pull_threshold: float = 0.80  # Pull when Binance depth momentum > this
    depth_recovery_seconds: float = 3.0  # Wait after pull before re-quoting

    # --- Profit targeting ---
    min_profit_pct: float = 0.20  # Don't sell until 20% above avg cost
    profit_decay_start_seconds: float = 300.0  # Start decaying profit floor at 5 min remaining
    force_sell_seconds: float = 60.0  # Force-sell mode in last N seconds
    force_sell_fire_sale_seconds: float = 5.0  # Fire sale at any price in last N seconds

    # --- Risk ---
    max_loss_per_window_usd: float = 2.0  # Stop quoting if net loss exceeds this
    max_open_orders: int = 8  # Max simultaneous open orders
    window_hop_pause_seconds: float = 15.0  # Pause quoting after window hop

    # --- Fill tracking ---
    fill_check_interval_seconds: float = 3.0  # Poll CLOB API for fills
    balance_reconcile_interval: float = 60.0  # Belt-and-suspenders balance check


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
    do_api_token: str = ""

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
        if field == "timeframes" and isinstance(value, dict):
            # Serialize per-timeframe overrides as nested dot keys:
            # strategies.micro_sniper.timeframes.15m.entry_threshold = 0.55
            for tf_key, tf_overrides in value.items():
                if isinstance(tf_overrides, dict):
                    for tf_field, tf_value in tf_overrides.items():
                        config[f"strategies.micro_sniper.timeframes.{tf_key}.{tf_field}"] = tf_value
            continue
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
            if not strategy_obj:
                continue

            # Handle per-timeframe overrides:
            # strategies.micro_sniper.timeframes.15m.entry_threshold = 0.55
            if strategy_field.startswith("timeframes.") and hasattr(strategy_obj, "timeframes"):
                tf_parts = strategy_field.split(".", 2)  # ["timeframes", "15m", "entry_threshold"]
                if len(tf_parts) == 3:
                    _, tf_key, tf_field = tf_parts
                    if tf_key not in strategy_obj.timeframes:
                        strategy_obj.timeframes[tf_key] = {}
                    strategy_obj.timeframes[tf_key][tf_field] = value
                continue

            if hasattr(strategy_obj, strategy_field):
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
