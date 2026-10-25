"""Prompt-injection and jailbreak detection.

The detector is a set of weighted heuristic rules grouped by attack category.
Each rule that fires contributes its weight, and weights are combined with a
"noisy-OR": ``risk = 1 - prod(1 - w_i)``. That keeps the score in [0, 1],
lets several weak signals add up to a block, and means no single low-weight
rule can block on its own.

Before matching, text is normalised (Unicode NFKC, zero-width characters
removed, common leetspeak folded) so trivial obfuscation does not slip past.
Base64-looking runs are decoded and re-scanned, because "decode this and do
what it says" is a classic bypass.

Heuristics are fast, explainable and free, but they will miss novel phrasing.
The ``classifier`` hook lets an ML or LLM classifier be added; the final score
is the max of the two so the classifier can only make the detector stricter.
"""

from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})


@dataclass(frozen=True)
class _Rule:
    name: str
    category: str
    weight: float
    pattern: re.Pattern[str]


def _r(name: str, category: str, weight: float, pattern: str) -> _Rule:
    return _Rule(name, category, weight, re.compile(pattern, re.IGNORECASE))


_RULES: tuple[_Rule, ...] = (
    # --- Instruction override ---------------------------------------------
    _r("ignore_previous", "instruction_override", 0.85,
       r"\b(ignore|disregard|forget|skip|override|bypass)\b.{0,40}\b(previous|prior|above|earlier|all|your|system|the)\b.{0,25}\b(instructions?|rules?|prompts?|guidelines?|directives?|polic(y|ies)|guardrails?|restrictions?)"),
    _r("new_instructions", "instruction_override", 0.55,
       r"\b(new|updated|real|actual|true)\s+(instructions?|rules|system prompt|task)\b"),
    _r("from_now_on", "instruction_override", 0.35, r"\bfrom now on\b|\bhenceforth\b|\bfor the rest of (this|the) conversation\b"),
    _r("no_restrictions", "instruction_override", 0.6,
       r"\b(without|no|free of|remove|disable|turn off)\s+(any\s+)?(restrictions?|filters?|limits?|limitations?|guardrails?|censorship|safety)\b"),
    _r("authority_claim", "instruction_override", 0.5,
       r"\b(i am|i'm|this is)\s+(the|a|an|your)?\s*(developer|admin(istrator)?|bank manager|security team|ceo|system|root|auditor|rbi official)\b"),
    _r("debug_mode", "instruction_override", 0.6, r"\b(developer|debug|maintenance|admin|god|sudo)\s+mode\b"),
    # --- Role-play / jailbreak --------------------------------------------
    _r("dan", "jailbreak", 0.9, r"(?-i:\bDAN\b)|\bdo anything now\b|\bjailbr(e|o)ak(en)?\b"),
    _r("pretend_roleplay", "jailbreak", 0.45,
       r"\b(pretend|imagine|act as|roleplay|role-play|you are now|behave like|simulate)\b.{0,40}\b(you|an?|the)\b"),
    _r("evil_persona", "jailbreak", 0.55,
       r"\b(evil|unfiltered|uncensored|unrestricted|rogue|amoral|no[- ]?rules?)\b.{0,20}\b(ai|assistant|bot|model|version|mode|persona)\b"),
    _r("hypothetical", "jailbreak", 0.3, r"\b(hypothetically|in a fictional (story|world)|for a novel|purely theoretical)\b"),
    _r("grandma", "jailbreak", 0.45, r"\b(grandma|grandmother)\b.{0,60}\b(used to|would)\b"),
    # --- System-prompt extraction -----------------------------------------
    _r("reveal_prompt", "prompt_extraction", 0.85,
       r"\b(reveal|show|print|display|output|repeat|tell me|give me|what (is|are|were)|leak|dump|share|recite)\b.{0,40}\b(system|initial|hidden|original|secret|internal)\s+(prompt|instructions?|message|rules|configuration|config)\b"),
    _r("verbatim_above", "prompt_extraction", 0.7,
       r"\b(repeat|print|output|copy)\b.{0,30}\b(everything|all|text|words)\b.{0,30}\b(above|before|so far|verbatim)\b"),
    _r("your_instructions", "prompt_extraction", 0.5,
       r"\bwhat\b.{0,20}\b(were you|have you been)\s+(told|instructed|programmed)\b|\byour (instructions|rules|guidelines) (are|say)\b"),
    # --- Delimiter / format injection -------------------------------------
    _r("chat_tokens", "delimiter", 0.8,
       r"<\|?(im_start|im_end|system|endoftext|eot_id|start_header_id)\|?>|\[/?INST\]|<</?SYS>>"),
    _r("fake_role_tag", "delimiter", 0.6, r"(^|\n)\s*(#{1,3}\s*)?(system|assistant|developer)\s*:|</?(system|instructions?)>"),
    _r("fenced_system", "delimiter", 0.6, r"```\s*(system|instructions?)\b"),
    _r("end_of_prompt", "delimiter", 0.55, r"\b(end of (system )?prompt|-----\s*begin)\b"),
    # --- Exfiltration / tool abuse ----------------------------------------
    _r("markdown_image", "exfiltration", 0.75, r"!\[[^\]]*\]\(\s*https?://"),
    _r("send_to_url", "exfiltration", 0.65,
       r"\b(send|post|upload|forward|exfiltrate|append)\b.{0,40}\b(to|into)\b.{0,20}(https?://|webhook|my server|this url|this link)"),
    # --- PII harvesting (other people's data) -----------------------------
    _r("bulk_customer_data", "pii_harvest", 0.75,
       r"\b(list|show|give|dump|export|reveal|tell me)\b.{0,40}\b(all|every|other|another|any)\b.{0,30}\b(customers?|users?|clients?|account ?holders?)\b.{0,40}\b(data|details|aadhaar|pan|accounts?|numbers?|balances?|records?|info(rmation)?)\b"),
    _r("someone_elses", "pii_harvest", 0.65,
       r"\b(aadhaar|pan|account|card|mobile|phone|balance|address|kyc)\b.{0,30}\b(of|for|belonging to)\b\s+(my )?(friend|neighbou?r|wife|husband|boss|colleague|customer|someone|mr\.?|mrs\.?|ms\.?|another person)\b"),
    # --- Encoding tricks ---------------------------------------------------
    _r("decode_and_follow", "encoding", 0.6,
       r"\b(decode|decrypt|translate|deobfuscate|reverse)\b.{0,40}\b(base64|rot13|hex|binary|cipher|this)\b.{0,60}\b(follow|execute|obey|do|run|then)\b"),
    _r("encoding_mention", "encoding", 0.25, r"\b(base64|rot13|hexadecimal|morse code|leetspeak)\b"),
)

