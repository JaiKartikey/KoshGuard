"""Model providers behind a common interface.

``GroqProvider`` talks to Groq's OpenAI-compatible Chat Completions API with
plain ``httpx`` (no vendor SDK, so swapping to another OpenAI-compatible
endpoint is a config change). ``MockProvider`` is a deterministic offline
stand-in used by tests and CI: it answers by extracting the most relevant
sentences from the context it is given, so the rest of the system (grounding
checks, evals) behaves realistically without a network call.

Errors are classified as retryable (timeouts, 429, 5xx) or not (bad request,
auth). Only the gateway decides what to do about them.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from koshguard.textutil import content_tokens

Message = dict[str, str]


@dataclass
class LLMResponse:
    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    provider: str

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class ProviderError(Exception):
    def __init__(self, message: str, *, retryable: bool, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class LLMProvider(Protocol):
    name: str

    def complete(
        self, messages: list[Message], *, model: str, max_tokens: int, temperature: float
    ) -> LLMResponse: ...


def estimate_tokens(text: str) -> int:
    """Rough token count (~4 characters per token) for budgeting before a call."""
    return max(1, len(text) // 4)


class GroqProvider:
    name = "groq"

    def __init__(self, api_key: str, base_url: str, timeout: float = 20.0, client: httpx.Client | None = None):
        self._client = client or httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def complete(self, messages, *, model, max_tokens, temperature):
        start = time.perf_counter()
        try:
            r = self._client.post(
                "/chat/completions",
                json={"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature},
            )
        except httpx.TimeoutException as e:
            raise ProviderError(f"timeout calling {model}", retryable=True) from e
        except httpx.TransportError as e:
            raise ProviderError(f"network error calling {model}: {e}", retryable=True) from e

        if r.status_code == 429 or r.status_code >= 500:
            raise ProviderError(f"{model} returned {r.status_code}", retryable=True, status=r.status_code)
        if r.status_code >= 400:
            raise ProviderError(f"{model} returned {r.status_code}: {r.text[:200]}", retryable=False, status=r.status_code)

        data = r.json()
        usage = data.get("usage", {})
        return LLMResponse(
            text=data["choices"][0]["message"]["content"] or "",
            model=data.get("model", model),
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            latency_ms=(time.perf_counter() - start) * 1000,
            provider=self.name,
        )


_CONTEXT_BLOCK = re.compile(r"^\[(?P<source>[^\]]+)\]\s*(?P<text>.+?)(?=^\[[^\]]+\]|^QUESTION:|\Z)", re.M | re.S)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


class MockProvider:
    """Deterministic extractive 'LLM' for offline runs.

    ``fail_times`` and ``failing_models`` inject faults so tests can exercise
    retries, fallback and the circuit breaker.
    """

    name = "mock"

    def __init__(self, fail_times: int = 0, failing_models: set[str] | None = None, retryable: bool = True):
        self.fail_times = fail_times
        self.failing_models = failing_models or set()
        self.retryable = retryable
        self.calls: list[str] = []

    def complete(self, messages, *, model, max_tokens, temperature):
        self.calls.append(model)
        if model in self.failing_models:
            raise ProviderError(f"{model} unavailable (injected)", retryable=self.retryable, status=503)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ProviderError("transient failure (injected)", retryable=self.retryable, status=503)

        prompt = messages[-1]["content"]
        text = self._answer(prompt)
        return LLMResponse(
            text=text,
            model=model,
            prompt_tokens=sum(estimate_tokens(m["content"]) for m in messages),
            completion_tokens=estimate_tokens(text),
            latency_ms=1.0,
            provider=self.name,
        )

    @staticmethod
    def _answer(prompt: str) -> str:
        question = prompt.rsplit("QUESTION:", 1)[-1]
        q_tokens = set(content_tokens(question))
        candidates: list[tuple[int, int, str, str]] = []
        for i, block in enumerate(_CONTEXT_BLOCK.finditer(prompt)):
            for j, sentence in enumerate(_SENTENCE.split(block["text"].strip())):
                sentence = sentence.strip().lstrip("#> ").strip()
                if len(sentence) < 15 or sentence.startswith("KoshBank is a fictional"):
                    continue
                overlap = len(q_tokens & set(content_tokens(sentence)))
                if overlap:
                    candidates.append((overlap, -(i * 1000 + j), sentence, block["source"]))
        if not candidates:
            return "I'm sorry, I don't have that information in KoshBank's policy documents."
        best = sorted(candidates, reverse=True)[:2]
        best.sort(key=lambda c: -c[1])  # restore document order
        sources = list(dict.fromkeys(c[3] for c in best))
        return " ".join(c[2] for c in best) + " " + " ".join(f"[{s}]" for s in sources)
