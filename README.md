# KoshGuard: Secure Conversational Banking Assistant

> *Kosh* (कोष) means treasury in Hindi.

KoshGuard is a customer-support chatbot for **KoshBank, a fictional Indian bank**. It answers banking FAQs from approved policy documents, wrapped in the security, reliability and audit controls a real bank would need before launching an AI assistant.

The chatbot is the easy part. This project is about everything around it:

* **Guardrails** against prompt injection, jailbreaks, system-prompt extraction and data exfiltration
* **PII protection** for Indian identifiers (Aadhaar with Verhoeff checksum, PAN, cards with Luhn, IFSC, account, mobile, email, UPI). The LLM never sees raw PII.
* **An LLM gateway** with rate limiting, daily token budgets, caching, retries, a circuit breaker, model fallback and cost tracking
* **Grounded answers** via RAG, with an output check that every number in the answer appears in the source policy
* **Human-in-the-loop escalation**: the assistant never executes account or money actions
* **Observability**: per-request traces, metrics (JSON and Prometheus) and a **hash-chained, tamper-evident audit log**
* **A red-team suite** with measured attack success rate (ASR) and false-positive rate (FPR), enforced in CI

Everything runs **offline with no API key**, using a deterministic mock LLM. Set `GROQ_API_KEY` to use real Llama models on Groq.

![KoshGuard Streamlit UI](docs/ui-screenshot.png)

*An answered FAQ with its source cited, and a prompt-injection attempt blocked. The side panel shows the injection score, the rules that fired, and the PAN that was detected and tokenized.*

---

## Architecture

```mermaid
flowchart LR
    U[User] --> API[FastAPI /chat]
    API --> IG[Input guardrails<br/>PII tokenize · injection · topic]
    IG -->|blocked| OUT
    IG --> INT{Intent}
    INT -->|account action| ESC[Escalate to human<br/>ticket, never execute]
    INT -->|FAQ| RAG[BM25 retrieval]
    RAG --> GW[LLM gateway<br/>rate limit · budget · cache<br/>retry · breaker · fallback]
    GW --> LLM[(Groq Llama 3.3 70B<br/>→ 3.1 8B fallback<br/>or offline mock)]
    LLM --> OG[Output guardrails<br/>PII leak · prompt leak · exfiltration · grounding]
    OG --> OUT[Answer<br/>PII masked]
    ESC --> OUT
    IG & RAG & GW & OG -.-> OBS[(Traces · metrics<br/>hash-chained audit log)]
```

The agent is a **LangGraph** state machine with explicit edges. The model is called in one node and has no tools. See [docs/architecture.md](docs/architecture.md) for the full graph, component map and threat model, and [docs/learning-guide.md](docs/learning-guide.md) for a walkthrough of every design decision.

---

## Results

All numbers below come from running the code at the commit shown in each report, in offline mock mode. Re-run the commands to reproduce them.

### Red-team ([redteam/report.md](redteam/report.md), [redteam/report_holdout.md](redteam/report_holdout.md))

| Suite | Attacks | ASR | Benign | FPR |
|---|---|---|---|---|
| Main suite, first run (before hardening) | 42 | 16.7% (7/42) | 20 | 0.0% (0/20) |
| Main suite, after hardening | 42 | **0.0%** (0/42) | 20 | **0.0%** (0/20) |
| **Held-out suite** (never used for tuning) | 18 | **38.9%** (7/18) | 10 | **0.0%** (0/10) |

Main suite after hardening, by category: direct injection 0/7, jailbreak/role-play 0/7, prompt extraction 0/7, PII exfiltration 0/7, encoding tricks 0/7, off-topic/advice 0/7.

**How to read this honestly:** the main-suite 0% is optimistic because the rules were fixed against that suite. The held-out set was written afterwards and never used to change a rule, so its **38.9%** is the better estimate of how well regex heuristics handle new phrasing. That gap is why an ML injection classifier is first on the roadmap. In mock mode the "model" never obeys injected instructions, so these numbers measure the guardrail layers alone.

### Evals ([evals/report.md](evals/report.md))

| Metric (16 golden questions) | Value |
|---|---|
| Retrieval hit@1 | 100.0% (16/16) |
| Retrieval hit@3 | 100.0% (16/16) |
| Answered (not refused) | 100.0% (16/16) |
| Fact accuracy (mock, extractive) | 93.8% (15/16) |
| Faithfulness (LLM-as-judge) | runs only with `--live` |

