"""LLM abstraction layer — supports Claude and OpenAI with ensemble mode."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from polyedge.core.config import AIConfig


@dataclass
class LLMResponse:
    text: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


# Approximate costs per 1M tokens (as of 2026)
COST_TABLE = {
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-opus-4-6": {"input": 15.0, "output": 75.0},
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.0},
    "gpt-4o": {"input": 2.50, "output": 10.0},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
}


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    costs = COST_TABLE.get(model, {"input": 3.0, "output": 15.0})
    return (input_tokens * costs["input"] + output_tokens * costs["output"]) / 1_000_000


class LLMClient:
    """Unified LLM client for Claude and OpenAI."""

    def __init__(self, config: AIConfig, anthropic_key: str = "", openai_key: str = ""):
        self.config = config
        self._anthropic_client = None
        self._openai_client = None
        self._total_cost_today = 0.0

        if anthropic_key:
            import anthropic

            self._anthropic_client = anthropic.Anthropic(api_key=anthropic_key)

        if openai_key:
            import openai

            self._openai_client = openai.OpenAI(api_key=openai_key)

    @property
    def total_cost_today(self) -> float:
        return self._total_cost_today

    def reset_daily_cost(self):
        self._total_cost_today = 0.0

    async def analyze(
        self,
        prompt: str,
        system: str = "",
        provider: Optional[str] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
    ) -> LLMResponse:
        """Send a prompt to the configured LLM and return the response."""
        provider = provider or self.config.provider
        temp = temperature if temperature is not None else self.config.temperature

        # Check budget
        if self._total_cost_today >= self.config.max_analysis_cost_per_day:
            return LLMResponse(
                text="[BUDGET EXCEEDED] Daily AI analysis budget reached.",
                model="none",
                provider="none",
            )

        if self.config.ensemble and provider == "ensemble":
            return await self._ensemble_analyze(prompt, system, temp)

        if provider == "claude":
            return await self._claude_analyze(
                prompt, system, model or self.config.claude_model, temp
            )
        elif provider == "openai":
            return await self._openai_analyze(
                prompt, system, model or self.config.openai_model, temp
            )
        else:
            # Default to claude
            return await self._claude_analyze(
                prompt, system, model or self.config.claude_model, temp
            )

    async def _claude_analyze(
        self, prompt: str, system: str, model: str, temperature: float
    ) -> LLMResponse:
        if not self._anthropic_client:
            raise ValueError("Anthropic API key not configured")

        messages = [{"role": "user", "content": prompt}]
        kwargs = {"model": model, "max_tokens": 2048, "messages": messages, "temperature": temperature}
        if system:
            kwargs["system"] = system

        response = self._anthropic_client.messages.create(**kwargs)

        text = response.content[0].text
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        cost = _estimate_cost(model, input_tokens, output_tokens)
        self._total_cost_today += cost

        return LLMResponse(
            text=text,
            model=model,
            provider="claude",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
        )

    async def _openai_analyze(
        self, prompt: str, system: str, model: str, temperature: float
    ) -> LLMResponse:
        if not self._openai_client:
            raise ValueError("OpenAI API key not configured")

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        response = self._openai_client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=2048,
        )

        text = response.choices[0].message.content
        input_tokens = response.usage.prompt_tokens
        output_tokens = response.usage.completion_tokens
        cost = _estimate_cost(model, input_tokens, output_tokens)
        self._total_cost_today += cost

        return LLMResponse(
            text=text,
            model=model,
            provider="openai",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
        )

    async def _ensemble_analyze(
        self, prompt: str, system: str, temperature: float
    ) -> LLMResponse:
        """Run both Claude and OpenAI, return combined result."""
        results = []

        if self._anthropic_client:
            try:
                r = await self._claude_analyze(
                    prompt, system, self.config.claude_model, temperature
                )
                results.append(r)
            except Exception:
                pass

        if self._openai_client:
            try:
                r = await self._openai_analyze(
                    prompt, system, self.config.openai_model, temperature
                )
                results.append(r)
            except Exception:
                pass

        if not results:
            raise ValueError("No LLM providers available for ensemble")

        if len(results) == 1:
            return results[0]

        # Combine responses
        combined_text = "\n\n---\n\n".join(
            f"[{r.provider}/{r.model}]\n{r.text}" for r in results
        )
        total_cost = sum(r.cost_usd for r in results)
        self._total_cost_today += total_cost - sum(
            r.cost_usd for r in results
        )  # Already counted individually

        return LLMResponse(
            text=combined_text,
            model="ensemble",
            provider="ensemble",
            input_tokens=sum(r.input_tokens for r in results),
            output_tokens=sum(r.output_tokens for r in results),
            cost_usd=total_cost,
        )
