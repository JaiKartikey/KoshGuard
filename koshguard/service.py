"""Composition root: builds every component once and exposes ``chat()``.

The API, the Streamlit UI (in local mode), the red-team runner and the evals
all go through this one class, so they exercise exactly the same code path.
"""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from koshguard.agent.graph import AgentDeps, build_graph
from koshguard.agent.prompts import build_system_prompt
from koshguard.agent.tickets import TicketStore
from koshguard.config import Settings, get_settings
from koshguard.gateway.gateway import LLMGateway, build_gateway
from koshguard.gateway.providers import LLMProvider
from koshguard.guardrails.injection import InjectionDetector
from koshguard.guardrails.pii import PIIVault
from koshguard.guardrails.pipeline import GuardrailPipeline
from koshguard.observability.audit import AuditLog
from koshguard.observability.metrics import Metrics
from koshguard.observability.tracing import Tracer
from koshguard.rag.retriever import BM25Retriever


@dataclass
class ChatResult:
    request_id: str
    answer: str
    outcome: str
    reason: str | None
    latency_ms: float
    verdicts: dict[str, Any] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    llm: dict[str, Any] | None = None
    ticket_id: str | None = None
    trace: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class KoshGuard:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        provider: LLMProvider | None = None,
        gateway: LLMGateway | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        s = self.settings
        # A fresh canary per process: it only has to be unguessable, not stable.
        self.canary = f"KG-{secrets.token_hex(6)}"
        self.system_prompt = build_system_prompt(self.canary)
        self.pipeline = GuardrailPipeline(
            injection_detector=InjectionDetector(threshold=s.injection_block_threshold),
            system_prompt=self.system_prompt,
            canary=self.canary,
            allowed_domains=s.allowed_link_domains,
            min_grounding=s.grounding_min_overlap,
            max_chars=s.max_message_chars,
        )
        self.retriever = BM25Retriever.from_dir(s.policies_dir)
        self.gateway = gateway or build_gateway(s, provider)
        self.tickets = TicketStore()
        self.metrics = Metrics()
        self.audit = AuditLog(s.audit_log_path, s.audit_hmac_key)
        self.tracer = Tracer(
            langsmith_api_key=s.langsmith_api_key,
            langsmith_project=s.langsmith_project,
            logfire_token=s.logfire_token,
        )
        self.graph = build_graph(
            AgentDeps(
                pipeline=self.pipeline,
                retriever=self.retriever,
                gateway=self.gateway,
                tickets=self.tickets,
                system_prompt=self.system_prompt,
                top_k=s.retrieval_top_k,
                min_score=s.retrieval_min_score,
                max_output_tokens=s.max_output_tokens,
            )
        )

    @property
    def mode(self) -> str:
        return getattr(self.gateway.provider, "name", "unknown")

    def chat(self, user_id: str, message: str) -> ChatResult:
        start = time.perf_counter()
        trace = self.tracer.start(user_id)
        state = self.graph.invoke({"user_id": user_id, "message": message, "trace": trace, "vault": PIIVault()})
        latency_ms = round((time.perf_counter() - start) * 1000, 3)

        iv = state.get("input_verdict")
        ov = state.get("output_verdict")
        verdicts = {
            "injection_score": iv.injection.score if iv and iv.injection else None,
            "injection_rules": iv.injection.matched_rules if iv and iv.injection else [],
            "pii_found": iv.pii_found if iv else {},
            "topic": iv.topic.category if iv and iv.topic else None,
            "intent": state.get("intent"),
            "output_passed": ov.passed if ov else None,
            "output_failures": ov.failures if ov else [],
            "grounding_score": ov.grounding_score if ov else None,
        }
        sources: list[str] = []
        if state.get("outcome") == "answered":
            retrieved = list(dict.fromkeys(c.chunk.source for c in state.get("chunks", [])))
            cited = [s for s in re.findall(r"\[([\w.-]+\.md)\]", state["answer"]) if s in retrieved]
            sources = list(dict.fromkeys(cited)) or retrieved
        result = ChatResult(
            request_id=trace.request_id,
            answer=state["answer"],
            outcome=state["outcome"],
            reason=state.get("reason"),
            latency_ms=latency_ms,
            verdicts=verdicts,
            sources=sources,
            llm=state.get("llm"),
            ticket_id=state.get("ticket_id"),
            trace=[sp.to_dict() for sp in trace.spans],
        )

        self.metrics.record(outcome=result.outcome, reason=result.reason, latency_ms=latency_ms,
                            pii=verdicts["pii_found"], llm=result.llm)
        self.tracer.finish(trace, outcome=result.outcome, reason=result.reason)
        # Audit stores the tokenized message only: the raw text may contain PII.
        self.audit.append(
            "chat",
            request_id=result.request_id,
            user_id=user_id,
            message=iv.sanitized_text if iv else "",
            outcome=result.outcome,
            reason=result.reason,
            verdicts=verdicts,
            sources=sources,
            model=(result.llm or {}).get("model"),
            ticket_id=result.ticket_id,
        )
        return result
