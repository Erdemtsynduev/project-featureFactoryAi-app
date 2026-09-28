"""API-equivalent pricing: dated cards, cache categories, long context, unknowns."""

import pytest
from sdd_usage.pricing import cost, resolve


def test_anthropic_cost_splits_cache_categories():
    # 1M measured input = 600k uncached + 300k cache reads + 100k cache writes.
    priced = cost("opus", "2026-09-28", 1_000_000, 100_000, 300_000, 100_000)
    assert priced.model == "claude-opus-5-5"
    assert priced.parts == {"input": 2.4, "cache_write": 0.5, "cache_read": 0.06, "output": 2.0}
    assert priced.usd == pytest.approx(4.96)


def test_dated_cards_and_snapshots():
    assert cost("claude-sonnet-5", "2026-08-31", 1_000_000, 0).usd == pytest.approx(2.0)
    assert cost("claude-sonnet-5", "2026-09-01", 1_000_000, 0).usd == pytest.approx(3.0)
    assert resolve("claude-opus-5-5-20260922").id == "claude-opus-5-5"


def test_openai_long_context_and_cached_input():
    short = cost("gpt-6-astra", "2026-09-28", 100_000, 10_000, 40_000)
    assert short.usd == pytest.approx(0.6 + 0.04 + 0.5)
    long = cost("gpt-6-astra", "2026-09-28", 300_000, 0)
    assert long.usd == pytest.approx(6.0)


def test_unknown_models_and_counts_stay_unpriced():
    assert cost("mystery-model", "2026-09-28", 10, 10).usd is None
    assert cost("opus", "2026-09-28", None, 10).usd is None
    assert cost(None, "2026-09-28", 10, 10).model is None