_BASE64_RUN = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{16,}={0,2}(?![A-Za-z0-9+/=])")


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH)
    return re.sub(r"\s+", " ", text)


def _decode_base64_runs(text: str) -> list[str]:
    decoded = []
    for run in _BASE64_RUN.findall(text):
        try:
            raw = base64.b64decode(run + "=" * (-len(run) % 4), validate=True)
            s = raw.decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        if s and sum(ch.isprintable() for ch in s) / len(s) > 0.9:
            decoded.append(s)
    return decoded


@dataclass
class InjectionResult:
    score: float
    blocked: bool
    matched_rules: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    classifier_score: float | None = None

    @property
    def top_category(self) -> str | None:
        return self.categories[0] if self.categories else None


class InjectionDetector:
    def __init__(
        self,
        threshold: float = 0.6,
        classifier: Callable[[str], float] | None = None,
    ) -> None:
        self.threshold = threshold
        self.classifier = classifier

    def _match(self, text: str) -> list[_Rule]:
        variants = {text, text.translate(_LEET)}
        return [rule for rule in _RULES if any(rule.pattern.search(v) for v in variants)]

    def scan(self, text: str) -> InjectionResult:
        clean = normalise(text)
        hits = self._match(clean)
        extra: list[tuple[str, str, float]] = []

        if any(ord(c) in _ZERO_WIDTH for c in text):
            # Invisible characters have no place in a banking question.
            extra.append(("zero_width_chars", "encoding", 0.4))

        for decoded in _decode_base64_runs(clean):
            inner = self._match(normalise(decoded))
            if inner:
                # A hidden payload that is itself an attack is a strong signal.
                extra.append(("base64_payload", "encoding", 0.9))
                hits.extend(inner)
            else:
                extra.append(("base64_blob", "encoding", 0.2))

        weighted: list[tuple[str, str, float]] = [(r.name, r.category, r.weight) for r in hits] + extra
        # De-duplicate by rule name, keep insertion order.
        seen: dict[str, tuple[str, float]] = {}
        for name, cat, w in weighted:
            seen.setdefault(name, (cat, w))

        remaining = 1.0
        for _, w in seen.values():
            remaining *= 1.0 - w
        score = round(1.0 - remaining, 4)

        # Categories ordered by strongest contribution first.
        by_cat: dict[str, float] = {}
        for cat, w in seen.values():
            by_cat[cat] = max(by_cat.get(cat, 0.0), w)
        categories = sorted(by_cat, key=by_cat.__getitem__, reverse=True)

        classifier_score = None
        if self.classifier is not None:
            classifier_score = float(self.classifier(clean))
            score = max(score, classifier_score)

        return InjectionResult(
            score=score,
            blocked=score >= self.threshold,
            matched_rules=list(seen),
            categories=categories,
            classifier_score=classifier_score,
        )
