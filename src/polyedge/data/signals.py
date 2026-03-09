"""External data sources for market analysis."""

from __future__ import annotations

from typing import Optional

import aiohttp

from polyedge.core.config import Settings


async def search_news(query: str, api_key: str = "", max_results: int = 5) -> list[dict]:
    """Search for recent news articles related to a market question.

    Uses NewsAPI.org if API key is available, otherwise returns empty.
    """
    if not api_key:
        return []

    url = "https://newsapi.org/v2/everything"
    params = {
        "q": query,
        "sortBy": "publishedAt",
        "pageSize": max_results,
        "language": "en",
        "apiKey": api_key,
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            articles = data.get("articles", [])
            return [
                {
                    "title": a.get("title", ""),
                    "description": a.get("description", ""),
                    "source": a.get("source", {}).get("name", ""),
                    "url": a.get("url", ""),
                    "published_at": a.get("publishedAt", ""),
                }
                for a in articles
            ]


async def get_weather_forecast(
    location: str,
    settings: Settings,
) -> Optional[dict]:
    """Get weather forecast from NOAA API (free, no key needed).

    Returns forecast data for US locations.
    """
    # First, geocode the location to get grid coordinates
    url = f"https://api.weather.gov/points/{location}"
    headers = {"User-Agent": "PolyEdge/0.1 (trading bot)"}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()

        forecast_url = data.get("properties", {}).get("forecast")
        if not forecast_url:
            return None

        async with session.get(forecast_url, headers=headers) as resp:
            if resp.status != 200:
                return None
            return await resp.json()


async def get_sports_odds(
    sport: str = "upcoming",
    api_key: str = "",
) -> list[dict]:
    """Get sports odds from the-odds-api.com.

    Useful for comparing sports prediction market prices to bookmaker odds.
    """
    if not api_key:
        return []

    url = f"https://api.the-odds-api.com/v4/sports/{sport}/odds"
    params = {
        "apiKey": api_key,
        "regions": "us",
        "markets": "h2h",
        "oddsFormat": "decimal",
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                return []
            return await resp.json()


async def get_polling_data(query: str) -> list[dict]:
    """Scrape or fetch polling data for political markets.

    This is a placeholder — real implementation would scrape
    FiveThirtyEight, RealClearPolitics, etc.
    """
    # TODO: Implement polling data fetching
    return []
