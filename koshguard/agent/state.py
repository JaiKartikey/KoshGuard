"""State that flows through the LangGraph agent.

LangGraph passes one state dict between nodes; each node returns only the keys
it changes. Keeping the state explicit and typed is what makes the control flow
auditable: after a run you can see exactly which verdicts led to the answer.
"""

from __future__ import annotations

from typing import Any, TypedDict

from koshguard.guardrails.output import OutputVerdict
from koshguard.guardrails.pii import PIIVault
from koshguard.guardrails.pipeline import InputVerdict
from koshguard.observability.tracing import Trace
from koshguard.rag.retriever import RetrievedChunk


class AgentState(TypedDict, total=False):
    # inputs
    user_id: str
    message: str
    trace: Trace
    vault: PIIVault
    # produced by nodes
    input_verdict: InputVerdict
    intent: str  # faq | action | greeting
    chunks: list[RetrievedChunk]
    draft: str
    output_verdict: OutputVerdict
    llm: dict[str, Any]  # model, tokens, cost_usd, cached, fallback_used, attempts
    ticket_id: str
    # final
    outcome: str  # answered | blocked_input | blocked_output | escalated | no_context | greeting | rate_limited | budget_exceeded | llm_unavailable
    reason: str | None
    answer: str
