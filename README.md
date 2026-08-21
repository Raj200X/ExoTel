# meta-NFS

**General-purpose AI legal triage system for India.** A caller describes their legal situation over a phone call, the AI reasons over it using curated legal knowledge, and either provides grounded guidance or routes them to the correct human authority — **without ever issuing a legal verdict.**

> ⚠️ **This system does not provide legal advice.** It helps callers understand their options and connects them to the right authority or lawyer.

---

## What It Does

```
Caller phones in → Describes their situation → AI extracts facts
    → Deterministic Scope Gate evaluates safety rules
        → ✅ PROCEED: Retrieves relevant law, provides grounded guidance with citations
        → ❓ CLARIFY: Asks follow-up questions to understand the situation
        → 🛑 HARD STOP: Routes to correct authority (police, court, legal aid, crisis helpline)
```

**Key safety properties:**
- The **Scope Gate is deterministic code**, never model discretion — every routing decision is traceable to a specific rule ID
- The system **never issues a legal verdict** ("you will win", "they are liable") — architectural invariant, not a prompt instruction
- **Universal hard-stops** (self-harm, active litigation, criminal element) cannot be overridden by any domain rule
- **Fail-safe default**: if no rule matches or confidence is low, the system routes to a human — it never guesses

---

## Architecture

```
┌──────────────────────┐
│   Voice Front-End    │  ← Telephony + ASR + TTS (Exotel + Bhashini)
└──────────┬───────────┘
           │
┌──────────▼───────────┐
│ Conversation Manager │  ← Session state, turn orchestration
└──┬───────┬───────────┘
   │       │
┌──▼──┐    │
│Fast │    │ ┌───────────────────┐
│Model│    └►│ Document Processor│
└──┬──┘      └────────┬──────────┘
   │                  │
   └──────┬───────────┘
          │
   ┌──────▼──────┐
   │  TriageFact │  ← Structured facts (typed, explicit safety signals)
   └──────┬──────┘
          │
   ┌──────▼──────┐
   │ Scope Gate  │  ← Deterministic rules, zero model discretion
   └──┬──┬──┬────┘
      │  │  │
      │  │  └──► HARD_STOP → Routing Engine → Authority/Helpline
      │  └─────► CLARIFY   → Ask follow-up question
      └────────► PROCEED   → Knowledge Base → Deep Model → Grounded answer
```

### Components

| Component | Purpose | Status |
|-----------|---------|--------|
| **Core Data Models** | `TriageFact`, `Session`, `ScopeGateDecision` — typed contracts between all components | ✅ Built |
| **Scope Gate Engine** | Deterministic rule engine with 7 universal hard-stops + domain-specific YAML rules | ✅ Built |
| **Routing Engine** | Resolves routing categories to specific authorities with state-aware lookup | ✅ Built |
| **Conversation Manager** | Orchestrates the full pipeline with domain locking, document handling, answer-checking | ✅ Built |
| **Knowledge Base** | Curated, versioned legal documents in pgvector with hybrid retrieval | 🔲 Planned |
| **Fast Model** | Gemini Flash — fact extraction + conversational response | 🔲 Planned |
| **Deep Model** | Gemini Pro — grounded reasoning over retrieved legal text | 🔲 Planned |
| **Document Processor** | Multimodal extraction from uploaded PDFs/images | 🔲 Planned |
| **Voice Front-End** | Telephony + ASR + TTS (Exotel + Bhashini/Deepgram) | 🔲 Planned |
| **Audit Logger** | PII-redacted decision logging with tiered retention | 🔲 Planned |

---

## Legal Domains (v1)

| Domain | Statute Coverage | Scope Gate Rules |
|--------|-----------------|------------------|
| **Cheque Bounce** | NI Act §138-§142 | ✅ 15 rules (hard-stops, clarify, proceed) |
| **Consumer Disputes** | Consumer Protection Act 2019 | 🔲 Planned |
| **Tenancy** | Model Tenancy Act + MH/UP/KA state acts | 🔲 Planned |
| **Employment** | Industrial Disputes Act, Payment of Wages | 🔲 Planned |
| **Insurance** | IRDAI Regulations, Motor Vehicles Act, PMFBY | 🔲 Planned |
| **Police / FIR** | CrPC §154-§157 / BNSS equivalents | 🔲 Planned |

