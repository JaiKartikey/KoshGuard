"""Hash-chained, tamper-evident audit log (JSON Lines).

Each record stores the hash of the previous record, and its own hash covers
its content plus that previous hash:

    hash_n = H(prev_hash || canonical_json(record_n))

Editing, deleting or re-ordering any line breaks every hash after it, and
``verify()`` reports the first broken sequence number. This is the same idea
as a blockchain or a certificate-transparency log, minus the consensus.

With ``AUDIT_HMAC_KEY`` set, H is HMAC-SHA256, so someone who can edit the
file but does not hold the key cannot recompute a valid chain. Without a key it
is plain SHA-256: it detects accidental or naive tampering but not a
determined attacker who rewrites the whole file. Production systems would also
ship the latest hash to write-once storage periodically.

Records contain tokenized messages and verdicts only, never raw PII.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


def _canonical(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


@dataclass
class VerifyResult:
    ok: bool
    records: int
    first_bad_seq: int | None = None
    error: str | None = None


class AuditLog:
    def __init__(self, path: Path, hmac_key: str | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._key = hmac_key.encode() if hmac_key else None
        self._lock = threading.Lock()
        self._seq, self._last_hash = self._tail()

    def _hash(self, prev_hash: str, body: dict[str, Any]) -> str:
        data = prev_hash.encode() + _canonical(body)
        if self._key:
            return hmac.new(self._key, data, hashlib.sha256).hexdigest()
        return hashlib.sha256(data).hexdigest()

    def _tail(self) -> tuple[int, str]:
        if not self.path.exists():
            return 0, GENESIS
        last = None
        with self.path.open("rb") as f:
            for line in f:
                if line.strip():
                    last = line
        if last is None:
            return 0, GENESIS
        rec = json.loads(last)
        return int(rec["seq"]), rec["hash"]

    def append(self, event: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            body = {
                "seq": self._seq + 1,
                "ts": datetime.now(timezone.utc).isoformat(),
                "event": event,
                **fields,
                "prev_hash": self._last_hash,
            }
            record = {**body, "hash": self._hash(self._last_hash, body)}
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
            self._seq, self._last_hash = body["seq"], record["hash"]
            return record

    def verify(self) -> VerifyResult:
        if not self.path.exists():
            return VerifyResult(True, 0)
        prev, expected_seq, count = GENESIS, 1, 0
        with self.path.open(encoding="utf-8") as f:
            for raw in f:
                if not raw.strip():
                    continue
                try:
                    rec = json.loads(raw)
                    stored = rec.pop("hash")
                except (json.JSONDecodeError, KeyError):
                    return VerifyResult(False, count, expected_seq, "unparseable record")
                if rec.get("seq") != expected_seq:
                    return VerifyResult(False, count, expected_seq, "sequence gap or reorder")
                if rec.get("prev_hash") != prev:
                    return VerifyResult(False, count, expected_seq, "broken link to previous record")
                if not hmac.compare_digest(self._hash(prev, rec), stored):
                    return VerifyResult(False, count, expected_seq, "record content modified")
                prev, expected_seq, count = stored, expected_seq + 1, count + 1
        return VerifyResult(True, count)
