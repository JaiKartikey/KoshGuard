# KoshGuard learning guide

This guide walks through each component in the order it was built: what it does, why it was designed that way, and the questions an interviewer is likely to ask. Read it next to the code. Every module also starts with a docstring explaining its design.

---

## 1. Configuration (`koshguard/config.py`)

**What:** a single `pydantic-settings` class. Each field can be overridden by an environment variable or `.env`.

**Why:** security posture should be reviewable in one place. Thresholds such as the injection block score, rate limits and budgets are risk decisions, not constants buried in code.

**Likely question:** *How would you change the injection threshold in production without a deploy?*
Set `INJECTION_BLOCK_THRESHOLD` in the environment and restart. Per-tenant thresholds would need a config service, but every component already takes its thresholds as constructor arguments, so that change stays small.

---

## 2. PII protection (`guardrails/pii.py`)

**What:** detects eight Indian identifier types and replaces them with placeholders such as `<AADHAAR_1>` before anything else sees the text.

**Key ideas**
* **Checksums cut false positives.** Aadhaar uses the Verhoeff algorithm, which catches all single-digit errors and adjacent transpositions. Card numbers use Luhn. Without these checks, any 12 digits would count as an Aadhaar.
* **Fail toward protection.** A 12-digit number that fails Verhoeff is still masked as a possible account number.
* **Overlap resolution.** Detectors run in priority order and the first match for a span wins (email beats UPI, a valid card beats an account number).
* **Reversible tokenization.** The vault maps placeholders back to values. On the way out, low-risk types (email, UPI, IFSC) are restored and sensitive ones are shown masked as `XXXX1234`, the way bank statements do.

**Likely questions**
* *Why not just use Microsoft Presidio?* Presidio is the right production choice and is on the roadmap. Building it by hand shows the mechanics (checksums, overlap handling, vault design) and keeps the project dependency-light.
* *Why tokenize instead of deleting PII?* The model still needs to know that a card was mentioned, so it can say "your card <CARD_1>". Deleting the value loses that meaning.

---

## 3. Prompt-injection detection (`guardrails/injection.py`)

**What:** about 30 weighted regex rules in categories such as instruction override, jailbreak, prompt extraction, delimiter injection, exfiltration, PII harvesting, internal-info requests, fraud intent and encoding tricks.

**Key ideas**
* **Noisy-OR scoring:** `risk = 1 − Π(1 − wᵢ)`. Several weak signals add up to a block, but a single weak rule (e.g. "hypothetically", 0.3) never blocks on its own. The score stays between 0 and 1 and is explainable, because you can list exactly which rules fired.
* **Normalisation before matching:** Unicode NFKC, zero-width characters removed, leetspeak folded (`1gn0re` → `ignore`).
* **Base64 rescanning:** encoded runs are decoded and scanned again. A hidden attack is a strong signal.
* **Classifier hook:** `max(heuristic, classifier)`, so adding an ML model can only make the detector stricter.

**Honest limitation:** on the held-out red-team set, which was written after the rules and never used to tune them, 38.9% of attacks got through. Heuristics generalise poorly to new phrasing. This is the strongest argument for adding an ML classifier, and a good interview talking point.

---

## 4. Topic scope (`guardrails/topic.py`)

**Why:** in India, personalised investment advice is SEBI-regulated. A bank's support bot giving stock tips is a compliance problem, not just an off-topic answer. Legal advice has the same issue.

**Design:** block on explicit patterns, plus long messages with no banking vocabulary. Short or ambiguous messages pass, because false positives frustrate real customers.

---

## 5. Output guardrails (`guardrails/output.py`)

Input filters will miss things, so the answer is checked too:

