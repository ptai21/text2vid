"""Per-artifact cost accounting — R10 and SPEC.md §14.

Two numbers, deliberately kept apart.

`total_usd` is what this job **actually spent**: metered Gemini tokens at list
rates, and zero for TTS because edge-tts is free. `production_estimate_usd` is
what the same job would cost with paid TTS, compute, storage and egress.
Reporting only the first would understate the real economics; reporting only
the second would claim spend that never happened.

The one thing this module exists to get right is that **thinking tokens are
billed at the output rate**. `gemini-3.5-flash-lite` is a thinking model, and
those tokens never appear in `response.text` — so a cost model that reads
`candidates_token_count` alone silently under-reports every job. The smoke test
measured 496 thinking tokens against 49 visible ones, an order of magnitude
more than the part you can see.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.domain.job import CostBreakdown

PRICING: dict[str, float] = {
    # Gemini list rates, per 1M tokens. Charged even on the free tier for the
    # purposes of this model: a number that reads $0 in development tells an
    # evaluator nothing about what the design costs to run.
    "llm_input_per_1m": 0.30,
    "llm_output_per_1m": 2.50,

    # Azure Neural TTS, per 1M characters. The stand-in for edge-tts, which is
    # free and unmetered but has no SLA behind it.
    "tts_per_1m_chars": 16.00,

    # Flat per-job infrastructure, from the §14 table. Small enough to round
    # away and itemised anyway, because "render and encode are nearly free" is
    # a claim the cost model should be able to show rather than assert.
    "compute_per_job": 0.0005,       # ~45s CPU at $0.04/vCPU-hour
    "storage_per_job_month": 0.0002,  # ~8 MB at $0.023/GB-month
    "egress_per_view": 0.0007,        # ~8 MB at $0.09/GB
}

FIXED_PRODUCTION_KEYS = ("compute_per_job", "storage_per_job_month",
                         "egress_per_view")


@dataclass(frozen=True)
class LLMUsage:
    """Token counts for one model call.

    `thinking_tokens` is a separate field rather than folded into
    `output_tokens` so the manifest can show how much of the bill was invisible
    reasoning. That split is the whole reason SPEC.md §14 quotes a *range*
    instead of a single figure.
    """

    prompt_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0

    @classmethod
    def from_response_usage(cls, meta: Any) -> LLMUsage:
        """Read `response.usage_metadata` from google-genai.

        Every field is coerced through `or 0`. The SDK reports
        `thoughts_token_count` as **None**, not 0, when a model did not think,
        and arithmetic on None raises inside the cost path — which would turn a
        successful generation into an `internal_error` at the accounting step.
        """
        return cls(
            prompt_tokens=int(getattr(meta, "prompt_token_count", 0) or 0),
            output_tokens=int(getattr(meta, "candidates_token_count", 0) or 0),
            thinking_tokens=int(getattr(meta, "thoughts_token_count", 0) or 0),
        )

    @property
    def billed_output_tokens(self) -> int:
        """What the invoice counts as output. Visible text plus hidden thinking."""
        return self.output_tokens + self.thinking_tokens

    @property
    def usd(self) -> float:
        return (
            self.prompt_tokens / 1e6 * PRICING["llm_input_per_1m"]
            + self.billed_output_tokens / 1e6 * PRICING["llm_output_per_1m"]
        )


@dataclass
class CostTracker:
    """Accumulates one job's spend. One instance per job, never shared."""

    calls: list[LLMUsage] = field(default_factory=list)
    tts_chars: int = 0

    def record_llm_call(self, usage: LLMUsage) -> None:
        """Called once per attempt, **retries included**.

        Counting only the successful attempt would make the retry path look
        free, which is the opposite of true: a job that failed G4 twice paid
        for three calls and the manifest should say so.
        """
        self.calls.append(usage)

    def record_tts(self, chars: int) -> None:
        self.tts_chars += chars

    @property
    def llm_calls(self) -> int:
        return len(self.calls)

    @property
    def totals(self) -> LLMUsage:
        """Summed token counts, for the manifest."""
        return LLMUsage(
            prompt_tokens=sum(c.prompt_tokens for c in self.calls),
            output_tokens=sum(c.output_tokens for c in self.calls),
            thinking_tokens=sum(c.thinking_tokens for c in self.calls),
        )

    def breakdown(self) -> CostBreakdown:
        llm_usd = sum(call.usd for call in self.calls)
        tts_production = self.tts_chars / 1e6 * PRICING["tts_per_1m_chars"]
        fixed = sum(PRICING[key] for key in FIXED_PRODUCTION_KEYS)

        return CostBreakdown(
            llm_usd=llm_usd,
            # Genuinely zero, not "not measured". edge-tts has no bill.
            tts_usd=0.0,
            total_usd=llm_usd,
            production_estimate_usd=llm_usd + tts_production + fixed,
        )