### Adding a new domain

Adding a domain is **purely additive** — no existing code changes required:

```
1. Add enum value           → src/metanfs/models/enums.py
2. Add Scope Gate rules     → config/scope_gate_rules/<domain>.yaml
3. Add routing table        → config/routing_tables/<domain>.json
4. Ingest knowledge base    → data/knowledge_base/<domain>/
5. Add eval cases (≥80)     → eval/<domain>/
```

---

## Quick Start

### Prerequisites

- Python 3.12+
- pip

### Setup

```bash
# Clone
git clone https://github.com/developeranil65/Metx-NFS.git
cd Metx-NFS

# Create virtual environment and install
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Run Tests

```bash
pytest tests/ -v
```

```
45 passed in 0.34s
```

Test coverage:
- **35 Scope Gate tests** — universal hard-stops, cheque bounce domain rules, fail-safe behavior, edge cases
- **10 E2E integration tests** — multi-turn workflows, hard-stop routing, document contradiction detection, session invariants

---

## Project Structure

```
metx-NFS/
├── docs/
│   ├── requirements_v1.md        # Full requirements document
│   └── architecture_v1.md        # High-level architecture + data flow diagrams
├── src/metanfs/
│   ├── models/
│   │   ├── enums.py              # Domain, Jurisdiction, ScopeGateAction, etc.
│   │   └── core.py               # TriageFact, Session, ScopeGateDecision, etc.
│   ├── scope_gate/
│   │   └── engine.py             # Deterministic rule engine (safety-critical)
│   ├── routing/
│   │   └── engine.py             # Geography-aware authority resolution
│   ├── conversation/
│   │   └── manager.py            # Pipeline orchestration
│   ├── knowledge_base/           # (planned) Vector store + retrieval
│   ├── document_processor/       # (planned) Multimodal extraction
│   ├── voice/                    # (planned) Telephony + ASR + TTS
│   └── audit/                    # (planned) PII-redacted logging
├── config/
│   ├── scope_gate_rules/
│   │   └── cheque_bounce.yaml    # 15 rules for NI Act §138
│   └── routing_tables/
│       ├── cheque_bounce.json    # State-specific court + legal aid routing
│       └── national.json         # National fallback (crisis, DLSA, IRDAI, etc.)
├── tests/
│   ├── scope_gate/
│   │   └── test_engine.py        # 35 unit tests
│   └── e2e/
│       └── test_cheque_bounce_e2e.py  # 10 integration tests
├── data/knowledge_base/          # (planned) Curated legal documents
├── eval/                         # (planned) Eval cases per domain
└── pyproject.toml
```

---

## Design Principles

1. **Safety over features** — The Scope Gate is deterministic code, never model discretion. Universal hard-stops are implemented in code (not YAML) and cannot be overridden by domain rules.

2. **Fail safe, not fail open** — If no rule matches, the system routes to a human. If a model fails, the system routes to a human. If confidence is low, the system routes to a human.

3. **Additive domain expansion** — Adding a new legal domain requires new rules + new knowledge base + new eval cases. It never requires loosening an existing domain's safety rules.

4. **Separation of concerns** — Fast model (conversational) and deep model (reasoning) are genuinely separate. The Scope Gate evaluates structured data, not model outputs directly.

5. **Grounded, not generative** — Every legal claim must cite a specific statute section from the curated knowledge base. No uncited claims.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.12+ |
| Web Framework | FastAPI |
| Models | Gemini Flash (fast) + Gemini Pro (deep) |
| Vector Store | PostgreSQL + pgvector |
| Session Store | Redis |
| Telephony | Exotel |
| ASR/TTS (Hindi) | Bhashini / Dhruva |
| ASR/TTS (English) | Deepgram / Google Cloud TTS |
| CI/CD | GitHub Actions |

---

## Documentation

- [Requirements Document](docs/requirements_v1.md) — Domain scope, personas, Scope Gate specification, eval requirements, non-goals
- [Architecture Document](docs/architecture_v1.md) — Component specs, interfaces, data flow diagrams, failure modes, technology decisions

---

## License

This project is proprietary. All rights reserved.
