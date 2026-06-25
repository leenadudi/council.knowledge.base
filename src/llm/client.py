"""
TrackedAnthropic — a thin wrapper around the Anthropic SDK that records
per-call token usage and estimated cost. Pricing, the cost estimator, and the
UsageRecord live here; the wrapper class is added in a later task.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Optional

# USD per 1,000,000 tokens: (input, output, cache_read, cache_write).
# cache_read ~= 0.1x input, cache_write ~= 1.25x input.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-sonnet-4-6": (3.0, 15.0, 0.30, 3.75),
    "claude-haiku-4-5": (1.0, 5.0, 0.10, 1.25),
}
_DEFAULT_PRICE = PRICES["claude-sonnet-4-6"]


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
) -> float:
    inp, out, cr, cw = PRICES.get(model, _DEFAULT_PRICE)
    total = (
        input_tokens * inp
        + output_tokens * out
        + cache_read_tokens * cr
        + cache_write_tokens * cw
    )
    return round(total / 1_000_000, 6)


@dataclass
class UsageRecord:
    call_site: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    est_cost_usd: float
    latency_ms: int
    query_id: Optional[str] = None
    batch_id: Optional[str] = None

    @classmethod
    def from_response(
        cls,
        *,
        call_site: str,
        model: str,
        response: Any,
        latency_ms: int,
        query_id: Optional[str] = None,
        batch_id: Optional[str] = None,
    ) -> "UsageRecord":
        u = getattr(response, "usage", None)
        input_tokens = int(getattr(u, "input_tokens", 0) or 0)
        output_tokens = int(getattr(u, "output_tokens", 0) or 0)
        cache_read = int(getattr(u, "cache_read_input_tokens", 0) or 0)
        cache_write = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
        return cls(
            call_site=call_site,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            est_cost_usd=estimate_cost(
                model, input_tokens, output_tokens, cache_read, cache_write
            ),
            latency_ms=latency_ms,
            query_id=query_id,
            batch_id=batch_id,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": uuid.uuid4(),
            "call_site": self.call_site,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "est_cost_usd": self.est_cost_usd,
            "latency_ms": self.latency_ms,
            "query_id": uuid.UUID(self.query_id) if self.query_id else None,
            "batch_id": self.batch_id,
        }
