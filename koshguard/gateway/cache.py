"""Exact-match response cache.

FAQ traffic is highly repetitive ("what is the FD rate?"), so caching saves
cost and latency. The key is a SHA-256 of model + messages + parameters, so
only byte-identical prompts hit. Because the prompt already contains the
*tokenized* question and retrieved context, a cached answer never mixes one
customer's PII into another's response.

Only deterministic calls (temperature 0) should be cached; the gateway
enforces that. A semantic (embedding-similarity) cache would hit more often
but can return a subtly wrong answer for a differently-worded question, which
is a poor trade for a bank.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, Protocol


def cache_key(model: str, messages: list[dict[str, str]], **params: Any) -> str:
    payload = json.dumps({"model": model, "messages": messages, "params": params}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


class ResponseCache(Protocol):
    def get(self, key: str) -> dict | None: ...
    def set(self, key: str, value: dict) -> None: ...


class InMemoryCache:
    """TTL + LRU bounded cache."""

    def __init__(self, ttl_seconds: float, max_entries: int, clock: Callable[[], float] = time.monotonic):
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._clock = clock
        self._data: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> dict | None:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            expires, value = item
            if self._clock() >= expires:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return value

    def set(self, key: str, value: dict) -> None:
        with self._lock:
            self._data[key] = (self._clock() + self.ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)


class RedisCache:
    def __init__(self, client: Any, ttl_seconds: int):
        self._client = client
        self.ttl = ttl_seconds

    def get(self, key: str) -> dict | None:
        raw = self._client.get(f"kg:cache:{key}")
        return json.loads(raw) if raw else None

    def set(self, key: str, value: dict) -> None:
        self._client.set(f"kg:cache:{key}", json.dumps(value), ex=self.ttl)
