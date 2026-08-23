"""Cost accounting tests — R10, "per-artifact cost is tracked and reportable".

Pre-written alongside `test_gates.py` (PLAN.md §0.1) and red until round 8
implements `app/pipeline/cost.py`.

The contract these pin down:

    PRICING              module-level rates, must match SPEC.md §14
    LLMUsage(prompt_tokens, output_tokens, thinking_tokens)
        .from_response_usage(obj)   reads a google-genai usage_metadata
    CostTracker()
        .record_llm_call(usage)     once per attempt, retries included
        .record_tts(chars)
        .breakdown()  -> CostBreakdown

    CostBreakdown(llm_usd, tts_usd, total_usd, production_estimate_usd)

`llm_usd` is metered from real token counts at Gemini list rates even on the
free tier, so the number is meaningful when the project is priced for
production. `tts_usd` is genuinely 0.0 because edge-tts costs nothing;
`production_estimate_usd` is what the same job would cost with paid TTS,
compute, storage and egress.

**The load-bearing test in this file is
`test_thinking_tokens_are_billed_at_the_output_rate`.** `gemini-3.5-flash-lite`
is a thinking model, thinking tokens bill at the output rate, and they never
appear in the response text — so a cost model that reads only
`candidates_token_count` silently under-reports. `scripts/smoke.py` observed
~500 thinking tokens against a 17-token prompt, i.e. an order of magnitude more
than the visible output.
"""

import pytest

from app.pipeline.cost import PRICING, CostTracker, LLMUsage

# SPEC.md §14. Kept literal here on purpose: if someone edits the rate in
# cost.py, this file is what says the spec disagrees.
LLM_INPUT_PER_1M = 0.30
LLM_OUTPUT_PER_1M = 2.50
TTS_PER_1M_CHARS = 16.00


def usage(prompt=1500, output=1000, thinking=0):
    return LLMUsage(prompt_tokens=prompt, output_tokens=output,
                    thinking_tokens=thinking)


class FakeResponseUsage:
    """The shape google-genai hands back on `response.usage_metadata`.

    `thoughts_token_count` is None rather than 0 when a model does not think,
    which is exactly the case a naive implementation crashes on.
    """

    def __init__(self, prompt, candidates, thoughts):
        self.prompt_token_count = prompt
        self.candidates_token_count = candidates
        self.thoughts_token_count = thoughts


# ---------------------------------------------------------------------------
# Rates
# ---------------------------------------------------------------------------

def test_pricing_constants_match_spec_section_14():
    assert PRICING["llm_input_per_1m"] == LLM_INPUT_PER_1M
    assert PRICING["llm_output_per_1m"] == LLM_OUTPUT_PER_1M
    assert PRICING["tts_per_1m_chars"] == TTS_PER_1M_CHARS


def test_output_tokens_cost_far_more_than_input_tokens():
    """Not a tautology — it is why thinking tokens matter so much.

    Input is $0.30/1M and output $2.50/1M, so a token moved from the invisible
    thinking budget to the bill costs over 8x what a prompt token does.
    """
    assert PRICING["llm_output_per_1m"] > 8 * PRICING["llm_input_per_1m"]


# ---------------------------------------------------------------------------
# Metering a single call
# ---------------------------------------------------------------------------

def test_a_call_with_no_thinking_costs_prompt_plus_visible_output():
    tracker = CostTracker()
    tracker.record_llm_call(usage(prompt=1500, output=1000, thinking=0))
    expected = 1500 / 1e6 * LLM_INPUT_PER_1M + 1000 / 1e6 * LLM_OUTPUT_PER_1M
    assert tracker.breakdown().llm_usd == pytest.approx(expected)


def test_thinking_tokens_are_billed_at_the_output_rate():
    """The regression this whole file exists to prevent.

    Two calls, identical prompts and identical visible output. The second one
    thought for 500 tokens. Those tokens are invisible in `response.text` and
    are billed at the output rate, so the second call must cost strictly more —
    by exactly 500 output tokens, not by zero.
    """
    quiet, thoughtful = CostTracker(), CostTracker()
    quiet.record_llm_call(usage(prompt=1500, output=1000, thinking=0))
    thoughtful.record_llm_call(usage(prompt=1500, output=1000, thinking=500))

    delta = thoughtful.breakdown().llm_usd - quiet.breakdown().llm_usd
    assert delta == pytest.approx(500 / 1e6 * LLM_OUTPUT_PER_1M), (
        "thinking tokens must be billed as output tokens; counting only "
        "candidates_token_count under-reports every job on a thinking model"
    )


def test_thinking_tokens_can_dominate_a_small_call():
    """The smoke test's real numbers: 17 prompt, 49 visible, 496 thinking.

    If thinking were free this call would cost about a ninth of what it does.
    """
    tracker = CostTracker()
    tracker.record_llm_call(usage(prompt=17, output=49, thinking=496))

    billed = tracker.breakdown().llm_usd
    ignoring_thinking = 17 / 1e6 * LLM_INPUT_PER_1M + 49 / 1e6 * LLM_OUTPUT_PER_1M
    assert billed > 5 * ignoring_thinking