### Tests

128 pytest tests covering Verhoeff and Luhn validators, tokenization round-trips, injection detection, output checks, gateway retry/fallback/circuit-breaker/rate-limit/budget/cache (including Redis backends via fakeredis), the end-to-end graph, the API and audit-chain tamper detection.

---

## Features in detail

| Area | What is implemented |
|---|---|
| PII | 8 detectors with checksum validation, overlap resolution, per-request reversible vault, masking on output (`XXXX1234`) |
| Injection | 29 weighted rules in 9 categories, noisy-OR risk score (0–1), Unicode/zero-width/leetspeak normalisation, base64 decode-and-rescan, optional classifier hook |
| Topic | Banking-only scope; refuses investment advice (SEBI-regulated), legal advice and general-purpose tasks |
| Output | PII leak, system-prompt leak (secret canary + 8-gram overlap), markdown-image and URL exfiltration, grounding score + strict number support |
| Fail closed | Any guardrail exception blocks the request |
| Gateway | Per-user token bucket, daily token budget, exact-match TTL/LRU cache, retries with full-jitter backoff, per-model circuit breaker, model fallback, cost per request; in-memory or Redis (atomic Lua) |
| RAG | Pure-Python BM25 over sectioned policy docs, abbreviation expansion, cited sources |
| Agent | LangGraph: input guard → intent → (escalate \| retrieve → generate → output guard) → respond |
| Observability | Spans per node, JSON logs, `/metrics` (p50/p95 latency, blocks by type, tokens, cost, cache hits) in JSON or Prometheus format; optional LangSmith and Pydantic Logfire |
| Audit | Append-only JSONL, SHA-256 or HMAC-SHA256 hash chain, `/audit/verify` pinpoints the first tampered record; stores tokenized text only |

---

## Setup (Windows, PowerShell)

Requires Python 3.11 or newer.

```powershell
git clone https://github.com/JaiKartikey/KoshGuard.git
cd KoshGuard
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
Copy-Item .env.example .env      # optional: add GROQ_API_KEY to use the real model
```

> If activation is blocked, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.

### Run the API

```powershell
uvicorn koshguard.api.main:app --reload
```

Open http://localhost:8000/docs for interactive Swagger docs, or call it from PowerShell:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/chat `
  -ContentType "application/json" `
  -Body '{"user_id": "demo", "message": "What is the FD rate for senior citizens?"}'

Invoke-RestMethod http://localhost:8000/health
Invoke-RestMethod http://localhost:8000/metrics
Invoke-RestMethod http://localhost:8000/audit/verify
```

### Run the UI

```powershell
streamlit run ui/streamlit_app.py
```

Choose **Local engine** to run without the API, or **API** to call a running server.

### Run tests, red-team and evals

```powershell
pytest
python redteam/run_redteam.py                                   # main suite -> redteam/report.md
python redteam/run_redteam.py --suite redteam/attacks_holdout.yaml
python evals/run_evals.py                                       # -> evals/report.md
```

With `GROQ_API_KEY` set, add `--live` to the red-team and eval commands to include the real model (and the LLM-as-judge faithfulness metric).

### Optional integrations

