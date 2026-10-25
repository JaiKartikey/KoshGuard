"""Output guardrails: checks run on the model's draft before the user sees it.

Input guardrails cannot catch everything, so the answer itself is verified:

* **PII leak**: any raw identifier in the answer that did not come from the
  retrieved policy text. The model only ever sees placeholders, so a raw
  Aadhaar or account number in its output is hallucinated or leaked.
* **System-prompt leak**: a secret canary string planted in the system prompt,
  plus long word n-grams copied from it. Canaries are cheap and give a
  near-zero false-positive signal that the prompt was dumped.
* **Exfiltration**: markdown images (which browsers fetch automatically, a
  known zero-click data-exfiltration channel) and links to non-allowlisted
  domains.
* **Grounding**: the answer's content words and, strictly, its numbers must be
  supported by the retrieved context. Numbers get the strict rule because a
  wrong FD rate or fee is the most damaging kind of hallucination for a bank.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from koshguard.guardrails.pii import detect_pii
from koshguard.textutil import content_tokens, numbers_in

_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_URL = re.compile(r"\b(?:https?://|www\.)[^\s)>\]]+", re.IGNORECASE)
_CITATION = re.compile(r"\[[^\]]+\.md\]")
_PLACEHOLDER = re.compile(r"<[A-Z]+_\d+>")
_LIST_MARKER = re.compile(r"^\s*\d+[.)]\s+", re.MULTILINE)


@dataclass
class OutputVerdict:
    passed: bool
    failures: list[str] = field(default_factory=list)
    grounding_score: float | None = None
    unsupported_numbers: list[str] = field(default_factory=list)
    leaked_pii_types: list[str] = field(default_factory=list)


def _shingles(text: str, n: int) -> set[tuple[str, ...]]:
    words = re.findall(r"[a-z0-9']+", text.lower())
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def check_pii_leak(answer: str, context: str) -> list[str]:
    context_upper = context.upper()
    return sorted(
        {m.pii_type for m in detect_pii(answer) if m.value.upper() not in context_upper}
    )


def check_prompt_leak(answer: str, system_prompt: str, canary: str, ngram: int = 8) -> bool:
    if canary and canary in answer:
        return True
    return bool(_shingles(answer, ngram) & _shingles(system_prompt, ngram))


def check_exfiltration(answer: str, allowed_domains: list[str]) -> list[str]:
    problems = []
    if _MARKDOWN_IMAGE.search(answer):
        problems.append("markdown_image")
    for url in _URL.findall(answer):
        host = urlparse(url if "://" in url else f"http://{url}").hostname or ""
        if not any(host == d or host.endswith("." + d) for d in allowed_domains):
            problems.append("untrusted_url")
            break
    return problems


def grounding(answer: str, context: str) -> tuple[float, list[str]]:
    """Return (share of answer content words found in context, unsupported numbers)."""
    body = _LIST_MARKER.sub("", _PLACEHOLDER.sub(" ", _CITATION.sub(" ", answer)))
    answer_tokens = content_tokens(body)
    if not answer_tokens:
        return 1.0, []
    ctx_tokens = set(content_tokens(context))
    score = sum(t in ctx_tokens for t in answer_tokens) / len(answer_tokens)
    unsupported = sorted(numbers_in(body) - numbers_in(context))
    return round(score, 3), unsupported


def check_output(
    answer: str,
    context: str,
    *,
    system_prompt: str,
    canary: str,
    allowed_domains: list[str],
    min_grounding: float,
) -> OutputVerdict:
    failures: list[str] = []

    leaked = check_pii_leak(answer, context)
    if leaked:
        failures.append("pii_leak")
    if check_prompt_leak(answer, system_prompt, canary):
        failures.append("system_prompt_leak")
    failures.extend(check_exfiltration(answer, allowed_domains))

    score, unsupported = grounding(answer, context)
    if score < min_grounding:
        failures.append("ungrounded")
    if unsupported:
        failures.append("unsupported_numbers")

    return OutputVerdict(
        passed=not failures,
        failures=failures,
        grounding_score=score,
        unsupported_numbers=unsupported,
        leaked_pii_types=leaked,
    )
