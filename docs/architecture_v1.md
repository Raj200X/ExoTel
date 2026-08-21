# meta-NFS v1 — High-Level Architecture

> **Status**: Draft — awaiting owner sign-off before low-level design.
> **Prerequisite**: [Requirements v1](file:///Users/raj/Desktop/metx-NFS/docs/requirements_v1.md) (approved).

---

## Resolved Open Questions (defaults assumed)

Before architecture, resolving the 7 open questions from the requirements doc with defaults. Override any of these.

| # | Question | Default Decision | Rationale |
|---|----------|-----------------|-----------|
| 1 | State selection | **MH, UP, KA** | Highest urbanization + legal-aid infrastructure; adjust with call data later |
| 2 | Language | **Hindi + English** for v1; code-switching supported | Covers ~55% of India; regional languages are additive |
| 3 | Document drafting | **Non-goal**, except RTI applications (low-risk, standardized form) added as a stretch goal | RTI is a fundamental right with a near-trivial form; everything else crosses into legal practice |
| 4 | Raw audio | **Transcript-only** for v1 | Avoids consent/storage complexity; ASR quality monitored via transcript confidence scores |
| 5 | Eval budget | **Hybrid**: 200 hand-curated + synthetic augmentation to 600 | Unblocks launch; hand-curated set grows organically with production traffic |
| 6 | Existing Munshi assets | **Clean-room build** (workspace empty) | No code or data inherited; PMFBY domain knowledge informs insurance domain design |
| 7 | Voice platform | **Decided in architecture below** — Exotel (telephony) + Bhashini/Dhruva (Indic ASR/TTS) + Deepgram (English fallback) | Exotel: strong India presence, toll-free support; Bhashini: government-backed Indic speech stack |

---

## System Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                         CALLER (phone/WhatsApp)                     │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │   Voice Front-End   │
                    │  (Telephony + ASR   │
                    │     + TTS)          │
                    └──────────┬──────────┘
                               │ text utterance / audio events
                    ┌──────────▼──────────┐
                    │ Conversation Manager│◄──────── Session Store
                    │  (Session state,    │          (Redis/in-memory)
                    │   turn orchestrator)│
                    └──┬───────┬──────────┘
                       │       │
            ┌──────────▼──┐    │
            │  Fast Model │    │ (document upload event)
            │ (fact extract│   │
            │  + response) │   ▼
            └──────┬──────┘  ┌──────────────────┐
                   │         │ Document Processor│
                   │         │ (OCR + extraction) │
                   │         └────────┬───────────┘
                   │                  │ extracted fields
                   │    ┌─────────────▼──┐
                   └───►│   TriageFact    │ (merged: spoken + document)
                        │   (structured)  │
                        └────────┬────────┘
                                 │
                      ┌──────────▼──────────┐
                      │     Scope Gate      │
                      │ (deterministic rules)│
                      └──┬──┬──┬──┬──┬──────┘
                         │  │  │  │  │
              ┌──────────┘  │  │  │  └──────────┐
              ▼             ▼  │  ▼              ▼
          PROCEED       CLARIFY│ HARD_STOP    SOFT_STOP
              │                │    │             │
              ▼                │    ▼             ▼
     ┌────────────────┐        │  ┌─────────┐  ┌──────────┐
     │ Knowledge Base │        │  │ Routing │  │ Routing  │
     │ (vector search │        │  │ Engine  │  │ + caveat │
     │  + tag filter) │        │  └────┬────┘  └─────┬────┘
     └───────┬────────┘        │       │             │
             │ relevant chunks │       ▼             ▼
     ┌───────▼────────┐        │  Route to       Route to
     │   Deep Model   │        │  authority      authority
     │ (grounded      │        │  (hard)         (soft,
     │  reasoning)    │        │                  + guidance)
     └───────┬────────┘        │
             │ answer          │
     ┌───────▼────────┐        │
     │  Scope Gate    │        │  (second pass —
     │  (answer check)│        │   verify answer
     └───────┬────────┘        │   doesn't violate
             │                 │   hard-stops)
             ▼                 │
        Response ◄─────────────┘ (clarify loops back)
             │
     ┌───────▼────────┐
     │  Audit Logger  │
     │ (decision log, │
     │  PII-redacted) │
     └────────────────┘
```

---

## Component Specifications

### 1. Voice Front-End

**Purpose**: Telephony ingress/egress, speech-to-text, text-to-speech.

**Interfaces**:
- IN: Raw audio stream from telephony provider (Exotel)
- OUT (to Conversation Manager): `Utterance { session_id, text, language_detected, asr_confidence, timestamp }`
- IN (from Conversation Manager): `Response { session_id, text, language, priority }` → synthesized as speech
- IN: Call lifecycle events (call_start, call_end, call_drop)

**Key decisions**:
- ASR: Bhashini/Dhruva for Hindi, Deepgram for English. Language detected on first utterance, switchable mid-call.
- TTS: Bhashini for Hindi, Google Cloud TTS for English (higher quality, lower latency).
- Telephony: Exotel (toll-free number, IVR-less — straight to AI after consent prompt).
- Hold/thinking prompts: Pre-recorded audio snippets ("Let me look into that…", "I'm reading your document…") played during retrieval/document processing to avoid dead air.

**Failure modes**:
| Failure | Handling |
|---------|----------|
| ASR confidence < 0.3 | "I didn't catch that clearly, could you say that again?" (max 3 retries → route to human) |
| ASR timeout (silence > 10s) | "Are you still there?" (max 2 → end call gracefully) |
| TTS failure | Fall back to pre-recorded generic prompt + SMS the response text |
| Call drop | Session state preserved 10 min; caller can redial and resume |

---

### 2. Conversation Manager

**Purpose**: Orchestrates the per-call session lifecycle. Routes utterances through the pipeline, manages turn state, enforces conversation-level invariants.

**Interfaces**:
- IN: `Utterance` from Voice Front-End; `DocumentUploadEvent` from Document Processor
- OUT: `Response` to Voice Front-End
- Reads/writes: Session Store
- Calls: Fast Model, Scope Gate, Knowledge Base, Deep Model, Routing Engine, Audit Logger

**Session state** (`Session`):
```
Session {
  session_id: uuid
  caller_phone_hash: string        // hashed, never raw
  language: enum[HI, EN]
  domain_lock: enum[Domain] | null // set by Scope Gate after confident classification
  turn_history: list<Turn>         // last 20 turns max (sliding window)
  triage_fact: TriageFact          // accumulated, merged across turns
  documents: list<ExtractedDoc>    // max 3
  scope_gate_decisions: list<ScopeGateDecision>
  state: enum[ACTIVE, CLARIFYING, RETRIEVING, ROUTING, ENDED]
  created_at: timestamp
  last_activity: timestamp
}
```

**Turn flow (happy path)**:
1. Receive `Utterance`
2. Append to `turn_history`
3. Call Fast Model with `(turn_history, current triage_fact)` → get `(updated_triage_fact, response_candidate)`
4. Call Scope Gate with `updated_triage_fact` → get `ScopeGateDecision`
5. Branch on decision:
   - `PROCEED` → need retrieval? call Knowledge Base → Deep Model → Scope Gate (answer check) → deliver response
   - `CLARIFY` → deliver clarifying question from Fast Model
   - `HARD_STOP` → call Routing Engine → deliver routing info → set state ROUTING
   - `SOFT_STOP` → deliver caveat + call Routing Engine → deliver routing as recommendation
6. Log to Audit Logger
7. Send `Response` to Voice Front-End

**Invariants enforced**:
- Max 30 turns per session (prevent infinite loops)
- Max 3 clarification loops on same fact before escalating
- Domain lock is write-once (can only be set, never changed)
- Every response passes through Scope Gate before delivery

**Failure modes**:
| Failure | Handling |
|---------|----------|
| Fast Model timeout (> 4s) | Return "Let me think about that" + retry once; if second timeout → "I'm having trouble, let me connect you to someone" → route |
| Session store unavailable | Operate stateless for current turn (degrade: no history, no domain lock check) + alert ops |
| Turn limit reached | "We've covered a lot — let me summarize and point you to the right next step" → route |

---

### 3. Fast Model

**Purpose**: Low-latency conversational model. Two jobs: (a) extract structured `TriageFact` from conversation, (b) generate conversational response candidates.

**Interfaces**:
- IN: `(turn_history: list<Turn>, current_triage_fact: TriageFact, domain_lock: Domain|null)`
- OUT: `(updated_triage_fact: TriageFact, response_candidate: string, needs_retrieval: bool, retrieval_query: string|null)`

**Model choice**: Gemini Flash (or equivalent low-latency model). Separate system prompt per task (extraction vs. response generation), run as **two parallel calls** within the same turn to stay under latency budget.

**Prompt architecture** (high-level; detailed in low-level design):
- Extraction prompt: Structured output mode. Given conversation, fill/update `TriageFact` fields. Must output valid JSON.
- Response prompt: Given conversation + TriageFact + Scope Gate constraints, generate a response that is informative but never conclusory. Hard rules injected: never say "you will win", "they are liable", "this is legal/illegal" as a final determination.

**Key constraint**: The Fast Model does **not** access the Knowledge Base. It operates on conversational context only. If it determines grounded legal information is needed, it sets `needs_retrieval = true` and formulates a `retrieval_query`.

**Failure modes**:
| Failure | Handling |
|---------|----------|
| Extraction returns invalid JSON | Retry with stricter prompt (1 retry); if still invalid, use previous turn's TriageFact unchanged + flag for review |
| Response contains verdict language | Post-generation regex filter catches verdict patterns ("you will win", "they are guilty", "this is legal"); if matched, substitute with safe alternative |
| Model refuses to respond | Use canned fallback: "I want to make sure I give you accurate information. Let me look into this more carefully." → trigger retrieval path |

---

### 4. Deep Model

**Purpose**: High-quality reasoning model for grounded legal information delivery. Only invoked when retrieval is needed — not on every turn.

**Interfaces**:
- IN: `(retrieval_query: string, retrieved_chunks: list<Chunk>, turn_history: list<Turn>, triage_fact: TriageFact)`
- OUT: `(grounded_answer: string, citations: list<Citation>, confidence: float, answer_triage_fact: TriageFact)`

**Model choice**: Gemini Pro (or equivalent reasoning-class model). Higher latency acceptable because caller hears "Let me look into that" hold prompt.

**Key constraints**:
- Must cite specific statute sections / rule clauses from retrieved chunks. No uncited claims.
- Must return `answer_triage_fact` so Scope Gate can re-evaluate (the answer itself may trigger a hard-stop — e.g., retrieval reveals a criminal element the caller didn't mention).
- Output `confidence` reflects grounding quality: did retrieved chunks actually address the query, or is the model extrapolating?

**Failure modes**:
| Failure | Handling |
|---------|----------|
| Model timeout (> 8s) | Fast Model generates a partial answer from conversation context alone + "I'd recommend verifying this with [authority]" |
| Retrieved chunks irrelevant (confidence < 0.4) | "I couldn't find specific legal provisions that address your exact situation. Let me connect you with [routing target]." |
| Answer contradicts retrieved chunks (detected by citation check) | Suppress answer → route to lawyer with context summary |

---

### 5. Scope Gate

**Purpose**: Deterministic rule engine. Evaluates `TriageFact` against hard-coded rules. Zero model discretion. This is the safety-critical component.

**Interfaces**:
- IN: `TriageFact`
- OUT: `ScopeGateDecision`

```
ScopeGateDecision {
  action: enum[PROCEED, CLARIFY, HARD_STOP, SOFT_STOP]
  reason: string                          // human-readable, logged
  rule_id: string                         // which rule fired (for audit)
  routing_target: RoutingTarget | null    // populated for HARD_STOP / SOFT_STOP
  clarification_question: string | null   // populated for CLARIFY
  missing_facts: list<string> | null      // what facts are needed for CLARIFY
}
```

**Rule evaluation order** (short-circuit):
1. **Universal hard-stops** (§4.3 of requirements) — always first, override everything
2. **Domain classification check** — is domain identified with sufficient confidence?
3. **Domain-specific hard-stops** — per domain rules (§4.4 of requirements)
4. **Completeness check** — are critical facts present for this domain?
5. **Confidence check** — is model confidence sufficient?
6. **PROCEED** — all checks pass

**Implementation**: Rule engine, not ML. Rules stored as structured data (JSON/YAML rule definitions per domain). Adding a domain = adding a new rule file — no changes to engine code.

**Rule definition format** (per domain):
```yaml
domain: CHEQUE_BOUNCE
rules:
  - id: CB_HARD_001
    description: "Criminal conspiracy context"
    condition: "triage_fact.key_facts.fraud_context == true"
    action: HARD_STOP
    routing: CRIMINAL_LAWYER
    priority: 1

  - id: CB_CLARIFY_001
    description: "Cheque amount not stated"
    condition: "triage_fact.key_facts.cheque_amount == null"
    action: CLARIFY
    clarification: "Could you tell me the amount on the cheque?"
    priority: 10

  - id: CB_SCOPE_001
    description: "In scope - standard §138 query"
    condition: "triage_fact.key_facts.cheque_amount != null AND triage_fact.key_facts.notice_sent != null"
    action: PROCEED
    priority: 100
```

**Critical invariants**:
- Rules are version-controlled and code-reviewed (they are safety-critical config, not user-editable content).
- No rule can produce `PROCEED` for a fact pattern that matches a universal hard-stop — this is enforced by evaluation order (universal hard-stops always checked first) and by a CI-time static analysis that verifies no domain rule can override a universal rule.
- Rule changes require eval suite to pass (no regression on hard-stop cases).

**Failure modes**:
| Failure | Handling |
|---------|----------|
| TriageFact has null domain after 3 turns | HARD_STOP → generic legal aid routing |
| Rule evaluation throws exception | Fail-safe: HARD_STOP → "I want to make sure you get accurate help" → generic routing |
| No rule matches (gap in rule coverage) | Fail-safe: SOFT_STOP → route with caveat |

---

### 6. Knowledge Base

**Purpose**: Curated, versioned, domain-tagged legal document store. Serves retrieval queries from the Conversation Manager.

**Interfaces**:
- IN (query): `RetrievalQuery { query_text, domain_filter: Domain, jurisdiction_filter: Jurisdiction, source_type_filter: list<SourceType> | null }`
- OUT: `list<Chunk> { text, source_doc, section_ref, domain, jurisdiction, version_date, relevance_score }`
- IN (ingestion): `IngestionRequest { document, domain, jurisdiction, source_type, version_date, gazette_ref }`

**Architecture**:
- **Vector store**: PostgreSQL + pgvector (single store, metadata-filtered queries). Simpler to operate than a managed vector DB for v1 scale.
- **Embedding model**: Gemini text-embedding (or `text-embedding-004`). All chunks embedded at ingestion time.
- **Chunk strategy**: Statute/rule text chunked by section/sub-section (natural legal document boundaries, not arbitrary token windows). Each chunk preserves its section hierarchy (e.g., "NI Act → Chapter XVII → §138 → Sub-section (1)").
- **Retrieval**: Hybrid — vector similarity (top-20) + BM25 keyword match (top-20) → reciprocal rank fusion → top-5 returned to Deep Model.

**Ingestion pipeline**:
1. Raw document (PDF/text) → section-level chunking (legal-document-aware splitter)
2. Each chunk tagged with `{domain, jurisdiction, source_type, version_date, gazette_ref, section_ref}`
3. Embedded → stored in pgvector
4. BM25 index updated
5. Validation: spot-check retrieval for 5 known queries per document to ensure chunks are retrievable

**Failure modes**:
| Failure | Handling |
|---------|----------|
| No chunks above relevance threshold (< 0.3) | Return empty → Scope Gate handles as LOW_CONFIDENCE |
| DB unavailable | Circuit breaker → Fast Model responds with general guidance + "I recommend verifying with [authority]" + SOFT_STOP |
| Stale data served (amendment missed) | `version_date` displayed in citation; monthly review catches within 30-day SLA |

---

### 7. Document Processor

**Purpose**: Handles caller-uploaded documents. Extracts structured information and merges into the session's `TriageFact`.

**Interfaces**:
- IN: `DocumentUpload { session_id, file_bytes, file_type: enum[PDF, JPEG, PNG], upload_source: enum[WHATSAPP, WEB] }`
- OUT (to Conversation Manager): `ExtractedDoc { doc_type: enum[DocType], extracted_fields: map<string, string>, extraction_confidence: float, raw_text: string }`

**Processing pipeline**:
1. File validation (size < 10MB, supported format, not malicious)
2. If image/scanned PDF → OCR (Gemini vision or Google Document AI)
3. Multimodal extraction → structured fields based on detected document type
4. Post-extraction validation: check that mandatory fields for this doc type were extracted
5. Contradiction check: compare extracted fields against spoken `TriageFact.key_facts`
6. Merge into session `TriageFact`

**Contradiction detection** (structural, not semantic):
```
For each field in extracted_fields:
  if field exists in triage_fact.key_facts:
    if extracted_value != spoken_value:
      flag CONTRADICTION on this field
```
Any contradiction → `triage_fact.document_contradicts_spoken = true` → Scope Gate handles.

**Failure modes**:
| Failure | Handling |
|---------|----------|
| File corrupted / unreadable | "I couldn't read this document. Could you try uploading it again, or describe the key details to me?" |
| Extraction confidence < 0.5 | "I could only partially read this document. Let me confirm: [state what was extracted]. Is that correct?" |
| Document type not recognized | Extract as generic text; don't attempt structured field extraction; inform caller |

---

### 8. Routing Engine

**Purpose**: Given a routing target category and caller location, resolves to a specific authority/helpline with contact details.

**Interfaces**:
- IN: `RoutingRequest { target_category: enum[RoutingTarget], domain: Domain, caller_state: State|null, caller_district: District|null, context_summary: string }`
- OUT: `RoutingResult { authority_name, authority_type, phone, address, portal_url, what_to_bring: list<string>, filing_fee: string|null, deadline_note: string|null }`

**Data source**: Static lookup table (JSON), manually curated. Structure:

```
routing_table/
  ├── national.json          // national-level authorities (NCDRC, IRDAI, NCW, etc.)
  ├── maharashtra.json       // MH-specific (district forums, rent controller, labor commissioner)
  ├── uttar_pradesh.json     // UP-specific
  ├── karnataka.json         // KA-specific
  └── fallback.json          // DLSA directory (all-India, district-level)
```

**Resolution logic**:
1. If `caller_district` known + state-specific table has entry → use state-specific
2. Else if `caller_state` known + national table has entry for `target_category` → use national
3. Else → use fallback (DLSA for caller's state, or generic NALSA helpline)

**Post-routing**: Optionally send SMS/WhatsApp to caller with routing details (name, phone, address, what to bring).

**Failure modes**:
| Failure | Handling |
|---------|----------|
| Caller location unknown | Ask: "Which city or district are you in?" If still unknown → national-level authority |
| No entry in routing table for this target+location | Fallback to DLSA + log gap for ops team to fill |

---

### 9. Audit Logger

**Purpose**: Immutable, PII-redacted log of all decisions and interactions for compliance and debugging.

**Interfaces**:
- IN: `AuditEvent { session_id, event_type, timestamp, payload }`
- Event types: `CALL_START, UTTERANCE, TRIAGE_FACT_UPDATE, SCOPE_GATE_DECISION, RETRIEVAL_QUERY, RETRIEVAL_RESULT, DEEP_MODEL_RESPONSE, DOCUMENT_UPLOAD, DOCUMENT_EXTRACTION, ROUTING_DECISION, CALL_END`

**Storage**:
- Decision log (TriageFact + ScopeGateDecision + RoutingResult): **1 year retention**, no PII, append-only store (PostgreSQL or cloud logging)
- Transcript log (PII-redacted utterances): **90 days retention**, encrypted at rest
- Document store (uploaded files): **30 days retention**, encrypted, auto-deleted, caller can request immediate deletion

**PII redaction pipeline**:
- Phone numbers, Aadhaar numbers, PAN, names, addresses → detected and replaced with `[PHONE]`, `[ID]`, `[NAME]`, `[ADDRESS]` tokens before logging
- Redaction runs on all text before it enters the transcript log
- Raw (un-redacted) text exists only in memory during the call; never persisted

---

## Data Flow Diagrams

### Happy Path: Voice Call with Retrieval

```
Caller speaks → ASR → "My landlord gave me 7 days notice to vacate"
  → Conversation Manager receives Utterance
  → Fast Model extracts TriageFact:
      domain: TENANCY
      jurisdiction: {IN, MH}
      involves_criminal: false
      caller_intent: UNDERSTAND_RIGHTS
      key_facts: {notice_period_given: "7 days", tenancy_type: null}
      missing_critical_facts: ["tenancy_type", "lease_duration"]
      confidence: 0.75
  → Scope Gate evaluates:
      - No universal hard-stops triggered
      - Domain: TENANCY (confidence 0.75 ≥ 0.7) → domain lock
      - Missing facts: tenancy_type → CLARIFY
  → Response: "I understand — your landlord has given you 7 days to vacate.
               To help you better, could you tell me: do you have a written
               rent agreement, and how long have you been staying there?"
  → Audit: log TriageFact + CLARIFY decision

Caller responds → "Yes written agreement, 2 years, in Pune"
  → Fast Model updates TriageFact:
      key_facts: {notice_period_given: "7 days", tenancy_type: "written",
                  lease_duration: "2 years", city: "Pune"}
      missing_critical_facts: []
      confidence: 0.85
      needs_retrieval: true
      retrieval_query: "Maharashtra rent control act notice period written lease"
  → Scope Gate: PROCEED (all facts present, no hard-stops)
  → Knowledge Base: query with domain=TENANCY, jurisdiction=MH
      → returns chunks from MH Rent Control Act §15 (notice provisions)
  → Deep Model: reasons over chunks + facts
      → "Under the Maharashtra Rent Control Act 1999, Section 15, a landlord
         must provide reasonable notice, typically one month for monthly tenancies.
         A 7-day notice for a 2-year written tenancy would generally not meet this
         requirement. [Citation: MH Rent Control Act 1999, §15(1)]
         I'd recommend: (1) respond to the notice in writing stating your position,
         (2) consult with a lawyer or your local Rent Controller for formal guidance."
      → confidence: 0.82
      → citations: [{source: "MH Rent Control Act 1999", section: "§15(1)", version: "2024-01-15"}]
  → Scope Gate (answer check): no verdict language, citations present, confidence ≥ 0.5 → PROCEED
  → Response delivered to caller via TTS
  → Routing Engine: proactively include Rent Controller info for Pune district
  → Audit: log full chain
```

### Hard-Stop Path: Criminal Element Detected

```
Caller: "My employer hasn't paid me in 3 months and when I asked, he
         threatened to have me beaten up"
  → Fast Model extracts:
      domain: EMPLOYMENT
      involves_criminal: true  ← threat of violence
      caller_intent: UNDERSTAND_RIGHTS
  → Scope Gate: UNIVERSAL HARD-STOP (involves_criminal == true)
      rule_id: UNIVERSAL_HARD_001
  → Routing Engine: police helpline (100/112) + labor commissioner
  → Response: "I'm concerned about the threat you described. That's a serious
               matter that goes beyond a wage dispute. I'd recommend:
               (1) If you feel unsafe, please call the police at 100 or 112.
               (2) For the wage issue, you can file a complaint with the Labour
               Commissioner at [specific office for their district].
               I can send you these details by SMS. Would that help?"
  → Audit: log HARD_STOP + routing + reason
```

### Document Upload Path

```
[Mid-conversation, domain locked to INSURANCE]
Caller uploads insurance claim rejection letter via WhatsApp
  → Document Processor:
      - OCR (image) → text
      - Extract: {rejection_reason: "pre-existing condition exclusion",
                  policy_clause: "Clause 4.3", claim_amount: "₹2,50,000",
                  rejection_date: "2024-08-01"}
      - Contradiction check: caller said "no exclusions mentioned" but
        document cites Clause 4.3 exclusion
      → document_contradicts_spoken = true
  → Scope Gate: SOFT_STOP (document contradiction)
  → Response: "I've read the rejection letter. There's something I want to flag:
               you mentioned no exclusions were discussed, but the rejection letter
               cites Clause 4.3 of your policy as a pre-existing condition exclusion.
               This discrepancy is important — I'd recommend having a lawyer or the
               Insurance Ombudsman review both your policy document and this rejection
               letter together. The Insurance Ombudsman for your area is:
               [specific ombudsman office]."
  → Audit: log SOFT_STOP + contradiction details
```

---

## Cross-Cutting Concerns

### Adding a New Domain (additive guarantee)

To add domain `X` to meta-NFS, the following are required — and **nothing else**:

| Artifact | Purpose |
|----------|---------|
| `knowledge_base/domain_X/` | Curated source documents, chunked and ingested |
| `scope_gate/rules/domain_X.yaml` | Domain-specific Scope Gate rules |
| `routing_table/domain_X_routing.json` | Routing targets for this domain |
| `eval/domain_X/` | ≥ 80 eval cases (in-scope + hard-stop + boundary) |
| Update `domain_registry.json` | Register the domain enum value + metadata |

No changes to Scope Gate engine code, Conversation Manager, Fast/Deep Model infrastructure, or other domains' rules.

### Security Boundaries

```
┌─────────────────────────────────────────┐
│ Trust Boundary: Internet-facing         │
│  Voice Front-End, Document upload       │
│  ─ Input validation, rate limiting      │
│  ─ File type/size validation            │
│  ─ No direct DB access                  │
└─────────────────────┬───────────────────┘
                      │ validated input only
┌─────────────────────▼───────────────────┐
│ Trust Boundary: Application core        │
│  Conversation Manager, Models,          │
│  Scope Gate, Knowledge Base             │
│  ─ All model outputs pass Scope Gate    │
│  ─ No raw user input reaches DB queries │
└─────────────────────┬───────────────────┘
                      │ structured events only
┌─────────────────────▼───────────────────┐
│ Trust Boundary: Data stores             │
│  Session Store, Vector DB, Audit Log    │
│  ─ Encrypted at rest                    │
│  ─ Access-logged                        │
│  ─ PII redaction before transcript log  │
└─────────────────────────────────────────┘
```

### Technology Stack Summary

| Component | Technology | Rationale |
|-----------|-----------|-----------|
| Runtime | Python 3.12+ (async) | ML ecosystem, Gemini SDK, fast prototyping |
| Web framework | FastAPI | Async, WebSocket support for streaming, good for API-first |
| Telephony | Exotel | India-native, toll-free, good API |
| ASR (Hindi) | Bhashini / Dhruva API | Government-backed, free/low-cost, Indic language focus |
| ASR (English) | Deepgram | Low latency, high accuracy |
| TTS (Hindi) | Bhashini | Matches ASR stack |
| TTS (English) | Google Cloud TTS | High quality |
| Fast Model | Gemini Flash | Low latency, structured output |
| Deep Model | Gemini Pro | Reasoning quality |
| Document understanding | Gemini Pro Vision | Multimodal, good on Indian documents |
| Vector store | PostgreSQL + pgvector | Simple ops, metadata filtering, battle-tested |
| BM25 | PostgreSQL full-text search (tsvector) | Co-located with vector store, no extra infra |
| Session store | Redis | Fast, TTL support for session expiry |
| Audit log | PostgreSQL (append-only table) | Queryable, retentive |
| Deployment | Docker + Cloud Run (or equivalent) | Stateless app tier, auto-scaling |
| CI/CD | GitHub Actions | Eval suite runs on every PR |

---

## Open Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| Bhashini ASR quality may be insufficient for noisy phone audio | Callers misunderstood → wrong TriageFact → wrong Scope Gate decision | Fallback to Deepgram multilingual model; ASR confidence threshold triggers re-prompt |
| Hindi legal terminology may not embed well with general-purpose embedding models | Poor retrieval for Hindi queries against English statute text | Bilingual embedding model; query translation (Hindi → English) before retrieval; eval explicitly measures Hindi retrieval quality |
| Scope Gate rule coverage gaps across 6 domains | Unhandled fact patterns fall through | Fail-safe default (SOFT_STOP + route); gap detection via logging of "no rule matched" events; weekly rule review |
| 30-day staleness window for law amendments | Serving outdated guidance | Prominent "knowledge current as of" disclosure; critical amendments (e.g., new CrPC → BNSS transition) tracked out-of-band |
| Latency budget tight for retrieval path (~5s total) | Caller experience degrades | Pre-retrieval hold audio; async retrieval while Fast Model delivers preliminary response; chunked TTS streaming |
