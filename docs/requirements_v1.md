# meta-NFS v1 — Requirements Document

> **Status**: Draft — awaiting owner sign-off before architecture work begins.
> **Scope**: General-purpose AI legal triage voice/phone line. India-focused, central + select state law, v1.

---

## 1. Legal Domain Coverage

### 1.1 In-scope domains for v1 (6 domains, deliberately bounded)

| # | Domain | Rationale |
|---|--------|-----------|
| 1 | **Consumer disputes** (Consumer Protection Act 2019) | High volume, well-codified, clear forum structure |
| 2 | **Tenancy / rent** (central model act + top-3 state Rent Control Acts) | Extremely common caller profile, bounded statute set |
| 3 | **Employment / labor basics** (Industrial Disputes Act, Payment of Wages, Shops & Establishments) | Gig/contract worker segment, frequent wage/termination queries |
| 4 | **Insurance claims** (IRDAI regulations, Motor Vehicles Act Ch. XI, PMFBY for agri) | Carries over Munshi work; document-heavy, good test of extraction pipeline |
| 5 | **Police / FIR procedure** (CrPC §154-§157 / BNSS equivalents, RTI for status) | "What do I do first?" queries — high impact, low legal complexity |
| 6 | **Cheque bounce / Negotiable Instruments Act** (NI Act §138-§142) | Extremely formulaic process, ideal for deterministic triage |

> [!NOTE]
> **Assumption A1**: India is the single jurisdiction for v1. Rationale: largest addressable user base for a Hindi + English voice line; statute text is publicly available; Munshi prototype was India-focused. **Changeable** — flag if you want a different country.

> [!NOTE]
> **Assumption A2**: We handle state-level variation for tenancy and employment only (top-3 states by call volume — proposed: Maharashtra, UP, Karnataka). All other domains are central-law-only for v1. State coverage expands additively in later versions.

### 1.2 Explicitly out-of-scope / universal hard-stop domains

These domains trigger an **immediate hard-stop and routing to a human**, regardless of caller input:

| Category | Examples |
|----------|----------|
| Active criminal proceedings | Caller is accused/arrested; bail matters; ongoing investigation where caller is suspect |
| Serious criminal allegations | Domestic violence (beyond protection-order info), sexual offences, dowry harassment — route to NCW/police helpline |
| Matters with live court deadline | "I have a hearing in 3 days" — route to lawyer, do not advise |
| Family law involving minors | Custody, guardianship, child support — route to family court / legal aid |
| Tax / revenue litigation | Complex, high-stakes, specialist domain |
| Property title disputes | Fact-intensive, evidence-dependent, title searches needed |
| Constitutional / PIL matters | Out of scope entirely |
| Corporate / securities / banking regulation | Not a consumer-caller profile |
| Anything requiring evidence assessment | System cannot weigh evidence; route out |

---

## 2. User / Persona Coverage

### 2.1 Caller archetypes (5 personas)

| # | Archetype | Profile | Literacy / Language | Typical query |
|---|-----------|---------|---------------------|---------------|
| 1 | **Urban consumer** | Middle-class, purchased a defective product or paid for an undelivered service | Literate, Hindi or English, smartphone user | "Company won't refund me, what forum do I go to?" |
| 2 | **Gig / contract worker** | Delivery rider, domestic worker, small-shop employee; may not have a written contract | Semi-literate, Hindi/regional, feature phone possible | "I was fired without notice, am I owed anything?" |
| 3 | **Small tenant** | Renting residential property; lease may be oral or a basic agreement | Literate to semi-literate, Hindi or English | "Landlord is demanding I vacate in 7 days, is that legal?" |
| 4 | **Rural / semi-urban complainant** | First-time interaction with legal process; needs to file an FIR, RTI, or insurance claim | Semi-literate, Hindi, limited formal-process exposure | "Police won't register my complaint, what can I do?" |
| 5 | **Small business owner / shopkeeper** | Cheque bounce, supplier dispute, wage compliance question | Literate, Hindi or English, may have basic documents | "Customer's cheque bounced, what are my options?" |

