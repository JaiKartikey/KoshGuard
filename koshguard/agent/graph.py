r"""The LangGraph agent.

    START -> input_guard --blocked--> respond
                 |
                 v
           classify_intent --greeting--> respond
                 |  \--action--> escalate -> respond
                 v
             retrieve --no context--> respond
                 |
                 v
             generate --gateway error--> respond
                 |
                 v
           output_guard --fail--> (safe fallback) -> respond
                 |
                 v
              respond -> END

Why LangGraph instead of a free-form agent loop? Every path is an explicit
edge, so the possible behaviours can be enumerated, tested and shown to an
auditor. The model is called in exactly one node and has no tools, so it
cannot decide to skip a guardrail or take an action.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from langgraph.graph import END, START, StateGraph

from koshguard.agent.prompts import TEMPLATES, build_user_prompt, format_context
from koshguard.agent.state import AgentState
from koshguard.agent.tickets import TicketStore
from koshguard.gateway.gateway import GatewayError, LLMGateway
from koshguard.guardrails.pipeline import GuardrailPipeline
from koshguard.rag.retriever import BM25Retriever

# Requests to *do* something to an account, as opposed to asking how.
_ACTION_VERBS = (
    r"(block|freeze|unblock|unfreeze|cancel|close|transfer|send|pay|move|withdraw|debit|credit|reverse|refund|"
    r"increase|raise|decrease|reduce|change|update|reset|activate|deactivate|stop|open|add|remove|link|delink|"
    r"dispute|waive)"
)
_ACTION_REQUEST = re.compile(
    rf"^\s*(please\s+)?{_ACTION_VERBS}\b"
    rf"|\b(can|could|would|will) you\s+(please\s+)?{_ACTION_VERBS}\b"
    rf"|\b(i want|i need|i'd like|i would like|kindly|help me)\s+(you\s+)?(to\s+)?{_ACTION_VERBS}\b"
    rf"|\b(do it|go ahead|right now|immediately)\b.{{0,20}}\b{_ACTION_VERBS}\b",
    re.IGNORECASE,
)
_ACTION_NAMES = re.compile(_ACTION_VERBS, re.IGNORECASE)


# Questions about *how* something works are FAQs even when they contain an
# action verb ("how often do I need to update my KYC?"), unless they ask the
# assistant itself to act ("can you block my card?").
_QUESTION = re.compile(r"^\s*(how|what|when|where|why|which|who|is|are|do|does|did|can i|could i|should i|am i|will i)\b", re.I)
_ASKS_ASSISTANT = re.compile(r"\b(can|could|would|will) you\b", re.I)


def classify_intent(text: str) -> tuple[str, str | None]:
    """Return (intent, action) where intent is action | faq."""
    if _QUESTION.search(text) and not _ASKS_ASSISTANT.search(text):
        return "faq", None
    if _ACTION_REQUEST.search(text):
        verb = _ACTION_NAMES.search(_ACTION_REQUEST.search(text).group(0))  # type: ignore[union-attr]
        return "action", verb.group(0).lower() if verb else "account_change"
    return "faq", None


@dataclass
class AgentDeps:
    pipeline: GuardrailPipeline
    retriever: BM25Retriever
    gateway: LLMGateway
    tickets: TicketStore
    system_prompt: str
    top_k: int = 3
    min_score: float = 0.5
    max_output_tokens: int = 400


def build_graph(deps: AgentDeps):
    def input_guard(state: AgentState) -> AgentState:
        with state["trace"].span("input_guard") as span:
            v = deps.pipeline.check_input(state["message"], state["vault"])
            span.set(
                blocked=v.blocked,
                reason=v.reason,
                pii=v.pii_found,
                injection_score=v.injection.score if v.injection else None,
                injection_rules=v.injection.matched_rules if v.injection else [],
                topic=v.topic.category if v.topic else None,
            )
        update: AgentState = {"input_verdict": v}
        if v.blocked:
            update.update(outcome="blocked_input", reason=v.reason, answer=TEMPLATES.get(v.reason or "", TEMPLATES["injection"]))
        return update

    def classify(state: AgentState) -> AgentState:
        with state["trace"].span("classify_intent") as span:
            v = state["input_verdict"]
            if v.topic and v.topic.category == "greeting":
                intent, action = "greeting", None
            else:
                intent, action = classify_intent(v.sanitized_text)
            span.set(intent=intent, action=action)
        update: AgentState = {"intent": intent}
        if intent == "greeting":
            update.update(outcome="greeting", reason=None, answer=TEMPLATES["greeting"])
        if action:
            update["reason"] = action
        return update

    def escalate(state: AgentState) -> AgentState:
        with state["trace"].span("escalate_to_human") as span:
            ticket = deps.tickets.create(state["user_id"], state.get("reason") or "account_change",
                                         state["input_verdict"].sanitized_text)
            span.set(ticket_id=ticket.id, action=ticket.action)
        return {
            "ticket_id": ticket.id,
            "outcome": "escalated",
            "answer": TEMPLATES["escalated"].format(ticket_id=ticket.id),
        }

    def retrieve(state: AgentState) -> AgentState:
        with state["trace"].span("retrieve") as span:
            chunks = deps.retriever.retrieve(state["input_verdict"].sanitized_text, deps.top_k, deps.min_score)
            span.set(sources=[c.chunk.id for c in chunks], scores=[c.score for c in chunks])
        update: AgentState = {"chunks": chunks}
        if not chunks:
            update.update(outcome="no_context", reason=None, answer=TEMPLATES["no_context"])
        return update

    def generate(state: AgentState) -> AgentState:
        messages = [
            {"role": "system", "content": deps.system_prompt},
            {"role": "user", "content": build_user_prompt(state["input_verdict"].sanitized_text, state["chunks"])},
        ]
        with state["trace"].span("generate") as span:
            try:
                res = deps.gateway.complete(state["user_id"], messages, max_tokens=deps.max_output_tokens)
            except GatewayError as e:
                span.set(error_kind=e.kind)
                return {"outcome": e.kind, "reason": e.kind, "answer": TEMPLATES[e.kind]}
            llm = {
                "model": res.response.model,
                "provider": res.response.provider,
                "prompt_tokens": res.response.prompt_tokens,
                "completion_tokens": res.response.completion_tokens,
                "cost_usd": res.cost_usd,
                "cached": res.cached,
                "fallback_used": res.fallback_used,
                "attempts": res.attempts,
            }
            span.set(**llm)
        return {"draft": res.response.text, "llm": llm}

    def output_guard(state: AgentState) -> AgentState:
        with state["trace"].span("output_guard") as span:
            v = deps.pipeline.check_output(state["draft"], format_context(state["chunks"]))
            span.set(passed=v.passed, failures=v.failures, grounding=v.grounding_score)
        if v.passed:
            return {"output_verdict": v, "outcome": "answered", "reason": None, "answer": state["draft"]}
        return {
            "output_verdict": v,
            "outcome": "blocked_output",
            "reason": v.failures[0],
            "answer": TEMPLATES["safe_fallback"],
        }

    def respond(state: AgentState) -> AgentState:
        with state["trace"].span("respond") as span:
            # Templates contain no placeholders; model answers may refer to
            # <CARD_1> etc. Sensitive values come back masked, never raw.
            answer = state["vault"].restore(state["answer"])
            span.set(outcome=state["outcome"])
        return {"answer": answer}

    def after_input(state: AgentState) -> str:
        return "respond" if state["input_verdict"].blocked else "classify_intent"

    def after_classify(state: AgentState) -> str:
        return {"greeting": "respond", "action": "escalate"}.get(state["intent"], "retrieve")

    def after_retrieve(state: AgentState) -> str:
        return "generate" if state["chunks"] else "respond"

    def after_generate(state: AgentState) -> str:
        return "output_guard" if "draft" in state else "respond"

    g = StateGraph(AgentState)
    g.add_node("input_guard", input_guard)
    g.add_node("classify_intent", classify)
    g.add_node("escalate", escalate)
    g.add_node("retrieve", retrieve)
    g.add_node("generate", generate)
    g.add_node("output_guard", output_guard)
    g.add_node("respond", respond)

    g.add_edge(START, "input_guard")
    g.add_conditional_edges("input_guard", after_input, ["respond", "classify_intent"])
    g.add_conditional_edges("classify_intent", after_classify, ["respond", "escalate", "retrieve"])
    g.add_edge("escalate", "respond")
    g.add_conditional_edges("retrieve", after_retrieve, ["generate", "respond"])
    g.add_conditional_edges("generate", after_generate, ["output_guard", "respond"])
    g.add_edge("output_guard", "respond")
    g.add_edge("respond", END)
    return g.compile()
