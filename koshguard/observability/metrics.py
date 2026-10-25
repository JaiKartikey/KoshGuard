"""In-process metrics: counters, latency percentiles, tokens and cost.

Exposed as JSON at ``GET /metrics`` and in Prometheus text format at
``GET /metrics?format=prometheus`` so it can be scraped into Grafana. Latency
percentiles use a bounded window of recent requests; a production system
would use Prometheus histograms instead, but the window keeps this
dependency-free and easy to read.
"""

from __future__ import annotations

import threading
from collections import Counter, deque
from typing import Any


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct / 100
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo), 3)


class Metrics:
    def __init__(self, window: int = 5000) -> None:
        self._lock = threading.Lock()
        self.requests_total = 0
        self.outcomes: Counter[str] = Counter()
        self.blocks_by_type: Counter[str] = Counter()
        self.pii_detected: Counter[str] = Counter()
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cost_usd = 0.0
        self.cache_hits = 0
        self.llm_calls = 0
        self.fallbacks = 0
        self._latencies: deque[float] = deque(maxlen=window)

    def record(self, *, outcome: str, reason: str | None, latency_ms: float, pii: dict[str, int],
               llm: dict[str, Any] | None) -> None:
        with self._lock:
            self.requests_total += 1
            self.outcomes[outcome] += 1
            if outcome.startswith("blocked") and reason:
                self.blocks_by_type[reason] += 1
            self.pii_detected.update(pii)
            self._latencies.append(latency_ms)
            if llm:
                self.llm_calls += 1
                self.prompt_tokens += llm.get("prompt_tokens", 0)
                self.completion_tokens += llm.get("completion_tokens", 0)
                self.cost_usd += llm.get("cost_usd", 0.0)
                self.cache_hits += bool(llm.get("cached"))
                self.fallbacks += bool(llm.get("fallback_used"))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            lat = list(self._latencies)
            return {
                "requests_total": self.requests_total,
                "outcomes": dict(self.outcomes),
                "blocks_by_type": dict(self.blocks_by_type),
                "pii_detected": dict(self.pii_detected),
                "latency_ms": {"p50": percentile(lat, 50), "p95": percentile(lat, 95), "window": len(lat)},
                "llm": {
                    "calls": self.llm_calls,
                    "cache_hits": self.cache_hits,
                    "fallbacks": self.fallbacks,
                    "prompt_tokens": self.prompt_tokens,
                    "completion_tokens": self.completion_tokens,
                    "cost_usd": round(self.cost_usd, 6),
                },
            }

    def prometheus(self) -> str:
        s = self.snapshot()
        lines = [
            "# TYPE koshguard_requests_total counter",
            f"koshguard_requests_total {s['requests_total']}",
            "# TYPE koshguard_outcomes_total counter",
            *(f'koshguard_outcomes_total{{outcome="{k}"}} {v}' for k, v in s["outcomes"].items()),
            "# TYPE koshguard_blocks_total counter",
            *(f'koshguard_blocks_total{{reason="{k}"}} {v}' for k, v in s["blocks_by_type"].items()),
            "# TYPE koshguard_latency_ms gauge",
            f'koshguard_latency_ms{{quantile="0.5"}} {s["latency_ms"]["p50"]}',
            f'koshguard_latency_ms{{quantile="0.95"}} {s["latency_ms"]["p95"]}',
            "# TYPE koshguard_tokens_total counter",
            f'koshguard_tokens_total{{kind="prompt"}} {s["llm"]["prompt_tokens"]}',
            f'koshguard_tokens_total{{kind="completion"}} {s["llm"]["completion_tokens"]}',
            "# TYPE koshguard_cost_usd_total counter",
            f"koshguard_cost_usd_total {s['llm']['cost_usd']}",
            "# TYPE koshguard_cache_hits_total counter",
            f"koshguard_cache_hits_total {s['llm']['cache_hits']}",
        ]
        return "\n".join(lines) + "\n"
