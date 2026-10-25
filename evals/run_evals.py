"""Offline evals: retrieval hit-rate and answer correctness on a golden set.

Usage (PowerShell, from the repo root):
    python evals/run_evals.py           # offline, mock LLM
    python evals/run_evals.py --live    # real model; also runs LLM-as-judge faithfulness

Metrics
-------
* retrieval hit@1 / hit@3: the expected policy file is the top / in the top 3 chunks.
* answered rate: share of questions that produced a guarded answer (not refused).
* fact accuracy: the answer contains every ``must_include`` fact.
* faithfulness (live only): an LLM judge says whether every claim in the answer
  is supported by the retrieved context. Skipped offline, because a mock
  judging a mock would be a meaningless number.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from koshguard.agent.prompts import format_context  # noqa: E402
from koshguard.config import Settings  # noqa: E402
from koshguard.service import KoshGuard  # noqa: E402

HERE = Path(__file__).resolve().parent

JUDGE_PROMPT = """You are grading a bank chatbot answer for faithfulness.
CONTEXT:
{context}

ANSWER:
{answer}

Is every factual claim in ANSWER directly supported by CONTEXT? Reply with exactly one word: YES or NO."""


def judge(kg: KoshGuard, context: str, answer: str) -> bool:
    res = kg.gateway.complete("eval-judge", [{"role": "user", "content": JUDGE_PROMPT.format(context=context, answer=answer)}],
                              max_tokens=3)
    return res.response.text.strip().upper().startswith("YES")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()

    golden = yaml.safe_load((HERE / "golden.yaml").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        overrides: dict = {"audit_log_path": Path(tmp) / "audit.jsonl", "daily_token_budget": 10**7,
                           "rate_limit_capacity": 1000}
        if not args.live:
            overrides["llm_mode"] = "mock"
        kg = KoshGuard(Settings(**overrides))
        use_judge = args.live and kg.mode != "mock"

        rows = []
        for i, item in enumerate(golden):
            hits = kg.retriever.retrieve(item["question"], k=3)
            sources = [h.chunk.source for h in hits]
            r = kg.chat(f"eval-{i}", item["question"])
            facts_ok = all(f.lower() in r.answer.lower() for f in item["must_include"])
            faithful = judge(kg, format_context(hits), r.answer) if use_judge and r.outcome == "answered" else None
            rows.append({
                "question": item["question"], "expected": item["expected_source"],
                "hit1": bool(sources) and sources[0] == item["expected_source"],
                "hit3": item["expected_source"] in sources,
                "outcome": r.outcome, "facts_ok": facts_ok, "faithful": faithful,
            })
        mode = kg.mode

    n = len(rows)

    def rate(key: str) -> str:
        vals = [r[key] for r in rows if r[key] is not None]
        return f"{sum(vals) / len(vals) * 100:.1f}% ({sum(vals)}/{len(vals)})" if vals else "not run (offline)"

    answered = sum(r["outcome"] == "answered" for r in rows)
    summary = {
        "Retrieval hit@1": rate("hit1"),
        "Retrieval hit@3": rate("hit3"),
        "Answered (not refused)": f"{answered / n * 100:.1f}% ({answered}/{n})",
        "Fact accuracy": rate("facts_ok"),
        "Faithfulness (LLM judge)": rate("faithful"),
    }
    lines = [
        "# KoshGuard eval report", "",
        f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} with LLM mode **{mode}** on {n} golden questions.",
        "Regenerate with `python evals/run_evals.py` (add `--live` for the real model and LLM-as-judge).", "",
        "| Metric | Value |", "|---|---|", *(f"| {k} | {v} |" for k, v in summary.items()), "",
        "| Question | Expected source | hit@1 | hit@3 | Outcome | Facts OK |", "|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(f"| {r['question']} | {r['expected']} | {'yes' if r['hit1'] else 'no'} | {'yes' if r['hit3'] else 'no'} "
                     f"| {r['outcome']} | {'yes' if r['facts_ok'] else 'no'} |")
    (HERE / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"mode={mode}")
    for k, v in summary.items():
        print(f"  {k:<26} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
