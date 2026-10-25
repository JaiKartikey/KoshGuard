import json

from koshguard.observability.audit import AuditLog
from koshguard.observability.metrics import Metrics, percentile
from koshguard.observability.tracing import Tracer


def _fill(log, n=5):
    for i in range(n):
        log.append("chat", request_id=f"r{i}", outcome="answered")


def test_chain_verifies_and_survives_restart(tmp_path):
    path = tmp_path / "audit.jsonl"
    _fill(AuditLog(path), 3)
    log = AuditLog(path)  # reopen: continues the chain
    log.append("chat", request_id="r3")
    assert log.verify().ok and log.verify().records == 4


def _rewrite(path, fn):
    lines = path.read_text().splitlines()
    fn(lines)
    path.write_text("\n".join(lines) + "\n")


def test_detects_modified_record(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    _fill(log)

    def tamper(lines):
        rec = json.loads(lines[2])
        rec["outcome"] = "blocked_input"
        lines[2] = json.dumps(rec)

    _rewrite(path, tamper)
    res = log.verify()
    assert not res.ok and res.first_bad_seq == 3 and "modified" in res.error


def test_detects_deleted_and_reordered_records(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    _fill(log)
    _rewrite(path, lambda lines: lines.pop(1))
    assert log.verify().first_bad_seq == 2

    path2 = tmp_path / "b.jsonl"
    log2 = AuditLog(path2)
    _fill(log2)
    _rewrite(path2, lambda lines: lines.__setitem__(slice(1, 3), [lines[2], lines[1]]))
    assert not log2.verify().ok


def test_recomputed_plain_hash_is_rejected_with_hmac(tmp_path):
    """An attacker without the key cannot forge a valid HMAC chain."""
    path = tmp_path / "audit.jsonl"
    keyed = AuditLog(path, hmac_key="secret")
    _fill(keyed, 2)
    assert keyed.verify().ok
    assert not AuditLog(path).verify().ok  # verifying with no/wrong key fails


def test_metrics_snapshot_and_prometheus():
    m = Metrics()
    for ms in [10, 20, 30, 40, 100]:
        m.record(outcome="answered", reason=None, latency_ms=ms, pii={}, llm={"prompt_tokens": 10, "completion_tokens": 5, "cost_usd": 0.001})
    m.record(outcome="blocked_input", reason="injection", latency_ms=1, pii={"PAN": 1}, llm=None)
    s = m.snapshot()
    assert s["requests_total"] == 6 and s["blocks_by_type"] == {"injection": 1}
    assert s["llm"]["prompt_tokens"] == 50 and s["pii_detected"] == {"PAN": 1}
    assert 'koshguard_blocks_total{reason="injection"} 1' in m.prometheus()
    assert percentile([1, 2, 3, 4], 50) == 2.5


def test_trace_records_spans_and_errors():
    trace = Tracer().start("u1")
    with trace.span("ok_step") as s:
        s.set(foo=1)
    try:
        with trace.span("bad_step"):
            raise ValueError
    except ValueError:
        pass
    d = trace.to_dict()
    assert [s["name"] for s in d["spans"]] == ["ok_step", "bad_step"]
    assert d["spans"][0]["foo"] == 1 and d["spans"][1]["status"] == "error"
