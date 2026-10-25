"""Per-user rate limiting (token bucket) and daily token budgets.

Token bucket: each user has a bucket of ``capacity`` requests that refills at
``refill_rate`` per second. It allows short bursts but caps sustained load,
which protects both the bank's LLM bill and the provider quota from a single
abusive client or a runaway script.

Daily budget: a hard cap on LLM tokens per user per UTC day. Rate limits stop
floods; budgets stop slow, expensive abuse (e.g. long prompts all day).

Both have an in-memory implementation (single process, the default) and a
Redis implementation for multiple API workers sharing state. The Redis bucket
uses a Lua script so read-modify-write is atomic on the server.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Protocol


class RateLimiter(Protocol):
    def allow(self, key: str, cost: float = 1.0) -> bool: ...


class InMemoryTokenBucket:
    def __init__(self, capacity: float, refill_rate: float, clock: Callable[[], float] = time.monotonic):
        self.capacity = capacity
        self.refill_rate = refill_rate
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_ts)
        self._lock = threading.Lock()

    def allow(self, key: str, cost: float = 1.0) -> bool:
        now = self._clock()
        with self._lock:
            tokens, last = self._buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.refill_rate)
            allowed = tokens >= cost
            if allowed:
                tokens -= cost
            self._buckets[key] = (tokens, now)
            return allowed


_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local state = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(state[1]) or capacity
local ts = tonumber(state[2]) or now
tokens = math.min(capacity, tokens + (now - ts) * rate)
local allowed = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
end
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, math.ceil(capacity / rate) + 60)
return allowed
"""


class RedisTokenBucket:
    def __init__(self, client: Any, capacity: float, refill_rate: float, clock: Callable[[], float] = time.time):
        self._client = client
        self.capacity = capacity
        self.refill_rate = refill_rate
        self._clock = clock
        self._script = client.register_script(_BUCKET_LUA)

    def allow(self, key: str, cost: float = 1.0) -> bool:
        res = self._script(keys=[f"kg:rl:{key}"], args=[self.capacity, self.refill_rate, self._clock(), cost])
        return bool(int(res))


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class InMemoryBudget:
    def __init__(self, daily_limit: int, today: Callable[[], str] = _today):
        self.daily_limit = daily_limit
        self._today = today
        self._used: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    def remaining(self, key: str) -> int:
        with self._lock:
            return self.daily_limit - self._used.get((key, self._today()), 0)

    def consume(self, key: str, tokens: int) -> None:
        with self._lock:
            k = (key, self._today())
            self._used[k] = self._used.get(k, 0) + tokens


class RedisBudget:
    def __init__(self, client: Any, daily_limit: int, today: Callable[[], str] = _today):
        self._client = client
        self.daily_limit = daily_limit
        self._today = today

    def _key(self, key: str) -> str:
        return f"kg:budget:{key}:{self._today()}"

    def remaining(self, key: str) -> int:
        return self.daily_limit - int(self._client.get(self._key(key)) or 0)

    def consume(self, key: str, tokens: int) -> None:
        k = self._key(key)
        pipe = self._client.pipeline()
        pipe.incrby(k, tokens)
        pipe.expire(k, 2 * 86400)
        pipe.execute()