# ---------------------------------------------------------------------------
# Reading usage off a real response
# ---------------------------------------------------------------------------

def test_usage_is_read_from_the_google_genai_response_shape():
    parsed = LLMUsage.from_response_usage(FakeResponseUsage(1500, 1000, 500))
    assert (parsed.prompt_tokens, parsed.output_tokens, parsed.thinking_tokens) \
        == (1500, 1000, 500)


def test_a_null_thinking_token_count_is_treated_as_zero():
    """Non-thinking responses report None, not 0. Arithmetic on None raises."""
    parsed = LLMUsage.from_response_usage(FakeResponseUsage(1500, 1000, None))
    assert parsed.thinking_tokens == 0

    tracker = CostTracker()
    tracker.record_llm_call(parsed)  # must not raise
    assert tracker.breakdown().llm_usd > 0


# ---------------------------------------------------------------------------
# Accumulating across a job
# ---------------------------------------------------------------------------

def test_llm_cost_accumulates_across_retry_attempts():
    """A job that failed G4 twice paid for three calls, and the bill says so.

    Under-counting retries would make the degraded path look free, which is
    the opposite of true.
    """
    one, three = CostTracker(), CostTracker()
    one.record_llm_call(usage())
    for _ in range(3):
        three.record_llm_call(usage())

    assert three.breakdown().llm_usd == pytest.approx(3 * one.breakdown().llm_usd)


def test_a_job_that_made_no_llm_call_costs_nothing_for_the_llm():
    """The fallback path still renders a video, but it never called Gemini."""
    assert CostTracker().breakdown().llm_usd == 0.0


def test_edge_tts_is_free_so_actual_tts_spend_is_zero():
    tracker = CostTracker()
    tracker.record_tts(chars=1000)
    assert tracker.breakdown().tts_usd == 0.0


def test_total_is_the_sum_of_what_was_actually_spent():
    tracker = CostTracker()
    tracker.record_llm_call(usage(thinking=500))
    tracker.record_tts(chars=1000)

    b = tracker.breakdown()
    assert b.total_usd == pytest.approx(b.llm_usd + b.tts_usd)


# ---------------------------------------------------------------------------
# The production estimate
# ---------------------------------------------------------------------------

def test_production_estimate_prices_tts_at_paid_rates():
    """edge-tts is free; the estimate answers "what if this were Azure"."""
    quiet, spoken = CostTracker(), CostTracker()
    quiet.record_llm_call(usage())
    spoken.record_llm_call(usage())
    spoken.record_tts(chars=1000)

    delta = (spoken.breakdown().production_estimate_usd
             - quiet.breakdown().production_estimate_usd)
    assert delta == pytest.approx(1000 / 1e6 * TTS_PER_1M_CHARS)


def test_production_estimate_exceeds_actual_spend():
    tracker = CostTracker()
    tracker.record_llm_call(usage(thinking=500))
    tracker.record_tts(chars=1000)

    b = tracker.breakdown()
    assert b.production_estimate_usd > b.total_usd


def test_a_reference_job_lands_inside_the_spec_section_14_range():
    """One script call, 1000 characters of narration — the §14 reference job.

    SPEC.md §14 puts a whole job at roughly $0.021-$0.025 in production. This
    is the test that fails loudly if someone changes a rate without updating
    the spec table the README quotes.
    """
    tracker = CostTracker()
    tracker.record_llm_call(usage(prompt=1500, output=1000, thinking=500))
    tracker.record_tts(chars=1000)

    estimate = tracker.breakdown().production_estimate_usd
    assert 0.021 <= estimate <= 0.025, (
        f"reference job estimated at ${estimate:.4f}, outside the $0.021-$0.025 "
        "band documented in SPEC.md §14"
    )


def test_tts_is_still_the_largest_line_item():
    """SPEC.md §14's headline finding, asserted rather than claimed.

    It survived the move to gemini-3.5-flash-lite, but only just — a
    reasoning-heavier model would flip it, and this test is what would catch
    that before the README says something false.
    """
    tracker = CostTracker()
    tracker.record_llm_call(usage(prompt=1500, output=1000, thinking=500))
    tracker.record_tts(chars=1000)

    b = tracker.breakdown()
    tts_share = 1000 / 1e6 * TTS_PER_1M_CHARS / b.production_estimate_usd
    llm_share = b.llm_usd / b.production_estimate_usd
    assert tts_share > llm_share


# ---------------------------------------------------------------------------
# The shape the API returns
# ---------------------------------------------------------------------------

def test_breakdown_exposes_exactly_the_spec_section_5_fields():
    tracker = CostTracker()
    tracker.record_llm_call(usage())
    tracker.record_tts(chars=1000)

    b = tracker.breakdown()
    for field in ("llm_usd", "tts_usd", "total_usd", "production_estimate_usd"):
        value = getattr(b, field)
        assert isinstance(value, float), f"{field} must serialise as a number"
        assert value >= 0.0
