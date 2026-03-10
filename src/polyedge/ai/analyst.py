"""AI market analyst — estimates probabilities and finds edges."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Optional

from polyedge.ai.llm import LLMClient
from polyedge.core.models import AIAnalysis, Market

SYSTEM_PROMPT = """You are an expert prediction market analyst. Your job is to estimate the TRUE probability of events and identify MISPRICINGS — cases where the market price is meaningfully wrong.

CRITICAL PRIOR: Prediction markets are generally well-calibrated. The market price reflects the aggregated wisdom of many informed traders with real money at stake. You need STRONG, SPECIFIC evidence to disagree with the market by more than 5%. Vague reasoning or "gut feeling" is not sufficient.

Rules:
1. Be CALIBRATED. A 70% probability should resolve YES ~70% of the time.
2. Focus on the RESOLUTION CRITERIA, not just the headline question.
3. Consider base rates, recent news, and domain-specific data.
4. Account for uncertainty — if you're unsure, your estimate should be CLOSE TO the market price, not pushed to 50%.
5. Be explicit about what could make you wrong.
6. Only output a probability that differs significantly from the market if you have concrete evidence the market is missing or misweighting.
7. If you have no strong evidence either way, output a probability within 3% of the current market price and a LOW confidence score.

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
    book_context: str = "",
    memory_context: str = "",
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

    if book_context:
        parts.append(f"\n{book_context}")

    if news_context:
        parts.append(f"\n## Recent News\n{news_context}")

    if memory_context:
        parts.append(f"\n## Agent Memory\n{memory_context}")

    if additional_context:
        parts.append(f"\n## Additional Context\n{additional_context}")

    parts.append(
        "\n## Your Task\n"
        "Estimate the TRUE probability that this market resolves YES. "
        "Consider the resolution criteria carefully. "
        "Factor in order book microstructure if provided (imbalance, whale activity, walls). "
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
    book_context: str = "",
    memory_context: str = "",
    provider: Optional[str] = None,
) -> AIAnalysis:
    """Use AI to analyze a market and estimate the true probability.

    Uses the research model by default for deep analysis.
    """
    prompt = _build_analysis_prompt(
        market, news_context, additional_context, book_context, memory_context
    )

    response = await llm.research(
        prompt=prompt,
        system=SYSTEM_PROMPT,
        purpose="market_analysis",
        market_id=market.condition_id,
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


async def quick_score_market(
    llm: LLMClient,
    market: Market,
    book_context: str = "",
) -> dict:
    """Quick and cheap market scoring using the compute model.

    Returns a dict with score (0-100) and brief reasoning.
    Used to pre-filter markets before expensive deep analysis.
    """
    prompt = (
        f"Score this prediction market for MISPRICING potential (0-100).\n\n"
        f"Question: {market.question}\n"
        f"YES price: ${market.yes_price:.2f} ({market.yes_price*100:.0f}% implied) | NO price: ${market.no_price:.2f}\n"
        f"Volume: ${market.volume:,.0f} | Liquidity: ${market.liquidity:,.0f}\n"
    )

    if market.end_date and market.hours_to_resolution:
        prompt += f"Time to resolution: {market.hours_to_resolution:.0f} hours\n"

    if book_context:
        prompt += f"\n{book_context}\n"

    prompt += (
        "\nScore ONLY based on whether the market price is likely WRONG — not on general trading quality.\n"
        "Ask yourself: do I have specific knowledge suggesting the true probability differs from {:.0f}%?\n".format(market.yes_price * 100)
        + "High scores (70+) = strong evidence the market is mispriced.\n"
        "Medium scores (40-70) = some reason to think market may be off.\n"
        "Low scores (0-40) = market price looks about right, no clear edge.\n"
        "Default to LOW scores unless you have specific contrary evidence.\n\n"
        "Output ONLY JSON: {\"score\": <0-100>, \"reason\": \"<one sentence>\"}"
    )

    response = await llm.compute(
        prompt=prompt,
        purpose="quick_score",
        market_id=market.condition_id,
    )

    try:
        data = json.loads(response.text)
        return {"score": int(data.get("score", 50)), "reason": data.get("reason", "")}
    except (json.JSONDecodeError, ValueError):
        return {"score": 50, "reason": "Failed to parse score"}


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
