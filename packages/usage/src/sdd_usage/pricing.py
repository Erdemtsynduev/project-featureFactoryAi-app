"""Dated model rate cards and API-equivalent cost of measured token usage.

Rates are USD per million tokens, as published by the providers and collected in
ClickHouse's tokenomics-viewer catalog (ISC licence; values as of 2026-09-22).
They price usage at public API rates: a subscription is billed separately, so
the figure is an equivalent, never an invoice. Unknown models stay unpriced.

Token categories follow the provider split:
* input — uncached prompt tokens;
* cache_write — prompt tokens written to the cache (Anthropic 5-minute tier);
* cache_read — prompt tokens served from the cache;
* output — generated tokens, reasoning included.
The engine's measured `input_tokens` already includes cached tokens, so the
uncached part is `input - cache_read - cache_write`.
"""

from dataclasses import dataclass, field

LONG_CONTEXT = 272_000  # OpenAI long-context pricing threshold (prompt tokens)


@dataclass(frozen=True)
class Rates:
    input: float
    output: float
    cache_read: float | None = None
    cache_write: float | None = None
    long: "Rates | None" = None


@dataclass(frozen=True)
class Card:
    """Rates in force from `since` (ISO date, inclusive) until the next card."""

    since: str
    rates: Rates


@dataclass(frozen=True)
class Model:
    id: str
    provider: str
    cards: tuple[Card, ...]
    aliases: tuple[str, ...] = ()


def _anthropic(i: float, o: float, read: float, write: float) -> Rates:
    return Rates(i, o, read, write)


def _openai(
    i: float, o: float, read: float | None, long: tuple[float, float, float | None] | None = None
) -> Rates:
    return Rates(i, o, read, None, Rates(long[0], long[1], long[2]) if long else None)


CATALOG: tuple[Model, ...] = (
    Model(
        "claude-fable-5",
        "anthropic",
        (Card("2026-01-01", _anthropic(10, 50, 1.0, 12.5)),),
        ("fable",),
    ),
    Model(
        "claude-opus-5-5",
        "anthropic",
        (Card("2026-09-22", _anthropic(4, 20, 0.2, 5.0)),),
        ("opus", "opus-5-5", "claude-opus-5.5"),
    ),
    Model("claude-opus-5", "anthropic", (Card("2026-01-01", _anthropic(5, 25, 0.5, 6.25)),)),
    Model("claude-opus-4-8", "anthropic", (Card("2026-01-01", _anthropic(5, 25, 0.5, 6.25)),)),
    Model("claude-opus-4-7", "anthropic", (Card("2026-01-01", _anthropic(5, 25, 0.5, 6.25)),)),
    Model("claude-opus-4-6", "anthropic", (Card("2026-01-01", _anthropic(5, 25, 0.5, 6.25)),)),
    Model("claude-opus-4-5", "anthropic", (Card("2025-01-01", _anthropic(5, 25, 0.5, 6.25)),)),
    Model("claude-opus-4-1", "anthropic", (Card("2025-01-01", _anthropic(15, 75, 1.5, 18.75)),)),
    Model(
        "claude-sonnet-5",
        "anthropic",
        (
            Card("2026-01-01", _anthropic(2, 10, 0.2, 2.5)),
            Card("2026-09-01", _anthropic(3, 15, 0.3, 3.75)),
        ),
        ("sonnet",),
    ),
    Model("claude-sonnet-4-6", "anthropic", (Card("2025-01-01", _anthropic(3, 15, 0.3, 3.75)),)),
    Model("claude-sonnet-4-5", "anthropic", (Card("2025-01-01", _anthropic(3, 15, 0.3, 3.75)),)),
    Model(
        "claude-haiku-4-5",
        "anthropic",
        (Card("2025-01-01", _anthropic(1, 5, 0.1, 1.25)),),
        ("haiku",),
    ),
    Model("gpt-6-astra", "openai", (Card("2026-01-01", _openai(10, 50, 1.0, (20, 75, 2.0))),)),
    Model("gpt-6-sol", "openai", (Card("2026-09-22", _openai(2, 10, 0.2, (4, 15, 0.4))),)),
    Model(
        "gpt-6-luna", "openai", (Card("2026-09-22", _openai(0.1, 0.5, 0.01, (0.2, 0.75, 0.02))),)
    ),
    Model("gpt-5.5", "openai", (Card("2026-01-01", _openai(5, 30, 0.5, (10, 45, 1.0))),)),
    Model("gpt-5.4", "openai", (Card("2025-01-01", _openai(2.5, 15, 0.25, (5, 22.5, 0.5))),)),
    Model("gpt-5.4-mini", "openai", (Card("2025-01-01", _openai(0.75, 4.5, 0.075)),)),
    Model("gpt-5.4-nano", "openai", (Card("2025-01-01", _openai(0.2, 1.25, 0.02)),)),
    Model("gpt-5", "openai", (Card("2025-01-01", _openai(1.25, 10, 0.125)),), ("gpt-5.1",)),
    Model("gpt-5-mini", "openai", (Card("2025-01-01", _openai(0.25, 2, 0.025)),)),
)


@dataclass(frozen=True)
class Cost:
    model: str | None  # resolved catalog id, None when unknown
    usd: float | None  # None when the model or a needed token count is unknown
    parts: dict[str, float] = field(default_factory=dict)
    note: str = ""


def resolve(name: str | None, catalog: tuple[Model, ...] = CATALOG) -> Model | None:
    if not name:
        return None
    key = name.strip().lower()
    for model in catalog:
        if key == model.id or key in model.aliases:
            return model
    # Dated snapshots such as "claude-opus-5-5-20260922" share the family's card.
    for model in sorted(catalog, key=lambda m: -len(m.id)):
        if key.startswith(model.id + "-"):
            return model
    return None


def rates_at(model: Model, day: str) -> Rates:
    """The card in force on an ISO date; before the first card, the first card."""
    chosen = model.cards[0]
    for card in model.cards:
        if card.since <= day:
            chosen = card
    return chosen.rates


def cost(
    name: str | None,
    day: str,
    input_tokens: int | None,
    output_tokens: int | None,
    cache_read: int | None = 0,
    cache_write: int | None = 0,
    catalog: tuple[Model, ...] = CATALOG,
) -> Cost:
    model = resolve(name, catalog)
    if model is None:
        return Cost(None, None, note=f"No public rates for {name or 'an unnamed model'}")
    if input_tokens is None or output_tokens is None:
        return Cost(model.id, None, note="Token counts were not reported")
    read, write = cache_read or 0, cache_write or 0
    uncached = max(0, input_tokens - read - write)
    rates = rates_at(model, day)
    if rates.long and input_tokens > LONG_CONTEXT:
        rates = rates.long
    parts = {
        "input": uncached * rates.input / 1e6,
        "cache_write": write
        * (rates.cache_write if rates.cache_write is not None else rates.input)
        / 1e6,
        "cache_read": read
        * (rates.cache_read if rates.cache_read is not None else rates.input)
        / 1e6,
        "output": output_tokens * rates.output / 1e6,
    }
    return Cost(model.id, sum(parts.values()), {k: round(v, 6) for k, v in parts.items() if v})
