"""BM25 retrieval over the KoshBank policy documents.

Why BM25 and not embeddings? The corpus is small, domain-specific and full of
exact terms (FD, KYC, UPI, AMB, 15G) where lexical matching is strong. BM25
needs no model download or GPU, is fully explainable ("these words matched"),
and gives identical results on every machine, which keeps evals reproducible.
A hybrid BM25 + embedding retriever is a natural upgrade (see README roadmap).

Documents are chunked by ``##`` section so each chunk is one coherent policy
topic and can be cited precisely as ``file.md``.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from koshguard.textutil import content_tokens


# Banking abbreviations customers use that the policy text spells out.
_EXPANSIONS = {
    "fd": "fixed deposit",
    "fds": "fixed deposit",
    "amb": "average monthly balance",
    "kyc": "kyc know your customer",
    "tds": "tds tax deducted at source",
    "emi": "emi loan repayment",
    "atm": "atm cash withdrawal",
}


def expand_query(query: str) -> str:
    words = re.findall(r"[a-z0-9]+", query.lower())
    return " ".join(_EXPANSIONS.get(w, w) for w in words)


@dataclass(frozen=True)
class Chunk:
    id: str
    source: str
    title: str
    text: str


@dataclass(frozen=True)
class RetrievedChunk:
    chunk: Chunk
    score: float


def load_chunks(policies_dir: Path) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in sorted(Path(policies_dir).glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        doc_title = next((ln[2:].strip() for ln in raw.splitlines() if ln.startswith("# ")), path.stem)
        for i, section in enumerate(re.split(r"^## ", raw, flags=re.M)[1:]):
            heading, _, body = section.partition("\n")
            body = body.strip()
            if body:
                chunks.append(
                    Chunk(
                        id=f"{path.stem}#{i}",
                        source=path.name,
                        title=f"{doc_title} > {heading.strip()}",
                        text=body,
                    )
                )
    return chunks


class BM25Retriever:
    """Okapi BM25 (k1=1.5, b=0.75), implemented in ~30 lines of pure Python."""

    def __init__(self, chunks: list[Chunk], k1: float = 1.5, b: float = 0.75) -> None:
        if not chunks:
            raise ValueError("no policy chunks to index")
        self.chunks = chunks
        self.k1, self.b = k1, b
        # Index title + body so headings like "Premature withdrawal" count.
        self._docs = [Counter(content_tokens(f"{c.title} {c.text}")) for c in chunks]
        self._lengths = [sum(d.values()) for d in self._docs]
        self._avgdl = sum(self._lengths) / len(self._lengths)
        df: Counter[str] = Counter()
        for d in self._docs:
            df.update(d.keys())
        n = len(chunks)
        self._idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    @classmethod
    def from_dir(cls, policies_dir: Path) -> BM25Retriever:
        return cls(load_chunks(policies_dir))

    def _score(self, query: list[str], i: int) -> float:
        doc, dl = self._docs[i], self._lengths[i]
        score = 0.0
        for t in query:
            tf = doc.get(t)
            if tf:
                norm = tf + self.k1 * (1 - self.b + self.b * dl / self._avgdl)
                score += self._idf[t] * tf * (self.k1 + 1) / norm
        return score

    def retrieve(self, query: str, k: int = 3, min_score: float = 0.0) -> list[RetrievedChunk]:
        q = list(dict.fromkeys(content_tokens(expand_query(query))))
        scored = [RetrievedChunk(c, round(self._score(q, i), 4)) for i, c in enumerate(self.chunks)]
        scored = [s for s in scored if s.score > min_score]
        return sorted(scored, key=lambda s: s.score, reverse=True)[:k]
