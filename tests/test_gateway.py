import fakeredis
import pytest

from koshguard.gateway.cache import InMemoryCache, RedisCache
from koshguard.gateway.gateway import (
    BudgetExceeded,
    CircuitBreaker,
    GatewayUnavailable,
    LLMGateway,
    RateLimitExceeded,
)
from koshguard.gateway.providers import MockProvider
from koshguard.gateway.ratelimit import InMemoryBudget, InMemoryTokenBucket, RedisBudget, RedisTokenBucket

PRIMARY, FALLBACK = "big-model", "small-model"
PROMPT = [{"role": "user", "content": "CONTEXT:\n[fd.md] Senior citizens get 0.50 percent extra.\nQUESTION: senior citizen rate?"}]


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make_gateway(provider, *, capacity=100, budget=100_000, cache=True, retries=2, threshold=3, clock=None):
    clock = clock or FakeClock()
    sleeps: list[float] = []
    gw = LLMGateway(
        provider,
        models=[PRIMARY, FALLBACK],
        rate_limiter=InMemoryTokenBucket(capacity, 1.0, clock),
        budget=InMemoryBudget(budget),
        cache=InMemoryCache(60, 10, clock) if cache else None,
        prices={PRIMARY: (1.0, 2.0)},
        max_retries=retries,
        breaker_threshold=threshold,
        breaker_reset=30,
        clock=clock,
        sleep=sleeps.append,
    )
    return gw, sleeps, clock


def test_happy_path_returns_grounded_text_and_cost():
    gw, _, _ = make_gateway(MockProvider())
    res = gw.complete("u1", PROMPT)
    assert "0.50 percent" in res.response.text and res.response.model == PRIMARY
    assert res.cost_usd == 0 and not res.cached and res.attempts == 1  # mock calls are free
    assert gw.price(PRIMARY, 1_000_000, 1_000_000) == 3.0  # $1 in + $2 out per 1M tokens


def test_retries_transient_failures_with_backoff():
    provider = MockProvider(fail_times=2)
    gw, sleeps, _ = make_gateway(provider, threshold=10)
    res = gw.complete("u1", PROMPT)
    assert res.attempts == 3 and not res.fallback_used
    assert len(sleeps) == 2 and all(s >= 0 for s in sleeps)


def test_falls_back_when_primary_down():
    provider = MockProvider(failing_models={PRIMARY})
    gw, _, _ = make_gateway(provider, threshold=10)
    res = gw.complete("u1", PROMPT)
    assert res.fallback_used and res.response.model == FALLBACK
    assert provider.calls == [PRIMARY] * 3 + [FALLBACK]


def test_non_retryable_error_skips_retries():
    provider = MockProvider(failing_models={PRIMARY}, retryable=False)
    gw, sleeps, _ = make_gateway(provider, threshold=10)
    gw.complete("u1", PROMPT)
    assert provider.calls == [PRIMARY, FALLBACK] and sleeps == []


def test_all_models_down_raises():
    gw, _, _ = make_gateway(MockProvider(failing_models={PRIMARY, FALLBACK}), threshold=10)
    with pytest.raises(GatewayUnavailable):
        gw.complete("u1", PROMPT)


def test_circuit_breaker_opens_and_skips_primary_then_recovers():
    provider = MockProvider(failing_models={PRIMARY})
    gw, _, clock = make_gateway(provider, cache=False, threshold=3)
    gw.complete("u1", PROMPT)  # 3 failed attempts -> breaker opens
    assert gw.health()[PRIMARY] == "open"
    provider.calls.clear()
    gw.complete("u1", PROMPT)
    assert provider.calls == [FALLBACK]  # primary skipped without being called

    clock.t += 31  # reset timeout passes -> half-open trial
    provider.failing_models.clear()
    provider.calls.clear()
    res = gw.complete("u1", PROMPT)
    assert provider.calls == [PRIMARY] and not res.fallback_used and gw.health()[PRIMARY] == "closed"


def test_breaker_reopens_if_half_open_trial_fails():
    clock = FakeClock()
    b = CircuitBreaker(2, 10, clock)
    b.record_failure(); b.record_failure()
    assert b.state == "open"
    clock.t += 11
    assert b.state == "half_open"
    b.record_failure()
    assert b.state == "open"


def test_rate_limit_per_user():
    gw, _, clock = make_gateway(MockProvider(), capacity=2, cache=False)
    gw.complete("u1", PROMPT)
    gw.complete("u1", PROMPT)
    with pytest.raises(RateLimitExceeded):
        gw.complete("u1", PROMPT)
    gw.complete("u2", PROMPT)  # other users unaffected
    clock.t += 1.0  # refill 1 token/sec
    gw.complete("u1", PROMPT)


def test_daily_budget():
    gw, _, _ = make_gateway(MockProvider(), budget=1000, cache=False)
    gw.complete("u1", PROMPT, max_tokens=300)
    with pytest.raises(BudgetExceeded):
        gw.complete("u1", PROMPT, max_tokens=980)


def test_cache_hit_is_free_and_skips_provider():
    provider = MockProvider()
    gw, _, _ = make_gateway(provider)
    first = gw.complete("u1", PROMPT)
    second = gw.complete("u2", PROMPT)
    assert second.cached and second.cost_usd == 0 and second.response.text == first.response.text
    assert len(provider.calls) == 1


def test_cache_ttl_and_lru():
    clock = FakeClock()
    c = InMemoryCache(10, 2, clock)
    c.set("a", {"v": 1}); c.set("b", {"v": 2}); c.set("c", {"v": 3})
    assert c.get("a") is None and c.get("c") == {"v": 3}
    clock.t += 11
    assert c.get("c") is None


def test_redis_backends_with_fakeredis():
    r = fakeredis.FakeRedis(decode_responses=True)
    clock = FakeClock()
    bucket = RedisTokenBucket(r, capacity=2, refill_rate=1.0, clock=clock)
    assert bucket.allow("u") and bucket.allow("u") and not bucket.allow("u")
    clock.t += 1
    assert bucket.allow("u")

    budget = RedisBudget(r, 100)
    budget.consume("u", 60)
    assert budget.remaining("u") == 40

    cache = RedisCache(r, 60)
    cache.set("k", {"text": "hi"})
    assert cache.get("k") == {"text": "hi"}