### 2.2 Language support

> [!NOTE]
> **Assumption A3**: v1 supports **Hindi** and **English** (voice + text). Multilingual (Marathi, Kannada, Tamil, etc.) is roadmap v2. The voice front-end must handle Hindi-English code-switching, which is the dominant real-world speech pattern.

### 2.3 Accessibility baseline

- Voice-first interaction (phone call) — the primary interface. No app install required.
- Optional document upload via WhatsApp or a simple web link sent by SMS during the call.
- System must tolerate noisy audio, incomplete sentences, and non-linear narration (caller jumps between topics).

---

## 3. Knowledge Base / Grounding Requirements

### 3.1 Authoritative source set per domain

Every retrieval-grounded answer must cite from **this curated set only** — no open web in the primary path.

| Domain | Source Documents | Source Type |
|--------|-----------------|-------------|
| **Consumer disputes** | Consumer Protection Act 2019 (full text); Consumer Protection Rules 2020; E-Commerce Rules 2020; Consumer Commission procedure rules; NCDRC/SCDRC/DCDRC jurisdiction rules | Statute + Rules |
| **Tenancy** | Model Tenancy Act 2021; Maharashtra Rent Control Act 1999; UP Urban Buildings Act 1972; Karnataka Rent Act 1999; relevant state rules/notifications | Statute (central + 3 state) |
| **Employment / labor** | Industrial Disputes Act 1947; Payment of Wages Act 1936; Shops & Establishments Acts (MH, UP, KA); Code on Wages 2019 (where notified); EPF & MP Act 1952 (basic); Payment of Gratuity Act 1972 | Statute + select rules |
| **Insurance claims** | Insurance Act 1938 (select sections); IRDAI (Protection of Policyholders' Interests) Regulations 2017; Motor Vehicles Act 1988 Ch. XI; PMFBY Operational Guidelines (latest season); Insurance Ombudsman Rules 2017 | Statute + Regulations + Guidelines |
| **Police / FIR** | CrPC §154-§157 (or BNSS §173-§175 post-transition); RTI Act 2005 §6-§7; relevant state police SOPs (top-3 states) | Statute + SOP |
| **Cheque bounce** | Negotiable Instruments Act 1881 §138-§142; relevant Supreme Court rulings on §138 procedure (curated set of ~10 landmark judgments) | Statute + Case law (curated) |

### 3.2 Knowledge store architecture

> [!NOTE]
> **Assumption A4**: Unified vector store with **mandatory metadata tags**: `domain`, `jurisdiction` (central / state+name), `source_type` (statute / rule / guideline / case_law), `version_date`, `amendment_status` (current / superseded). Retrieval queries are always filtered by domain + jurisdiction before similarity search. This is simpler to operate than N separate stores and still prevents cross-domain contamination via hard tag filters.

### 3.3 Versioning and staleness policy

| Aspect | Policy |
|--------|--------|
| **Amendment tracking** | Monthly manual review of gazette notifications for the 6 in-scope domains. Automated alert via India Code RSS / gazette scraper where available. |
| **Re-ingestion trigger** | Any amendment, superseding notification, or new operational guideline triggers re-chunking + re-embedding of the affected source. Old version is marked `superseded`, retained for audit but excluded from retrieval. |
| **Version label** | Every chunk carries `version_date` and `gazette_ref` (notification number). |
| **Staleness SLA** | Source documents must reflect law as of no more than **30 days prior** to the current date. System displays "knowledge current as of [date]" to caller. |

> [!IMPORTANT]
> **Assumption A5**: For v1, amendment tracking is a **manual, scheduled process** (ops team reviews monthly). Fully automated legislative-change detection is roadmap. This is a known risk — flag if you want automated from day 1.

---

## 4. Scope Gate Requirements

### 4.1 Architecture invariant

The Scope Gate is **deterministic code** (rule engine + threshold checks), not model discretion. The model's role is to extract structured facts from the conversation; the Scope Gate evaluates those facts against rules.

### 4.2 Structured fact extraction (model → Scope Gate input)

The fast model must produce a structured `TriageFact` object per conversational turn:

```
TriageFact {
  detected_domain: enum[Domain] | UNKNOWN
  detected_jurisdiction: {country, state?}
  involves_criminal_element: bool
  involves_minor_children: bool
  has_active_litigation: bool
  has_court_deadline: bool
  has_self_harm_violence: bool
  document_present: bool
  document_contradicts_spoken: bool | null
  caller_intent: enum[UNDERSTAND_RIGHTS, FILE_COMPLAINT, DISPUTE_RESOLUTION, CHECK_ELIGIBILITY, PROCESS_GUIDANCE, OTHER]
  confidence_score: float [0,1]  // model's self-assessed confidence on domain + fact extraction
  key_facts: map<string, string>  // domain-specific fact slots
  missing_critical_facts: list<string>  // facts needed but not yet stated
}
```

### 4.3 Universal hard-stops (fire regardless of domain)

These are evaluated **before** any domain-specific logic:

| Trigger | Condition on `TriageFact` | Action |
|---------|---------------------------|--------|
| Criminal element | `involves_criminal_element == true` | Hard-stop → police helpline / criminal lawyer routing |
| Minor children (family context) | `involves_minor_children == true AND detected_domain IN {FAMILY, TENANCY, EMPLOYMENT}` | Hard-stop → legal aid / family court |
| Active litigation | `has_active_litigation == true` | Hard-stop → "consult your lawyer of record" |
| Court deadline | `has_court_deadline == true` | Hard-stop → urgent lawyer routing |
| Self-harm / violence | `has_self_harm_violence == true` | Hard-stop → crisis helpline (iCall / Vandrevala) + police if needed |
| Document contradiction | `document_contradicts_spoken == true` | Soft-stop → flag discrepancy to caller, recommend lawyer review |
| Domain unknown | `detected_domain == UNKNOWN AND confidence_score < 0.4` after 3 clarification attempts | Hard-stop → generic legal aid routing |
| Low confidence | `confidence_score < 0.5` after retrieval pass | Soft-stop → "I'm not confident enough to guide you here" → lawyer routing |

### 4.4 Domain-specific Scope Gate rules (per domain)

Each domain defines its own rule set. Examples for v1 domains:

**Consumer disputes:**
- IN-scope: defective goods/services, unfair trade practice, e-commerce complaint, forum selection guidance, complaint drafting guidance
- Hard-stop: claim amount > ₹10 crore (National Commission — needs a real lawyer), medical negligence claims (specialist domain)

**Tenancy:**
- IN-scope: notice period validity, rent increase legality, security deposit disputes, eviction grounds
- Hard-stop: illegal encroachment / land grab (criminal), commercial lease > ₹X (specialist)

**Employment:**
- IN-scope: wage non-payment, notice period, gratuity eligibility, PF withdrawal process
- Hard-stop: sexual harassment at workplace (route to ICC / SHe-box), ongoing termination dispute past limitation period

**Insurance:**
- IN-scope: claim filing process, rejection challenge, ombudsman complaint, PMFBY claim status
- Hard-stop: suspected insurance fraud (either side), subrogation disputes

**Police / FIR:**
- IN-scope: how to file FIR, zero FIR, online FIR portals, RTI for FIR status, complaint to SP/DGP
- Hard-stop: caller is the accused, matter involves ongoing investigation

**Cheque bounce:**
- IN-scope: §138 notice drafting guidance, filing timeline, court procedure, interim compensation
- Hard-stop: cheque amount in context of broader fraud/criminal conspiracy

### 4.5 Domain creep prevention

1. **Single-domain lock**: Once the Scope Gate assigns a domain (with confidence ≥ 0.7), the conversation is locked to that domain. If new facts suggest a different domain, the system explicitly says: *"This sounds like it may involve [other domain], which is different from what we've been discussing. I'd recommend speaking to a [specialist type] about that aspect."*
2. **Multi-domain detection**: If initial fact extraction returns two plausible domains (both ≥ 0.5), system asks a disambiguating question before locking. After 2 failed disambiguations → hard-stop to legal aid.
3. **Adjacent-domain guard**: Each domain defines a list of "adjacent but out-of-scope" domains (e.g., consumer → product liability tort; tenancy → property title). If extracted facts match an adjacent domain's keywords, the Scope Gate blocks and routes, rather than attempting to stretch the in-scope domain's knowledge base.

---

## 5. Document Handling

### 5.1 Supported document types

| Document Type | Expected Domains | Extraction Needs |
|---------------|-----------------|------------------|
| Rent agreement / lease deed | Tenancy | Party names, tenure, rent amount, notice clause, jurisdiction clause |
| Employment offer / appointment letter | Employment | Designation, salary, notice period, termination clause |
| Termination / relieving letter | Employment | Date, reason, notice compliance |
| Insurance policy document | Insurance | Policy number, coverage, exclusions, claim procedure, sum insured |
| Insurance claim rejection letter | Insurance | Rejection reason, policy clause cited, timeline |
| FIR copy / complaint receipt | Police/FIR | FIR number, sections cited, date, station |
| Consumer complaint / company response | Consumer | Complaint summary, company position, timeline |
| Cheque / bank memo (return memo) | Cheque bounce | Cheque amount, date, return reason, parties |
| Legal notice (any domain) | Cross-domain | Sender, recipient, subject matter, deadline stated, legal basis cited |
| Government order / notification | Cross-domain | Scheme name, applicability, effective date |

### 5.2 Format support

- **PDF** (text-based and scanned/image-based via OCR)
- **Images** (JPEG, PNG — photographed documents, common for phone-camera uploads)
- **No Word/Excel for v1** — callers rarely have these

> [!NOTE]
> **Assumption A6**: We use a multimodal model (e.g., Gemini) for document understanding rather than building format-specific extraction pipelines. The model extracts into the same `TriageFact.key_facts` schema. A post-extraction validation step checks that mandatory fields for the document type were actually extracted; if not, it flags "I couldn't fully read this document" rather than hallucinating fields.

### 5.3 Multi-document and contradiction handling

1. **Max 3 documents per session** for v1 (complexity bound).
2. If two documents conflict (e.g., lease says 3-month notice, termination notice gives 15 days), system:
   - Flags the specific contradiction to the caller with exact quotes from each document.
   - Does **not** resolve the contradiction (that's legal judgment).
   - Routes to a lawyer with the contradiction summary pre-attached.
3. Contradiction detection is structural (compare extracted field values), not semantic (don't ask the model "who is right").

---

## 6. Escalation / Routing Requirements

### 6.1 Routing table (v1)

| Domain | Escalation Target | Routing Info Needed |
|--------|-------------------|---------------------|
| **Consumer** | District Consumer Forum (DCDRC) / State Commission / National Commission | Claim amount → forum selection; caller's district → nearest forum address + filing portal link |
| **Consumer** (e-commerce) | National Consumer Helpline (1800-11-4000) | Direct handoff |
| **Tenancy** | Rent Controller / Rent Court (state-specific) | Caller's city → correct authority; Legal Aid (DLSA) for low-income |
| **Employment** | Labour Commissioner / Labour Court | Caller's district → jurisdictional office; EPFO regional office for PF matters |
| **Insurance** | Insurance Ombudsman (jurisdiction-based) | Insurer name + caller city → correct ombudsman office; IRDAI Grievance portal (IGMS) |
| **Insurance** (motor) | MACT (Motor Accident Claims Tribunal) | Accident location → jurisdictional MACT |
| **Police / FIR** | SP / DGP office; State Human Rights Commission; NCW (if gender-based) | Caller's district → correct SP office; relevant helpline numbers |
| **Cheque bounce** | Magistrate Court (§138 complaint) | Location of bank branch → jurisdictional court; legal aid if caller can't afford lawyer |
| **Universal fallback** | Nearest DLSA (District Legal Services Authority) | Caller's district → DLSA address + phone |
| **Crisis** | iCall (9152987821), Vandrevala Foundation (1860-2662-345), Police (100/112) | Immediate, no further triage |

### 6.2 Geography-aware routing

> [!NOTE]
> **Assumption A7**: v1 supports geography-aware routing for **top-3 states** (MH, UP, KA) with a static lookup table (district → authority address/phone/portal). For other states, we fall back to the national-level authority or DLSA directory. The lookup table is a **manually curated dataset**, not a live API, for v1.

### 6.3 Routing output format

When routing, the system provides the caller:
1. **Why** it's routing ("This involves [X], which requires a [specialist/authority]")
2. **Where** specifically (name, address, phone, portal URL if applicable)
3. **What to bring / prepare** (documents needed, filing fees if any, timeline to act)
4. Optionally: SMS/WhatsApp message with the above details (so the caller has it in writing)

---

## 7. Non-functional / Trust Requirements

### 7.1 Latency targets

| Interaction Type | Target (p95) | Notes |
|------------------|-------------|-------|
| Conversational turn (fast model) | **< 2 seconds** | Includes ASR + fast model inference + TTS |
| Retrieval-augmented answer | **< 5 seconds** | Includes vector search + deep model reasoning + TTS |
| Document processing | **< 15 seconds** | OCR + extraction + validation; caller hears "I'm reading your document, one moment" |
| Scope Gate evaluation | **< 200 ms** | Deterministic code, no model call |

### 7.2 Logging and audit

| Data Type | Retention | Access Control | Privacy Treatment |
|-----------|-----------|----------------|-------------------|
| Call transcript (ASR output) | 90 days | Engineering + Legal Ops only | PII (names, numbers, addresses) redacted in logs; raw kept in encrypted, access-logged store |
| Uploaded documents | 30 days post-call | Caller + Engineering + Legal Ops | Encrypted at rest; auto-deleted after retention period; caller can request immediate deletion |
| Scope Gate decision log | 1 year | Engineering + Audit | No PII — only structured `TriageFact` + gate decision + routing target |
| Model prompts / completions | 90 days | Engineering only | PII-redacted |
| Routing outcome (did caller reach authority?) | 1 year | Product + Ops | Anonymized |

> [!WARNING]
> **Assumption A8**: We do **not** record raw audio for v1 — only ASR transcripts. Raw audio recording has significant consent and storage implications. If raw audio is needed for quality/eval purposes, we need explicit caller consent flow + separate storage policy. **Flag if you want raw audio.**

### 7.3 Consent flow

At call start:
1. System identifies itself as an AI assistant (legal requirement in many jurisdictions).
2. States: "This is not legal advice. I can help you understand your options and point you to the right authority."
3. If document upload occurs: "I'll process your document to help answer your question. It will be stored securely and deleted within 30 days."

### 7.4 Eval set requirements

| Dimension | Target |
|-----------|--------|
| **Total eval cases** | **≥ 600** for v1 Scope Gate validation |
| **Per domain** | ≥ 80 cases per in-scope domain (6 domains × 80 = 480 baseline) |
| **Cross-domain / ambiguous** | ≥ 60 cases that straddle domain boundaries or are deliberately ambiguous |
| **Hard-stop triggers** | ≥ 60 cases that should trigger universal or domain-specific hard-stops |
| **Document-based** | ≥ 30% of cases include a document artifact |
| **Persona coverage** | Each of the 5 archetypes represented in ≥ 15% of cases |
| **Language split** | 50% Hindi-primary, 30% English-primary, 20% code-switched |

> [!IMPORTANT]
> **Assumption A9**: Eval cases are **manually curated by a legal domain expert**, not synthetically generated. Synthetic augmentation is fine for volume, but the base set of ≥ 600 must be human-written and human-validated for correctness of the expected Scope Gate decision. Budget for this.

---

## 8. Explicit Non-goals for v1

| # | Non-goal | Rationale |
|---|----------|-----------|
| 1 | **No criminal defense or bail assistance** | High stakes, requires real lawyer, active-proceedings hard-stop covers this |
| 2 | **No litigation strategy** | System tells you *what forum to go to*, not *how to argue your case* |
| 3 | **No verdict issuance — ever** | Architectural invariant, not a v1 constraint. System never says "you will win" or "they are liable." |
| 4 | **No multi-state coverage beyond top-3** | Additive expansion later; central law covers the rest |
| 5 | **No payments, referral fees, or lawyer marketplace** | Trust / regulatory complexity; not in v1 |
| 6 | **No real-time court status / case tracking** | Requires integration with eCourts API; roadmap |
| 7 | **No legal document drafting** | System can explain what a §138 notice should contain, but does not generate the notice text. Drafting = practicing law. |
| 8 | **No medical-legal opinion** | Medical negligence, disability assessment, forensic matters — always route out |
| 9 | **No family law beyond basic information** | Divorce, custody, maintenance — too fact-intensive and emotionally charged for AI triage v1 |
| 10 | **No land revenue / mutation / title search** | State-specific, record-dependent, fraud-prone |

> [!NOTE]
> Non-goal #7 (no document drafting) is a **deliberate safety boundary**. Generating legal notices or complaints, even from templates, crosses into "practicing law" territory in most jurisdictions. We provide *guidance on what the document should contain* and route to a lawyer or legal aid for actual drafting. **Flag if you disagree — this is debatable.**

---

## 9. Assumptions Summary

All assumptions made in this document, collected for review:

| ID | Assumption | Section | Risk if Wrong |
|----|-----------|---------|---------------|
| A1 | India is the single jurisdiction for v1 | §1.1 | Architecture would need multi-country statute handling |
| A2 | State-level variation limited to top-3 states (MH, UP, KA) for tenancy and employment only | §1.1 | Under-serves callers from other states on state-specific law |
| A3 | Hindi + English only for v1; code-switching supported | §2.2 | Excludes ~40% of India's population who are more comfortable in other languages |
| A4 | Unified vector store with domain+jurisdiction tag filtering | §3.2 | If cross-domain contamination occurs despite filters, may need physical separation |
| A5 | Amendment tracking is manual/monthly for v1 | §3.3 | Risk of serving stale law for up to 30 days after an amendment |
| A6 | Multimodal model for document extraction, no format-specific pipelines | §5.2 | If extraction quality is poor on scanned/low-quality docs, may need dedicated OCR pipeline |
| A7 | Geography-aware routing for top-3 states only; static lookup table | §6.2 | Callers from other states get less specific routing |
| A8 | No raw audio recording; ASR transcripts only | §7.2 | Limits ability to debug ASR errors or do speech quality evaluation |
| A9 | Eval set is human-curated (≥ 600 cases) | §7.4 | Requires budget + legal expert time; could delay launch if under-resourced |

---

## 10. Open Questions for Owner

> [!IMPORTANT]
> These require your input before I can finalize requirements and proceed to architecture.

1. **State selection**: I proposed MH, UP, KA as top-3 states. Do you have call volume data or user research suggesting different states? (e.g., Tamil Nadu, Gujarat, Rajasthan could be higher volume.)

2. **Language priority**: Is Hindi + English correct for v1, or do you need one regional language (e.g., Marathi for MH) from day 1?

3. **Document drafting boundary (Non-goal #7)**: Do you agree that the system should *never* generate legal document text (notices, complaints, applications), or do you want templated drafting for low-risk documents (e.g., RTI applications, which are simple and low-stakes)?

4. **Raw audio**: Do you want raw call audio recorded (with consent) for ASR quality evaluation, or is transcript-only acceptable for v1?

5. **Eval budget**: The 600-case human-curated eval set requires significant domain expert time. Is this resourced, or should I propose a smaller bootstrapping strategy (e.g., 200 hand-curated + synthetic augmentation to 600)?

6. **Existing Munshi assets**: Is there an existing codebase, knowledge base, or eval set from the Munshi prototype that I should build on, or is this a clean-room build?

7. **Voice platform**: Do you have a preferred voice/telephony provider (e.g., Twilio, Exotel, Vonage) or ASR/TTS stack (e.g., Google STT/TTS, Deepgram, Bhashini for Indic languages), or should I include platform selection in the architecture phase?