| Check | How | Why |
|---|---|---|
| PII leak | Run the PII detector on the answer; ignore values that appear in the retrieved policy text (e.g. the bank's care email) | The model only sees placeholders, so a raw identifier in its output was made up or leaked |
| Prompt leak | A random **canary** string in the system prompt, plus 8-word overlap with the prompt | The canary gives a near-zero false-positive signal that the prompt was dumped |
| Exfiltration | Markdown images and non-allowlisted URLs | Chat UIs fetch images automatically, a known zero-click channel for leaking data |
| Grounding | Share of answer content words found in the context ≥ 0.5, and **every number must appear in the context** | A wrong FD rate or fee is the most harmful kind of hallucination for a bank |

---

## 6. Fail-closed pipeline (`guardrails/pipeline.py`)

If any guardrail raises an exception, the request is blocked. Order: length limit → PII tokenization (first, so nothing downstream sees raw PII) → injection (on the original text, so obfuscation is visible) → topic.

**Likely question:** *Fail closed hurts availability. When would you fail open?* For low-risk features (e.g. a spelling suggestion), or when a guardrail is only advisory. For a bank's customer channel, a wrong refusal costs much less than a missed attack.

---

## 7. LLM gateway (`gateway/`)

Every model call goes through one class, in this order:

1. **Token bucket rate limit** per user: allows short bursts and caps sustained load.
2. **Daily token budget** per user: stops slow, expensive abuse that a rate limit misses.
3. **Exact-match cache**, keyed on a SHA-256 of model, messages and parameters. Only temperature-0 calls are cached. The prompt already holds placeholders, so one customer's PII can never appear in another's cached answer.
4. **Model chain:** primary, then fallback. For each model:
   * a **circuit breaker** (closed → open after N failures → half-open trial after a timeout),
   * **retries with full-jitter exponential backoff**, only for retryable errors (timeouts, 429, 5xx),
5. **Cost accounting** from a configurable price table. The offline mock costs $0, so no made-up cost numbers are reported.

The Redis rate limiter uses a **Lua script** so the read-modify-write is atomic across API workers.

**Likely questions**
* *Why full jitter?* Without it, every client retries at the same moment after an outage and knocks the provider over again (the thundering-herd problem).
* *Why a circuit breaker if you already retry?* Retrying a dead provider adds latency to every request. An open breaker skips straight to the fallback.
* *Why not a semantic cache?* A similar-but-different question ("FD rate for seniors" vs "FD rate for NRIs") could get the wrong cached answer. For a bank, a miss is cheaper than a wrong answer.

---

## 8. RAG (`rag/retriever.py`)

Pure-Python **BM25** over `##` sections. Why not embeddings? The corpus is small and full of exact terms (FD, KYC, AMB, 15G), BM25 needs no model download, it is explainable, and it is deterministic, which keeps evals reproducible. A small abbreviation map expands "FD" to "fixed deposit".

---

## 9. LangGraph agent (`agent/graph.py`)

An explicit `StateGraph`. Every possible path is an edge you can draw, test and show to an auditor. The model is called in exactly one node and has **no tools**, so it cannot skip a guardrail or take an action.

**Human-in-the-loop:** requests to *do* something ("block my card", "transfer ₹5,000") create a ticket for a human officer. Questions about *how* ("how do I block my card?") are answered from policy. A successful prompt injection can, at worst, create a ticket. It can never move money.

---

## 10. Observability (`observability/`)

* **Traces:** one span per graph node with duration and verdicts, emitted as a single JSON log line per request. LangSmith and Logfire export switch on when their keys are set.
* **Metrics:** request counts, blocks by type, p50/p95 latency, tokens, cost and cache hits, served as JSON or Prometheus text.
* **Audit log:** `hash_n = H(prev_hash ‖ record_n)`. Editing, deleting or reordering any line breaks the chain from that point, and `/audit/verify` reports the first bad record. With `AUDIT_HMAC_KEY` set, H is HMAC-SHA256, so someone without the key cannot rebuild a valid chain.

**Likely question:** *Can an attacker who controls the server defeat the audit log?* With a plain hash, yes: they can rewrite the whole file. HMAC protects against anyone who can edit the file but does not have the key. Full protection means periodically anchoring the latest hash in write-once storage (S3 Object Lock, a separate logging service).

---

## 11. Red-team and evals (`redteam/`, `evals/`)

* **ASR** (attack success rate): attacks that got a model answer past both guardrail layers. **FPR**: benign queries that were blocked.
* **The improvement loop, with real numbers:** the first run on `attacks.yaml` gave **16.7% ASR**. Fixing the gaps it exposed brought that set to **0.0%**, but that number is optimistic because the rules were tuned against it. A **held-out set**, written afterwards and never used for tuning, shows **38.9% ASR**. Both sets have **0% FPR**. Being able to explain why these numbers differ (overfitting to your own test set) is worth more in an interview than a perfect score.
* **CI gate:** the build fails if the main suite's ASR or FPR goes above 5%, so a regex change that weakens a defence cannot be merged silently.
