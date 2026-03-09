"""AI-powered news digestion and sentiment extraction."""

from __future__ import annotations

from polyedge.ai.llm import LLMClient
from polyedge.core.models import Market
from polyedge.data.signals import search_news


NEWS_SUMMARY_SYSTEM = """You are a news analyst for prediction markets. Given a market question and recent news articles, provide a concise summary of the most relevant information that could affect the market outcome.

Focus on:
1. Hard facts and data points
2. Recent developments or changes
3. Expert opinions or official statements
4. Anything that shifts the probability of the outcome

Be brief and factual. No speculation. Output plain text, 2-4 sentences max."""


async def get_news_context(
    llm: LLMClient,
    market: Market,
    news_api_key: str = "",
    max_articles: int = 5,
) -> str:
    """Fetch and summarize relevant news for a market.

    Returns a concise AI-generated summary of relevant news,
    or empty string if no relevant news found.
    """
    # Extract search keywords from market question
    query = _extract_search_query(market.question)

    articles = await search_news(query, api_key=news_api_key, max_results=max_articles)
    if not articles:
        return ""

    # Build news context
    news_text = "\n\n".join(
        f"- [{a['source']}] {a['title']}: {a['description']}"
        for a in articles
        if a.get("title")
    )

    if not news_text:
        return ""

    prompt = (
        f"Market question: {market.question}\n\n"
        f"Recent news articles:\n{news_text}\n\n"
        "Summarize the most relevant information for predicting this market's outcome."
    )

    response = await llm.analyze(
        prompt=prompt,
        system=NEWS_SUMMARY_SYSTEM,
        provider="claude",  # Use Claude for news — better at summarization
    )

    return response.text.strip()


def _extract_search_query(question: str) -> str:
    """Extract key search terms from a market question.

    Removes common prediction market phrasing to get to the core topic.
    """
    # Remove common prefixes
    removals = [
        "Will ", "Will the ", "Is ", "Does ", "Do ", "Has ", "Have ",
        "Can ", "Could ", "Should ", "Would ",
        "by ", "before ", "after ", "in ", "on ", "at ",
    ]
    query = question.rstrip("?")
    for r in removals:
        if query.startswith(r):
            query = query[len(r):]
            break

    # Limit length for search API
    words = query.split()
    if len(words) > 8:
        words = words[:8]

    return " ".join(words)
