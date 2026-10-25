"""Lightweight tracing and structured JSON logging.

Each request gets a ``Trace`` with one ``Span`` per pipeline step (guardrails,
retrieval, LLM call, ...). Spans record duration and attributes such as
verdicts, token counts and cost. When the request finishes, the whole trace is
emitted as one JSON log line, which any log stack (ELK, Loki, CloudWatch) can
index without a custom parser.

Optional exporters, each enabled only when its key is set and its package is
installed:

* **LangSmith** traces LangGraph runs natively via environment variables.
* **Pydantic Logfire** receives every span through ``logfire.span``.

Attributes must never contain raw PII; callers pass tokenized text and counts.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("koshguard.trace")


class JsonFormatter(logging.Formatter):
    _SKIP = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update({k: v for k, v in record.__dict__.items() if k not in self._SKIP})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())


@dataclass
class Span:
    name: str
    start: float
    end: float | None = None
    status: str = "ok"
    attributes: dict[str, Any] = field(default_factory=dict)

    def set(self, **attrs: Any) -> None:
        self.attributes.update(attrs)

    @property
    def duration_ms(self) -> float:
        return round(((self.end or time.perf_counter()) - self.start) * 1000, 3)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "duration_ms": self.duration_ms, "status": self.status, **self.attributes}


@dataclass
class Trace:
    user_id: str
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    started: float = field(default_factory=time.perf_counter)
    spans: list[Span] = field(default_factory=list)
    _logfire: Any = None

    @contextmanager
    def span(self, name: str, **attrs: Any) -> Iterator[Span]:
        s = Span(name, time.perf_counter(), attributes=dict(attrs))
        self.spans.append(s)
        lf_ctx = self._logfire.span(name, request_id=self.request_id) if self._logfire else None
        try:
            if lf_ctx is not None:
                with lf_ctx as lf_span:
                    yield s
                    lf_span.set_attributes({k: v for k, v in s.attributes.items() if isinstance(v, (str, int, float, bool))})
            else:
                yield s
        except Exception as e:
            s.status = "error"
            s.set(error=type(e).__name__)
            raise
        finally:
            s.end = time.perf_counter()

    @property
    def duration_ms(self) -> float:
        return round((time.perf_counter() - self.started) * 1000, 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "user_id": self.user_id,
            "duration_ms": self.duration_ms,
            "spans": [s.to_dict() for s in self.spans],
        }


class Tracer:
    def __init__(self, *, langsmith_api_key: str | None = None, langsmith_project: str = "koshguard",
                 logfire_token: str | None = None) -> None:
        self.exporters: list[str] = []
        if langsmith_api_key:
            # LangGraph/LangChain pick these up and trace every graph run.
            os.environ.setdefault("LANGSMITH_TRACING", "true")
            os.environ.setdefault("LANGSMITH_API_KEY", langsmith_api_key)
            os.environ.setdefault("LANGSMITH_PROJECT", langsmith_project)
            self.exporters.append("langsmith")
        self._logfire = None
        if logfire_token:
            try:
                import logfire

                logfire.configure(token=logfire_token, service_name="koshguard", console=False)
                self._logfire = logfire
                self.exporters.append("logfire")
            except ImportError:
                logging.getLogger(__name__).warning("LOGFIRE_TOKEN set but logfire is not installed")

    def start(self, user_id: str) -> Trace:
        return Trace(user_id=user_id, _logfire=self._logfire)

    def finish(self, trace: Trace, **summary: Any) -> dict[str, Any]:
        record = {**trace.to_dict(), **summary}
        logger.info("request.complete", extra={"trace": record})
        return record
