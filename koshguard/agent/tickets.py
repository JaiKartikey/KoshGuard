"""Human-in-the-loop escalation tickets.

The agent never executes money or account actions. When a customer asks for
one ("block my card", "transfer 5000 to ..."), it files a ticket for a human
officer and tells the customer how to act securely themselves. This keeps the
LLM out of the authorisation path entirely: a prompt injection can, at worst,
create a ticket, never move money.
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class Ticket:
    id: str
    user_id: str
    action: str
    sanitized_message: str  # tokenized; never raw PII
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: str = "open"


class TicketStore:
    def __init__(self) -> None:
        self._tickets: list[Ticket] = []
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def create(self, user_id: str, action: str, sanitized_message: str) -> Ticket:
        with self._lock:
            ticket = Ticket(f"KSR-{next(self._ids):06d}", user_id, action, sanitized_message)
            self._tickets.append(ticket)
            return ticket

    def all(self) -> list[Ticket]:
        with self._lock:
            return list(self._tickets)
