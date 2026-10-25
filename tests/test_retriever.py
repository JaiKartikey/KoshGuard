import pytest

from koshguard.config import Settings
from koshguard.rag.retriever import BM25Retriever, Chunk, load_chunks

POLICIES = Settings().policies_dir


def test_chunks_by_section_with_source():
    chunks = load_chunks(POLICIES)
    assert len(chunks) > 20
    assert all(c.source.endswith(".md") and c.text for c in chunks)


@pytest.mark.parametrize(
    "query,source",
    [
        ("senior citizen FD rate", "fixed_deposits.md"),
        ("how do I block my lost debit card", "debit_card.md"),
        ("documents needed for KYC", "kyc.md"),
        ("home loan interest rate", "loans_faq.md"),
        ("escalate complaint to nodal officer", "grievance_redressal.md"),
    ],
)
def test_top_hit_is_relevant(query, source):
    hits = BM25Retriever.from_dir(POLICIES).retrieve(query, k=3)
    assert hits and hits[0].chunk.source == source


def test_unrelated_query_returns_nothing():
    assert BM25Retriever.from_dir(POLICIES).retrieve("pet insurance for dogs", min_score=0.5) == []


def test_rare_terms_outrank_common_ones():
    r = BM25Retriever([Chunk("a", "a.md", "A", "bank bank bank account"), Chunk("b", "b.md", "B", "bank nominee")])
    assert r.retrieve("bank nominee")[0].chunk.id == "b"


def test_empty_corpus_rejected():
    with pytest.raises(ValueError):
        BM25Retriever([])
