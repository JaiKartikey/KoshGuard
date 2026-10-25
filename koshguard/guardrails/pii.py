"""PII detection and reversible tokenization for Indian banking identifiers.

Design notes
------------
* Regex alone over-matches (any 12 digits look like an Aadhaar), so identifiers
  that carry a checksum are validated: Aadhaar with Verhoeff, card numbers with
  Luhn. A number that fails its checksum is not thrown away; if it is long
  enough it is still treated as a possible account number. For a bank, masking
  a harmless number costs little while leaking a real one costs a lot.
* Detection runs in priority order and overlapping matches are resolved in
  favour of the higher-priority, more specific type (an email beats a UPI ID,
  a Luhn-valid card beats an account number).
* ``PIIVault`` replaces each value with a stable placeholder such as
  ``<AADHAAR_1>`` so the LLM can still refer to "your Aadhaar" without ever
  seeing it. The vault lives only for one request and is never logged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --- Checksums --------------------------------------------------------------

_VERHOEFF_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_VERHOEFF_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)
_VERHOEFF_INV = (0, 4, 3, 2, 1, 5, 6, 7, 8, 9)


def verhoeff_checksum(digits: str) -> int:
    """Return the Verhoeff check digit that should be appended to ``digits``."""
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _VERHOEFF_D[c][_VERHOEFF_P[(i + 1) % 8][int(ch)]]
    return _VERHOEFF_INV[c]


def verhoeff_valid(digits: str) -> bool:
    """True if ``digits`` (check digit included) passes the Verhoeff check."""
    if not digits.isdigit():
        return False
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _VERHOEFF_D[c][_VERHOEFF_P[i % 8][int(ch)]]
    return c == 0


def luhn_valid(digits: str) -> bool:
    """True if ``digits`` passes the Luhn (mod 10) check used by card numbers."""
    if not digits.isdigit() or len(digits) < 2:
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def is_valid_aadhaar(value: str) -> bool:
    digits = re.sub(r"[ -]", "", value)
    return len(digits) == 12 and digits[0] not in "01" and verhoeff_valid(digits)


# --- Detection --------------------------------------------------------------

# Lookarounds stop a pattern from matching the middle of a longer digit run.
_NO_DIGIT_BEFORE = r"(?<!\d)(?<!\d[ -])"
_NO_DIGIT_AFTER = r"(?![ -]?\d)"


@dataclass(frozen=True)
class _Rule:
    pii_type: str
    pattern: re.Pattern[str]
    validator: object = None  # Callable[[str], bool] | None


_RULES: tuple[_Rule, ...] = (
    _Rule("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")),
    # UPI handles look like emails without a dotted domain: name@okbank
    _Rule("UPI", re.compile(r"\b[A-Za-z0-9._-]{2,256}@[A-Za-z]{2,64}\b(?!\.[A-Za-z])")),
    # PAN: 5 letters (4th = holder type), 4 digits, 1 letter
    _Rule("PAN", re.compile(r"\b[A-Z]{3}[ABCFGHLJPT][A-Z]\d{4}[A-Z]\b")),
    _Rule("IFSC", re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")),
    _Rule(
        "CARD",
        re.compile(_NO_DIGIT_BEFORE + r"(?:\d[ -]?){12,18}\d" + _NO_DIGIT_AFTER),
        lambda v: 13 <= len(re.sub(r"\D", "", v)) <= 19 and luhn_valid(re.sub(r"\D", "", v)),
    ),
    _Rule(
        "AADHAAR",
        re.compile(_NO_DIGIT_BEFORE + r"[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}" + _NO_DIGIT_AFTER),
        is_valid_aadhaar,
    ),
    _Rule(
        "MOBILE",
        re.compile(r"(?<![\d+])(?:\+91[ -]?|0)?[6-9]\d{4}[ -]?\d{5}" + _NO_DIGIT_AFTER),
    ),
    # Anything else that looks like a bank account number (9-18 digits).
    _Rule("ACCOUNT", re.compile(r"(?<!\d)\d{9,18}(?!\d)")),
)

PII_TYPES: tuple[str, ...] = tuple(r.pii_type for r in _RULES)
_PLACEHOLDER_RE = re.compile(r"<(" + "|".join(PII_TYPES) + r")_(\d+)>")


@dataclass(frozen=True)
class PIIMatch:
    pii_type: str
    value: str
    start: int
    end: int


def detect_pii(text: str) -> list[PIIMatch]:
    """Return non-overlapping PII matches in ``text``, ordered by position."""
    taken: list[tuple[int, int]] = []
    found: list[PIIMatch] = []
    for rule in _RULES:
        for m in rule.pattern.finditer(text):
            start, end = m.span()
            if any(start < e and s < end for s, e in taken):
                continue
            if rule.validator is not None and not rule.validator(m.group()):  # type: ignore[operator]
                continue
            taken.append((start, end))
            found.append(PIIMatch(rule.pii_type, m.group(), start, end))
    return sorted(found, key=lambda p: p.start)


def mask_value(pii_type: str, value: str) -> str:
    """Bank-style masking that keeps only the last four characters."""
    if pii_type == "EMAIL":
        local, _, domain = value.partition("@")
        return f"{local[:1]}***@{domain}"
    tail = re.sub(r"[ -]", "", value)[-4:]
    return f"XXXX{tail}"


# --- Vault ------------------------------------------------------------------

# Types that may be shown back to the customer verbatim. Government IDs, card
# and account numbers are only ever shown masked, which mirrors how banks
# display them on statements and in apps.
DEFAULT_RESTORABLE: frozenset[str] = frozenset({"EMAIL", "UPI", "IFSC"})


def _normalise(pii_type: str, value: str) -> str:
    if pii_type in {"EMAIL", "UPI", "PAN", "IFSC"}:
        return value.upper()
    return re.sub(r"\D", "", value)[-12:] if pii_type == "MOBILE" else re.sub(r"\D", "", value)


@dataclass
class PIIVault:
    """Per-request mapping between raw PII values and placeholders."""

    _by_value: dict[tuple[str, str], str] = field(default_factory=dict)
    _by_token: dict[str, tuple[str, str]] = field(default_factory=dict)
    _counters: dict[str, int] = field(default_factory=dict)

    def _token_for(self, match: PIIMatch) -> str:
        key = (match.pii_type, _normalise(match.pii_type, match.value))
        if key not in self._by_value:
            n = self._counters.get(match.pii_type, 0) + 1
            self._counters[match.pii_type] = n
            token = f"<{match.pii_type}_{n}>"
            self._by_value[key] = token
            self._by_token[token] = (match.pii_type, match.value)
        return self._by_value[key]

    def tokenize(self, text: str) -> tuple[str, list[PIIMatch]]:
        """Replace every PII value with a placeholder. Same value -> same token."""
        matches = detect_pii(text)
        out, cursor = [], 0
        for m in matches:
            out.append(text[cursor : m.start])
            out.append(self._token_for(m))
            cursor = m.end
        out.append(text[cursor:])
        return "".join(out), matches

    def restore(self, text: str, restorable: frozenset[str] = DEFAULT_RESTORABLE) -> str:
        """Put values back: verbatim for safe types, masked for sensitive ones.

        Placeholders the vault never issued (e.g. invented by the model) are
        left untouched rather than guessed at.
        """

        def _sub(m: re.Match[str]) -> str:
            entry = self._by_token.get(m.group(0))
            if entry is None:
                return m.group(0)
            pii_type, value = entry
            return value if pii_type in restorable else mask_value(pii_type, value)

        return _PLACEHOLDER_RE.sub(_sub, text)

    @property
    def raw_values(self) -> list[str]:
        return [v for _, v in self._by_token.values()]

    def __len__(self) -> int:
        return len(self._by_token)

    def __repr__(self) -> str:  # never print raw values
        return f"PIIVault(<{len(self)} redacted values>)"


def summarize(matches: list[PIIMatch]) -> dict[str, int]:
    """Counts per type, safe to log (contains no values)."""
    counts: dict[str, int] = {}
    for m in matches:
        counts[m.pii_type] = counts.get(m.pii_type, 0) + 1
    return counts
