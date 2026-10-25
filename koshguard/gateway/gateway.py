"""The LLM gateway: the only code path that calls a model.

Every request goes through, in order:

1. **Rate limit** (per-user token bucket): reject floods before spending anything.
2. **Budget** (per-user daily token cap): reject if the estimate would exceed it.
3. **Cache** (exact match): free and instant if the same prompt was answered.
4. **Model chain** (primary, then fallback). For each model:
   * skip it if its **circuit breaker** is open,
   * call it with **retries and exponential backoff with jitter** for
     retryable errors,
   * record success or failure on the breaker.
5. **Accounting**: actual tokens charged to the budget and cost computed from
   the configured price table.

Centralising this means the agent never needs to know about quotas, outages or
pricing, and every policy (who may spend what) is enforced in one place. It is
the same role commercial gateways like Portkey or LiteLLM play.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from koshguard.config import Settings
from koshguard.gateway.cache import InMemoryCache, RedisCache, ResponseCache, cache_key
from koshguard.gateway.providers import (
    GroqProvider,
    LLMProvider,
    LLMResponse,
    Message,
    MockProvider,
    ProviderError,
    estimate_tokens,
)
from koshguard.gateway.ratelimit import (
    InMemoryBudget,
    InMemoryTokenBucket,
    RateLimiter,
    RedisBudget,
    RedisTokenBucket,
)

logger = logging.getLogger(__name__)


class GatewayError(Exception):
    """Base class; ``kind`` is a stable string for metrics and API responses."""

    kind = "gateway_error"


class RateLimitExceeded(GatewayError):
    kind = "rate_limited"


class BudgetExceeded(GatewayError):
    kind = "budget_exceeded"


class GatewayUnavailable(GatewayError):
    kind = "llm_unavailable"


class CircuitBreaker:
    """Classic three-state breaker.

    closed     -> calls flow; consecutive failures are counted.
    open       -> calls are skipped for ``reset_timeout`` seconds (fail fast,
                  give the provider room to recover, go straight to fallback).
    half_open  -> one trial call is let through; success closes the breaker,
                  failure re-opens it.
    """

    def __init__(self, failure_threshold: int, reset_timeout: float, clock: Callable[[], float] = time.monotonic):
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at >= self.reset_timeout:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        return self.state != "open"

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            if self.state == "half_open":
                self._opened_at = self._clock()  # trial failed: open again
                return
            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._opened_at = self._clock()


@dataclass
class GatewayResult:
    response: LLMResponse
    cost_usd: float
    cached: bool
    attempts: int
    fallback_used: bool
    errors: list[str] = field(default_factory=list)


class LLMGateway:
    def __init__(
        self,
        provider: LLMProvider,
        *,
        models: list[str],
        rate_limiter: RateLimiter,
        budget: InMemoryBudget | RedisBudget,
        cache: ResponseCache | None,
        prices: dict[str, tuple[float, float]],
        max_retries: int = 2,
        backoff_base: float = 0.5,
        backoff_max: float = 8.0,
        breaker_threshold: int = 5,
        breaker_reset: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.provider = provider
        self.models = models
        self.rate_limiter = rate_limiter
        self.budget = budget
        self.cache = cache
        self.prices = prices
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self._sleep = sleep
        self.breakers = {m: CircuitBreaker(breaker_threshold, breaker_reset, clock) for m in models}

    def cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        price_in, price_out = self.prices.get(model, (0.0, 0.0))
        return round((prompt_tokens * price_in + completion_tokens * price_out) / 1_000_000, 8)

    def _backoff(self, attempt: int) -> float:
        # Full jitter (AWS architecture blog): spreads retries so many clients
        # recovering at once do not stampede the provider.
        return random.uniform(0, min(self.backoff_max, self.backoff_base * 2**attempt))

    def complete(
        self,
        user_id: str,
        messages: list[Message],
        *,
        max_tokens: int = 400,
        temperature: float = 0.0,
    ) -> GatewayResult:
        if not self.rate_limiter.allow(user_id):
            raise RateLimitExceeded(f"rate limit exceeded for {user_id}")

        estimate = sum(estimate_tokens(m["content"]) for m in messages) + max_tokens
        if self.budget.remaining(user_id) < estimate:
            raise BudgetExceeded(f"daily token budget exhausted for {user_id}")

        key = None
        if self.cache is not None and temperature == 0:
            key = cache_key(self.models[0], messages, max_tokens=max_tokens, temperature=temperature)
            hit = self.cache.get(key)
            if hit is not None:
                resp = LLMResponse(**hit)
                return GatewayResult(resp, cost_usd=0.0, cached=True, attempts=0, fallback_used=False)

        attempts = 0
        errors: list[str] = []
        for index, model in enumerate(self.models):
            breaker = self.breakers[model]
            if not breaker.allow():
                errors.append(f"{model}: circuit open")
                continue
            for attempt in range(self.max_retries + 1):
                attempts += 1
                try:
                    resp = self.provider.complete(messages, model=model, max_tokens=max_tokens, temperature=temperature)
                except ProviderError as e:
                    breaker.record_failure()
                    errors.append(f"{model}: {e}")
                    logger.warning("llm call failed", extra={"model": model, "attempt": attempt, "error": str(e)})
                    if not e.retryable or not breaker.allow() or attempt == self.max_retries:
                        break
                    self._sleep(self._backoff(attempt))
                    continue

                breaker.record_success()
                self.budget.consume(user_id, resp.total_tokens)
                if key is not None:
                    self.cache.set(key, resp.__dict__)  # type: ignore[union-attr]
                return GatewayResult(
                    response=resp,
                    cost_usd=self.cost(resp.model, resp.prompt_tokens, resp.completion_tokens),
                    cached=False,
                    attempts=attempts,
                    fallback_used=index > 0,
                    errors=errors,
                )

        raise GatewayUnavailable("; ".join(errors) or "no models configured")

    def health(self) -> dict[str, str]:
        return {m: b.state for m, b in self.breakers.items()}


def build_gateway(settings: Settings, provider: LLMProvider | None = None) -> LLMGateway:
    """Wire a gateway from settings: Groq or mock, in-memory or Redis state."""
    if provider is None:
        if settings.use_mock_llm:
            provider = MockProvider()
        else:
            provider = GroqProvider(settings.groq_api_key or "", settings.groq_base_url, settings.request_timeout_seconds)

    if settings.redis_url:
        import redis  # optional dependency

        client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
        limiter: RateLimiter = RedisTokenBucket(client, settings.rate_limit_capacity, settings.rate_limit_refill_per_second)
        budget: InMemoryBudget | RedisBudget = RedisBudget(client, settings.daily_token_budget)
        cache: ResponseCache = RedisCache(client, settings.cache_ttl_seconds)
    else:
        limiter = InMemoryTokenBucket(settings.rate_limit_capacity, settings.rate_limit_refill_per_second)
        budget = InMemoryBudget(settings.daily_token_budget)
        cache = InMemoryCache(settings.cache_ttl_seconds, settings.cache_max_entries)

    return LLMGateway(
        provider,
        models=[settings.primary_model, settings.fallback_model],
        rate_limiter=limiter,
        budget=budget,
        cache=cache,
        prices=settings.model_prices,
        max_retries=settings.max_retries,
        backoff_base=settings.backoff_base_seconds,
        backoff_max=settings.backoff_max_seconds,
        breaker_threshold=settings.breaker_failure_threshold,
        breaker_reset=settings.breaker_reset_seconds,
    )
