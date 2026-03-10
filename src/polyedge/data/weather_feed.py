"""Weather data feed — fetches forecasts from Open-Meteo and NOAA.

Open-Meteo (global, free, no API key):
  - Ensemble forecasts from multiple models (GFS, ECMWF, etc.)
  - Provides natural probability distribution via ensemble spread
  - Up to 10,000 calls/day on free tier

NOAA (US only, free, matches Polymarket resolution source):
  - Official US government weather data
  - Polymarket explicitly resolves US weather markets against NOAA
  - No API key needed (just User-Agent header)

The ensemble approach is key: Open-Meteo provides multiple model runs for each
forecast day.  If 35 out of 50 ensemble members predict a high temp of 45-50°F,
that's a 70% probability for that bucket — no AI needed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

import aiohttp

logger = logging.getLogger("polyedge.weather_feed")

# --- Location Registry ---
# Cities with active weather markets on Polymarket.
# Coordinates, timezone, and resolution source for each.

LOCATIONS: dict[str, dict] = {
    "nyc": {
        "name": "New York City",
        "lat": 40.7128,
        "lon": -74.0060,
        "timezone": "America/New_York",
        "resolution_source": "noaa",
        "aliases": ["new york", "nyc", "new york city", "manhattan"],
    },
    "london": {
        "name": "London",
        "lat": 51.5074,
        "lon": -0.1278,
        "timezone": "Europe/London",
        "resolution_source": "wunderground",
        "aliases": ["london"],
    },
    "seoul": {
        "name": "Seoul",
        "lat": 37.5665,
        "lon": 126.978,
        "timezone": "Asia/Seoul",
        "resolution_source": "open_meteo",
        "aliases": ["seoul"],
    },
    "chicago": {
        "name": "Chicago",
        "lat": 41.8781,
        "lon": -87.6298,
        "timezone": "America/Chicago",
        "resolution_source": "noaa",
        "aliases": ["chicago"],
    },
    "miami": {
        "name": "Miami",
        "lat": 25.7617,
        "lon": -80.1918,
        "timezone": "America/New_York",
        "resolution_source": "noaa",
        "aliases": ["miami"],
    },
    "la": {
        "name": "Los Angeles",
        "lat": 34.0522,
        "lon": -118.2437,
        "timezone": "America/Los_Angeles",
        "resolution_source": "noaa",
        "aliases": ["los angeles", "la", "l.a."],
    },
    "seattle": {
        "name": "Seattle",
        "lat": 47.6062,
        "lon": -122.3321,
        "timezone": "America/Los_Angeles",
        "resolution_source": "noaa",
        "aliases": ["seattle"],
    },
}


def find_location(text: str) -> Optional[str]:
    """Match text to a known location ID."""
    text_lower = text.lower()
    for loc_id, loc in LOCATIONS.items():
        for alias in loc["aliases"]:
            if alias in text_lower:
                return loc_id
    return None


# --- Forecast Data Models ---


@dataclass
class EnsembleForecast:
    """Ensemble forecast for a single day and location.

    The ensemble_temps list contains one temperature value per model run.
    The natural spread of these values gives us a probability distribution.
    """
    location_id: str
    target_date: date
    metric: str  # "temperature_max", "temperature_min", "precipitation"

    # Ensemble values (one per model run)
    ensemble_values: list[float] = field(default_factory=list)

    # Summary stats
    mean: float = 0.0
    std: float = 0.0
    min_val: float = 0.0
    max_val: float = 0.0

    # Metadata
    source: str = "open_meteo"
    model: str = "gfs_seamless"
    fetched_at: float = 0.0  # Unix timestamp
    unit: str = "°F"

    @property
    def n_members(self) -> int:
        return len(self.ensemble_values)

    def probability_in_range(self, low: float, high: float) -> float:
        """Compute probability that actual value falls in [low, high].

        Simply counts what fraction of ensemble members fall in range.
        This is the core probability engine — no AI needed.
        """
        if not self.ensemble_values:
            return 0.0
        count = sum(1 for v in self.ensemble_values if low <= v < high)
        return count / len(self.ensemble_values)

    def probability_above(self, threshold: float) -> float:
        """Probability that value exceeds threshold."""
        if not self.ensemble_values:
            return 0.0
        count = sum(1 for v in self.ensemble_values if v >= threshold)
        return count / len(self.ensemble_values)

    def probability_below(self, threshold: float) -> float:
        """Probability that value is below threshold."""
        if not self.ensemble_values:
            return 0.0
        count = sum(1 for v in self.ensemble_values if v < threshold)
        return count / len(self.ensemble_values)


@dataclass
class NOAAForecast:
    """NOAA point forecast (single value, not ensemble)."""
    location_id: str
    target_date: date
    high_temp: Optional[float] = None  # °F
    low_temp: Optional[float] = None   # °F
    precip_chance: Optional[float] = None  # 0-100
    precip_amount: Optional[float] = None  # inches
    wind_speed: Optional[float] = None  # mph
    short_forecast: str = ""
    fetched_at: float = 0.0


# --- Weather Feed ---


class WeatherFeed:
    """Fetches and caches weather forecasts from Open-Meteo and NOAA."""

    OPEN_METEO_ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
    NOAA_POINTS_URL = "https://api.weather.gov/points"

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None

        # Cache: (location_id, date, metric) -> EnsembleForecast
        self._ensemble_cache: dict[tuple[str, date, str], EnsembleForecast] = {}
        # Cache: (location_id, date) -> NOAAForecast
        self._noaa_cache: dict[tuple[str, date], NOAAForecast] = {}
        # NOAA grid lookups: location_id -> (office, gridX, gridY)
        self._noaa_grids: dict[str, tuple[str, int, int]] = {}

        self._cache_ttl = 1800  # 30 minutes

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": "PolyEdge/1.0 (weather trading bot)"}
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # --- Open-Meteo Ensemble ---

    async def get_ensemble_forecast(
        self,
        location_id: str,
        target_date: date,
        metric: str = "temperature_max",
    ) -> Optional[EnsembleForecast]:
        """Fetch ensemble forecast from Open-Meteo.

        Returns multiple model runs for natural probability estimation.

        Args:
            location_id: Key from LOCATIONS dict
            target_date: The date to forecast
            metric: "temperature_max", "temperature_min", or "precipitation"
        """
        cache_key = (location_id, target_date, metric)
        cached = self._ensemble_cache.get(cache_key)
        if cached and (time.time() - cached.fetched_at) < self._cache_ttl:
            return cached

        loc = LOCATIONS.get(location_id)
        if not loc:
            logger.warning(f"Unknown location: {location_id}")
            return None

        # Map our metric names to Open-Meteo API variables
        metric_map = {
            "temperature_max": "temperature_2m_max",
            "temperature_min": "temperature_2m_min",
            "precipitation": "precipitation_sum",
        }
        api_var = metric_map.get(metric)
        if not api_var:
            logger.warning(f"Unknown metric: {metric}")
            return None

        # Determine days ahead
        today = date.today()
        days_ahead = (target_date - today).days
        if days_ahead < 0:
            return None  # Can't forecast the past
        forecast_days = max(days_ahead + 1, 2)  # Need at least 2 for the API

        params = {
            "latitude": loc["lat"],
            "longitude": loc["lon"],
            "daily": api_var,
            "models": "gfs_seamless",
            "temperature_unit": "fahrenheit",
            "precipitation_unit": "inch",
            "forecast_days": min(forecast_days, 16),
        }

        try:
            session = await self._get_session()
            async with session.get(self.OPEN_METEO_ENSEMBLE_URL, params=params) as resp:
                if resp.status != 200:
                    logger.warning(f"Open-Meteo error {resp.status} for {location_id}")
                    return None
                data = await resp.json()
        except Exception as e:
            logger.error(f"Open-Meteo request failed: {e}")
            return None

        return self._parse_ensemble_response(data, location_id, target_date, metric, api_var)

    def _parse_ensemble_response(
        self,
        data: dict,
        location_id: str,
        target_date: date,
        metric: str,
        api_var: str,
    ) -> Optional[EnsembleForecast]:
        """Parse Open-Meteo ensemble response into EnsembleForecast."""
        daily = data.get("daily", {})
        times = daily.get("time", [])
        target_str = target_date.isoformat()

        # Find index for our target date
        date_idx = None
        for i, t in enumerate(times):
            if t == target_str:
                date_idx = i
                break

        if date_idx is None:
            logger.debug(f"Target date {target_str} not in forecast range")
            return None

        # Collect ensemble member values for this date
        # Open-Meteo returns ensemble members as separate keys:
        # temperature_2m_max_member01, temperature_2m_max_member02, ...
        ensemble_values = []

        for key, values in daily.items():
            if key.startswith(api_var + "_member") and isinstance(values, list):
                if date_idx < len(values) and values[date_idx] is not None:
                    ensemble_values.append(float(values[date_idx]))

        # Also check the main variable (the ensemble mean)
        main_values = daily.get(api_var)
        if not ensemble_values and main_values and date_idx < len(main_values):
            # No ensemble members — just have the mean
            # Create a synthetic spread using typical forecast uncertainty
            mean_val = float(main_values[date_idx])
            days_ahead = (target_date - date.today()).days
            # Uncertainty grows with forecast horizon
            uncertainty = 2.0 + days_ahead * 0.8  # °F
            if metric == "precipitation":
                uncertainty = 0.1 + days_ahead * 0.05  # inches

            import random
            random.seed(int(target_date.toordinal()) + hash(location_id))
            ensemble_values = [
                mean_val + random.gauss(0, uncertainty) for _ in range(30)
            ]

        if not ensemble_values:
            return None

        import statistics
        unit = "inches" if metric == "precipitation" else "°F"

        forecast = EnsembleForecast(
            location_id=location_id,
            target_date=target_date,
            metric=metric,
            ensemble_values=ensemble_values,
            mean=statistics.mean(ensemble_values),
            std=statistics.stdev(ensemble_values) if len(ensemble_values) > 1 else 0,
            min_val=min(ensemble_values),
            max_val=max(ensemble_values),
            source="open_meteo",
            model="gfs_seamless",
            fetched_at=time.time(),
            unit=unit,
        )

        self._ensemble_cache[(location_id, target_date, metric)] = forecast
        return forecast

    # --- NOAA Forecast ---

    async def get_noaa_forecast(
        self,
        location_id: str,
        target_date: date,
    ) -> Optional[NOAAForecast]:
        """Fetch official NOAA forecast. US locations only.

        Matches Polymarket's resolution source for US weather markets.
        """
        cache_key = (location_id, target_date)
        cached = self._noaa_cache.get(cache_key)
        if cached and (time.time() - cached.fetched_at) < self._cache_ttl:
            return cached

        loc = LOCATIONS.get(location_id)
        if not loc or loc.get("resolution_source") != "noaa":
            return None

        # Step 1: Get grid info (cached)
        grid = await self._get_noaa_grid(location_id)
        if not grid:
            return None

        office, grid_x, grid_y = grid

        # Step 2: Fetch forecast
        forecast_url = f"https://api.weather.gov/gridpoints/{office}/{grid_x},{grid_y}/forecast"

        try:
            session = await self._get_session()
            async with session.get(forecast_url) as resp:
                if resp.status != 200:
                    logger.warning(f"NOAA forecast error {resp.status} for {location_id}")
                    return None
                data = await resp.json()
        except Exception as e:
            logger.error(f"NOAA forecast request failed: {e}")
            return None

        return self._parse_noaa_forecast(data, location_id, target_date)

    async def _get_noaa_grid(self, location_id: str) -> Optional[tuple[str, int, int]]:
        """Look up NOAA grid coordinates for a location. Cached."""
        if location_id in self._noaa_grids:
            return self._noaa_grids[location_id]

        loc = LOCATIONS.get(location_id)
        if not loc:
            return None

        points_url = f"{self.NOAA_POINTS_URL}/{loc['lat']},{loc['lon']}"

        try:
            session = await self._get_session()
            async with session.get(points_url) as resp:
                if resp.status != 200:
                    logger.warning(f"NOAA points error {resp.status} for {location_id}")
                    return None
                data = await resp.json()
        except Exception as e:
            logger.error(f"NOAA points request failed: {e}")
            return None

        props = data.get("properties", {})
        office = props.get("gridId")
        grid_x = props.get("gridX")
        grid_y = props.get("gridY")

        if not all([office, grid_x is not None, grid_y is not None]):
            return None

        grid = (office, int(grid_x), int(grid_y))
        self._noaa_grids[location_id] = grid
        return grid

    def _parse_noaa_forecast(
        self,
        data: dict,
        location_id: str,
        target_date: date,
    ) -> Optional[NOAAForecast]:
        """Parse NOAA forecast response."""
        periods = data.get("properties", {}).get("periods", [])
        if not periods:
            return None

        target_str = target_date.isoformat()
        high_temp = None
        low_temp = None
        precip_chance = None
        short_forecast = ""

        for period in periods:
            # Match by date in the startTime field
            start = period.get("startTime", "")
            if target_str not in start:
                continue

            temp = period.get("temperature")
            is_daytime = period.get("isDaytime", True)

            if is_daytime and temp is not None:
                high_temp = float(temp)
                short_forecast = period.get("shortForecast", "")
                precip_val = period.get("probabilityOfPrecipitation", {})
                if isinstance(precip_val, dict):
                    precip_chance = precip_val.get("value")
            elif not is_daytime and temp is not None:
                low_temp = float(temp)

        if high_temp is None and low_temp is None:
            return None

        forecast = NOAAForecast(
            location_id=location_id,
            target_date=target_date,
            high_temp=high_temp,
            low_temp=low_temp,
            precip_chance=precip_chance,
            short_forecast=short_forecast,
            fetched_at=time.time(),
        )

        self._noaa_cache[(location_id, target_date)] = forecast
        return forecast

    # --- Combined ---

    async def get_forecast(
        self,
        location_id: str,
        target_date: date,
        metric: str = "temperature_max",
    ) -> Optional[EnsembleForecast]:
        """Get the best available forecast for a location and date.

        Tries Open-Meteo ensemble first (gives probability distribution).
        For US locations, also fetches NOAA as a reference point.
        """
        ensemble = await self.get_ensemble_forecast(location_id, target_date, metric)

        # For US locations, also get NOAA for cross-reference
        loc = LOCATIONS.get(location_id, {})
        if loc.get("resolution_source") == "noaa":
            noaa = await self.get_noaa_forecast(location_id, target_date)
            if noaa and ensemble:
                logger.debug(
                    f"NOAA {location_id} {target_date}: "
                    f"high={noaa.high_temp}°F, "
                    f"ensemble mean={ensemble.mean:.1f}°F"
                )

        return ensemble

    def clear_cache(self):
        """Clear all cached forecasts."""
        self._ensemble_cache.clear()
        self._noaa_cache.clear()
