"""AI market analyst — estimates probabilities and finds edges."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Optional

from polyedge.ai.llm import LLMClient
from polyedge.core.models import AIAnalysis, Market

SYSTEM_PROMPT = """You are an expert prediction market analyst. Your job is to estimate the TRUE probability of events, independent of what the market currently prices them at.

Rules:
1. Be CALIBRATED. A 70% probability should resolve YES ~70% of the time.
2. Focus on the RESOLUTION CRITERIA, not just the headline question.
3. Consider base rates, recent news, and domain-specific data.
4. Account for uncertainty — if you're unsure, push your estimate toward 50%.
5. Be explicit about what could make you wrong.

Output format (STRICT JSON):
{
  "probability": <float 0-100>,
  "confidence": <float 0-100>,
  "reasoning": "<step-by-step reasoning>",
  "risk_factors": ["<factor 1>", "<factor 2>", ...],
  "key_evidence": "<most important evidence for your estimate>"
}"""


def _build_analysis_prompt(
    market: Market,
    news_context: str = "",
    additional_context: str = "",
) -> str:
    parts = [
        f"## Market Question\n{market.question}",
        f"\n## Description\n{market.description}" if market.description else "",
        f"\n## Current Market Price\nYES: ${market.yes_price:.2f} ({market.yes_price*100:.1f}% implied probability)",
        f"NO: ${market.no_price:.2f}",
    ]

    if market.end_date:
        hours = market.hours_to_resolution
        if hours is not None:
            if hours < 24:
                parts.append(f"\n## Time to Resolution\n{hours:.1f} hours")
            else:
                parts.append(f"\n## Time to Resolution\n{hours/24:.1f} days")

    if market.volume:
        parts.append(f"\n## Market Stats\nVolume: ${market.volume:,.0f} | Liquidity: ${market.liquidity:,.0f}")

    if news_context:
        parts.append(f"\n## Recent News\n{news_context}")

    if additional_context:
        parts.append(f"\n## Additional Context\n{additional_context}")

    parts.append(
        "\n## Your Task\n"
        "Estimate the TRUE probability that this market resolves YES. "
        "Consider the resolution criteria carefully. "
        "Output your analysis as JSON matching the required format."
    )

    return "\n".join(parts)


def _parse_analysis_response(text: str, market: Market, provider: str, model: str, cost: float) -> AIAnalysis:
    """Parse LLM response into structured AIAnalysis."""
    # Try to extract JSON from response
    json_match = re.search(r'\{[^{}]*"probability"[^{}]*\}', text, re.DOTALL)
    if not json_match:
        # Try to find JSON block
        json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
        if json_match:
            json_str = json_match.group(1)
        else:
            # Fallback: try parsing the whole thing
            json_str = text
    else:
        json_str = json_match.group(0)

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        # Last resort: extract numbers from text
        prob_match = re.search(r'probability["\s:]+(\d+\.?\d*)', text, re.IGNORECASE)
        conf_match = re.search(r'confidence["\s:]+(\d+\.?\d*)', text, re.IGNORECASE)
        data = {
            "probability": float(prob_match.group(1)) if prob_match else 50.0,
            "confidence": float(conf_match.group(1)) if conf_match else 30.0,
            "reasoning": text[:500],
            "risk_factors": [],
        }

    probability = float(data.get("probability", 50)) / 100  # Convert to 0-1
    confidence = float(data.get("confidence", 50)) / 100

    # Clamp values
    probability = max(0.01, min(0.99, probability))
    confidence = max(0.1, min(1.0, confidence))

    return AIAnalysis(
        market_id=market.condition_id,
        question=market.question,
        probability=probability,
        confidence=confidence,
        reasoning=data.get("reasoning", ""),
        risk_factors=data.get("risk_factors", []),
        provider=provider,
        model=model,
        cost_usd=cost,
    )


async def analyze_market(
    llm: LLMClient,
    market: Market,
    news_context: str = "",
    additional_context: str = "",
    provider: Optional[str] = None,
) -> AIAnalysis:
    """Use AI to analyze a market and estimate the true probability."""
    prompt = _build_analysis_prompt(market, news_context, additional_context)

    response = await llm.analyze(
        prompt=prompt,
        system=SYSTEM_PROMPT,
        provider=provider,
    )

    analysis = _parse_analysis_response(
        response.text,
        market,
        response.provider,
        response.model,
        response.cost_usd,
    )
    analysis.news_context = news_context
    return analysis


async def batch_analyze(
    llm: LLMClient,
    markets: list[Market],
    news_contexts: Optional[dict[str, str]] = None,
    provider: Optional[str] = None,
) -> list[AIAnalysis]:
    """Analyze multiple markets. Returns analyses sorted by edge size."""
    news_contexts = news_contexts or {}
    analyses = []

    for market in markets:
        try:
            analysis = await analyze_market(
                llm,
                market,
                news_context=news_contexts.get(market.condition_id, ""),
                provider=provider,
            )
            analyses.append(analysis)
        except Exception as e:
            # Log but continue — don't let one failure stop the batch
            analyses.append(
                AIAnalysis(
                    market_id=market.condition_id,
                    question=market.question,
                    probability=market.yes_price,  # Default to market price
                    confidence=0.0,
                    reasoning=f"Analysis failed: {str(e)}",
                    provider="error",
                    model="none",
                )
            )

    return analyses