| Integration | How to enable |
|---|---|
| Groq | `GROQ_API_KEY=...` (models: `llama-3.3-70b-versatile`, fallback `llama-3.1-8b-instant`; check Groq's model list and pricing pages, as both change) |
| Redis | `pip install redis` and `REDIS_URL=redis://localhost:6379/0` |
| LangSmith | `pip install langsmith` and `LANGSMITH_API_KEY=...` |
| Pydantic Logfire | `pip install logfire` and `LOGFIRE_TOKEN=...` |
| Keyed audit chain | `AUDIT_HMAC_KEY=<long random string>` |

---

## Project layout

```
koshguard/
  config.py              typed settings (all thresholds via env)
  service.py             composition root used by API, UI, red-team, evals
  textutil.py            shared tokenizer for retrieval and grounding
  guardrails/            pii.py · injection.py · topic.py · output.py · pipeline.py
  gateway/               providers.py · gateway.py · ratelimit.py · cache.py
  rag/                   retriever.py (BM25)
  agent/                 graph.py · state.py · prompts.py · tickets.py
  observability/         tracing.py · metrics.py · audit.py
  api/                   main.py (FastAPI)
data/policies/           8 fictional KoshBank policy documents
ui/streamlit_app.py      chat UI with guardrail verdict panel
redteam/                 attacks.yaml · attacks_holdout.yaml · run_redteam.py · reports
evals/                   golden.yaml · run_evals.py · report.md
tests/                   pytest suite (offline)
docs/                    architecture.md · learning-guide.md
.github/workflows/ci.yml tests + red-team gate + evals on every push
```

---

## Design decisions and trade-offs

| Decision | Why | Trade-off |
|---|---|---|
| **Fail closed** | A missed attack on a bank channel costs far more than a wrongly refused question | Guardrail bugs show up as refusals, so they need monitoring (`blocks_by_type{reason="guardrail_error"}`) |
| **Tokenize PII before anything else** | The LLM provider, logs, cache and audit log never hold raw identifiers | Answers can only show masked values for sensitive types |
| **Heuristic injection detection first** | Fast, free, explainable, works offline and in CI | Weak on new phrasing (38.9% held-out ASR); the classifier hook exists for this |
| **LangGraph with explicit edges** | Every path can be listed, tested and audited; the model cannot choose to skip a step | Less flexible than an autonomous agent, which is the point |
| **No tools for the model; human escalation** | Prompt injection cannot become an account action | Customers needing an action wait for a human (self-service steps are offered) |
| **BM25 instead of embeddings** | Exact banking terms, no model download, deterministic evals | Misses paraphrases with no shared words; hybrid search is on the roadmap |
| **Strict number grounding** | Wrong rates and fees are the most damaging hallucination | A real model that rephrases numbers (e.g. "1 lakh" vs "1,00,000") may be refused more often |
| **Exact-match cache, not semantic** | A semantic cache can return the answer to a *similar but different* question | Lower hit rate |
| **Custom gateway instead of Portkey/LiteLLM** | Shows the mechanics (breaker, jitter, Lua rate limiting) | More code to maintain; swapping in a managed gateway is on the roadmap |
| **Templates for refusals and escalations** | A template cannot be talked into leaking | Less conversational tone |

---

## Limitations

* **Heuristic guardrails generalise poorly.** The held-out red-team set shows 38.9% of new-phrasing attacks getting past the input guardrails (they are still answered only from policy context, and the output guard still applies).
* **The red-team and golden sets are small and written by the same developer as the rules**, so even the held-out numbers are likely optimistic. External suites (garak, PyRIT, PromptBench) would give a fairer picture.
* **Mock-mode numbers measure the guardrails, not the model.** The mock never follows injected instructions. Live numbers depend on the model and are not reported here because they have not been run.
* **The grounding check is lexical.** It cannot detect a claim built entirely from words that appear in the context but combined wrongly.
* **Metrics, tickets and rate-limit state are in-memory by default**, so they reset on restart. Redis covers rate limits, budgets and the cache; tickets and metrics would need a database and Prometheus.
* **Single-turn only.** There is no conversation memory, which also closes off multi-turn manipulation, but limits usefulness.
* **No authentication.** `user_id` is taken from the request. A real deployment would put this behind the bank's identity provider.
* Policy documents and figures are fictional.

---

## Roadmap

* Swap the custom gateway for **Portkey** or **TensorZero** (or LiteLLM) and compare.
* Add **NeMo Guardrails** or **Guardrails AI** validators, and an ML injection classifier (e.g. Prompt Guard or a fine-tuned DeBERTa) through the existing hook.
* Replace the regex PII detectors with **Microsoft Presidio** plus custom Indian recognisers.
* Hybrid BM25 + embedding retrieval with a reranker.
* Multi-turn conversations with per-session memory and conversation-level injection scoring.
* Anchor audit-log hashes in write-once storage (S3 Object Lock).
* Run the red-team suite with `--live` and against external attack corpora; track ASR over time in CI.
* OpenTelemetry export for traces and Prometheus histograms for latency.

---

## Credits

Built while following Krish Naik's Udemy course *AI Security Bootcamp: Guardrails, LLM Gateways & Observability*. KoshBank and all policy content are fictional; any resemblance to a real bank is unintended.

## License

MIT
