"""Small text helpers shared by the retriever and the grounding check.

Both need the same idea of a "content word", so they share one tokenizer:
lower-case, alphanumeric runs, stop-words dropped and a very light suffix
stemmer. It is crude compared to a real NLP pipeline, but it has no
dependencies and behaves identically on every machine, which matters for
reproducible red-team and eval numbers.
"""

from __future__ import annotations

import re

STOPWORDS: frozenset[str] = frozenset(
    """a an and are as at be been but by can could do does did for from has have how i if in into is it its
    me my of on or our please so than that the their them then there these they this to us was we were what
    when where which who why will with would you your yours about also any all just more most other some such
    only own same too very s t don should now get got want need know tell let like may might must shall i'm
    am per up out over under again further once here both each few nor not no""".split()
)

_TOKEN = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")


def stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def content_tokens(text: str) -> list[str]:
    return [stem(t) for t in _TOKEN.findall(text.lower()) if t not in STOPWORDS]


_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?![\w])")


def numbers_in(text: str) -> set[str]:
    """Numbers normalised so '1,00,000' and '100000' compare equal; '7.10' == '7.1'."""
    out = set()
    for raw in _NUMBER.findall(text):
        n = raw.replace(",", "")
        if "." in n:
            n = n.rstrip("0").rstrip(".")
        out.add(n)
    return out
