"""GroqProvider against a fake HTTP transport (no network)."""

import httpx
import pytest

from koshguard.gateway.providers import GroqProvider, ProviderError


def provider_with(handler):
    client = httpx.Client(base_url="https://api.groq.test/openai/v1", transport=httpx.MockTransport(handler))
    return GroqProvider("test-key", "unused", client=client)


MESSAGES = [{"role": "user", "content": "hi"}]


def test_parses_openai_compatible_response():
    def handler(request):
        assert request.url.path.endswith("/chat/completions")
        body = request.read().decode()
        assert '"model":"llama-3.3-70b-versatile"' in body.replace(" ", "")
        return httpx.Response(200, json={
            "model": "llama-3.3-70b-versatile",
            "choices": [{"message": {"content": "Hello"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        })

    r = provider_with(handler).complete(MESSAGES, model="llama-3.3-70b-versatile", max_tokens=10, temperature=0)
    assert (r.text, r.prompt_tokens, r.completion_tokens, r.provider) == ("Hello", 12, 3, "groq")


@pytest.mark.parametrize("status,retryable", [(429, True), (500, True), (503, True), (400, False), (401, False)])
def test_error_classification(status, retryable):
    p = provider_with(lambda req: httpx.Response(status, json={"error": "x"}))
    with pytest.raises(ProviderError) as exc:
        p.complete(MESSAGES, model="m", max_tokens=10, temperature=0)
    assert exc.value.retryable is retryable and exc.value.status == status


def test_timeout_is_retryable():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ProviderError) as exc:
        provider_with(handler).complete(MESSAGES, model="m", max_tokens=10, temperature=0)
    assert exc.value.retryable
