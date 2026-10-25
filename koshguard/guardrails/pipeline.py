"""Guardrail pipeline: one entry point for input checks and one for output checks.

The pipeline **fails closed**. If any check raises (a regex bug, a classifier
timeout, bad input), the request is treated as blocked rather than waved
through. For a bank an occasional wrongly-refused question is acceptable; an
attack that slips past because a guardrail crashed is not.

Order of input checks:

1. Length limit: cheap, and stops oversized payloads before any regex runs.
2. PII tokenization: happens first so that nothing downstream (logs, the
   LLM, the audit trail) ever sees raw identifiers.
3. Injection scan: on the original text, so obfuscated payloads are visible.
4. Topic scope.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from koshguard.guardrails.injection import InjectionDetector, InjectionResult
from koshguard.guardrails.output import OutputVerdict, check_output
from koshguard.guardrails.pii import PIIVault, summarize
from koshguard.guardrails.topic import TopicVerdict, check_topic

logger = logging.getLogger(__name__)


@dataclass
class InputVerdict:
    blocked: bool
    reason: str | None  # None | too_long | injection | off_topic | investment_advice | legal_advice | guardrail_error
    sanitized_text: str
    pii_found: dict[str, int] = field(default_factory=dict)
    injection: InjectionResult | None = None
    topic: TopicVerdict | None = None


class GuardrailPipeline:
    def __init__(
        self,
        *,
        injection_detector: InjectionDetector,
        system_prompt: str,
        canary: str,
        allowed_domains: list[str],
        min_grounding: float,
        max_chars: int,
    ) -> None:
        self.injection_detector = injection_detector
        self.system_prompt = system_prompt
        self.canary = canary
        self.allowed_domains = allowed_domains
        self.min_grounding = min_grounding
        self.max_chars = max_chars

    def check_input(self, text: str, vault: PIIVault) -> InputVerdict:
        try:
            if len(text) > self.max_chars:
                return InputVerdict(True, "too_long", "")

            sanitized, matches = vault.tokenize(text)
            pii = summarize(matches)

            injection = self.injection_detector.scan(text)
            if injection.blocked:
                return InputVerdict(True, "injection", sanitized, pii, injection)

            topic = check_topic(sanitized)
            if not topic.allowed:
                return InputVerdict(True, topic.category, sanitized, pii, injection, topic)

            return InputVerdict(False, None, sanitized, pii, injection, topic)
        except Exception:  # noqa: BLE001 - fail closed on *any* guardrail error
            logger.exception("input guardrail failed; blocking request")
            return InputVerdict(True, "guardrail_error", "")

    def check_output(self, answer: str, context: str) -> OutputVerdict:
        try:
            return check_output(
                answer,
                context,
                system_prompt=self.system_prompt,
                canary=self.canary,
                allowed_domains=self.allowed_domains,
                min_grounding=self.min_grounding,
            )
        except Exception:  # noqa: BLE001
            logger.exception("output guardrail failed; withholding answer")
            return OutputVerdict(passed=False, failures=["guardrail_error"])
