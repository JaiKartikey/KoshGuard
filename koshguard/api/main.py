"""FastAPI app.

Run (PowerShell):  uvicorn koshguard.api.main:app --reload

Endpoints
---------
POST /chat           -> answer + per-message guardrail verdicts
GET  /health         -> liveness, LLM mode, circuit-breaker states
GET  /metrics        -> JSON metrics (``?format=prometheus`` for Prometheus)
GET  /audit/verify   -> verifies the hash chain of the audit log

Request bodies are validated by Pydantic (length limits, user id format)
before any guardrail runs, so malformed input is rejected cheaply.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from koshguard import __version__
from koshguard.config import get_settings
from koshguard.observability.tracing import configure_logging
from koshguard.service import KoshGuard


class ChatRequest(BaseModel):
    user_id: str = Field(default="demo-user", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    message: str = Field(min_length=1, max_length=4000)


class ChatResponse(BaseModel):
    request_id: str
    answer: str
    outcome: str
    reason: str | None
    latency_ms: float
    verdicts: dict[str, Any]
    sources: list[str]
    llm: dict[str, Any] | None
    ticket_id: str | None
    trace: list[dict[str, Any]]


def create_app(service: KoshGuard | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = get_settings() if service is None else service.settings
        configure_logging(settings.log_level)
        app.state.kg = service or KoshGuard(settings)
        yield

    app = FastAPI(title="KoshGuard", version=__version__, lifespan=lifespan,
                  description="Secure conversational banking assistant for the fictional KoshBank.")

    def kg(request: Request) -> KoshGuard:
        return request.app.state.kg

    # Plain ``def`` handlers run in FastAPI's threadpool, which suits the
    # synchronous graph and HTTP client.
    @app.post("/chat", response_model=ChatResponse)
    def chat(body: ChatRequest, request: Request) -> dict[str, Any]:
        return kg(request).chat(body.user_id, body.message).to_dict()

    @app.get("/health")
    def health(request: Request) -> dict[str, Any]:
        k = kg(request)
        return {
            "status": "ok",
            "version": __version__,
            "llm_mode": k.mode,
            "models": k.gateway.models,
            "circuit_breakers": k.gateway.health(),
            "exporters": k.tracer.exporters,
        }

    @app.get("/metrics")
    def metrics(request: Request, format: str = Query("json", pattern="^(json|prometheus)$")):
        m = kg(request).metrics
        if format == "prometheus":
            return PlainTextResponse(m.prometheus())
        return m.snapshot()

    @app.get("/audit/verify")
    def audit_verify(request: Request) -> dict[str, Any]:
        r = kg(request).audit.verify()
        return {"ok": r.ok, "records": r.records, "first_bad_seq": r.first_bad_seq, "error": r.error}

    return app


app = create_app()
