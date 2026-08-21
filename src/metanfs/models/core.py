"""
Core data models for meta-NFS.

These Pydantic models define the contracts between all system components.
They are the interfaces from the architecture doc, made concrete.

Key models:
- TriageFact: The structured fact representation extracted from conversation + documents.
  This is the Scope Gate's input — everything the Gate needs to make a decision.
- ScopeGateDecision: The Gate's output — what to do next.
- Session: The per-call state managed by the Conversation Manager.
- Turn: A single conversational exchange (utterance + response).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from metanfs.models.enums import (
    CallerIntent,
    DocumentType,
    Domain,
    Jurisdiction,
    Language,
    RoutingTargetCategory,
    ScopeGateAction,
    SessionState,
)


# ---------------------------------------------------------------------------
# TriageFact — the core data structure flowing through the system
# ---------------------------------------------------------------------------


class TriageFact(BaseModel):
    """Structured fact representation extracted from conversation and documents.

    This is the single input to the Scope Gate. The Fast Model populates it;
    the Document Processor merges document-extracted fields into it; the Scope
    Gate evaluates it against deterministic rules.

    Design principle: every field the Scope Gate could ever need to make a
    safety decision must be an explicit, typed field here — never buried
    in key_facts as an untyped string.
    """

    # --- Domain classification ---
    detected_domain: Domain = Domain.UNKNOWN
    domain_confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    # --- Jurisdiction ---
    country: str = "IN"
    state: str | None = None
    district: str | None = None

    # --- Universal safety signals (checked before domain-specific rules) ---
    involves_criminal_element: bool = False
    involves_minor_children: bool = False
    has_active_litigation: bool = False
    has_court_deadline: bool = False
    has_self_harm_violence: bool = False

    # --- Document state ---
    document_present: bool = False
    document_contradicts_spoken: bool | None = None
    document_contradiction_details: list[str] = Field(default_factory=list)

    # --- Caller intent ---
    caller_intent: CallerIntent = CallerIntent.OTHER
    overall_confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    # --- Domain-specific fact slots ---
    # Structured as a flat dict of string key-value pairs.
    # Keys are domain-specific (e.g., "cheque_amount", "notice_period_given").
    # The Scope Gate rules reference these keys explicitly.
    key_facts: dict[str, Any] = Field(default_factory=dict)

    # --- What's still missing ---
    missing_critical_facts: list[str] = Field(default_factory=list)

    # --- Clarification tracking ---
    clarification_attempts: int = 0


# ---------------------------------------------------------------------------
# Scope Gate models
# ---------------------------------------------------------------------------


class RoutingTarget(BaseModel):
    """A specific authority/helpline to route the caller to."""

    category: RoutingTargetCategory
    authority_name: str | None = None
    phone: str | None = None
    address: str | None = None
    portal_url: str | None = None
    what_to_bring: list[str] = Field(default_factory=list)
    filing_fee: str | None = None
    deadline_note: str | None = None


class ScopeGateDecision(BaseModel):
    """Output of the Scope Gate — the system's next action.

    Every turn produces exactly one ScopeGateDecision. This is the
    authoritative signal that the Conversation Manager acts on.
    """

    action: ScopeGateAction
    reason: str  # Human-readable explanation (logged, not shown to caller)
    rule_id: str  # Which rule fired (for audit trail)
    routing_target: RoutingTarget | None = None  # Populated for HARD_STOP / SOFT_STOP
    clarification_question: str | None = None  # Populated for CLARIFY
    missing_facts: list[str] | None = None  # What facts are needed (CLARIFY)


# ---------------------------------------------------------------------------
# Conversation / Session models
# ---------------------------------------------------------------------------


class Utterance(BaseModel):
    """A single utterance from the caller, as delivered by the Voice Front-End."""

    session_id: str
    text: str
    language_detected: Language = Language.HINDI
    asr_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class FastModelOutput(BaseModel):
    """Output of the Fast Model for a single turn.

    Two jobs: (1) update TriageFact, (2) generate response candidate.
    """

    updated_triage_fact: TriageFact
    response_candidate: str
    needs_retrieval: bool = False
    retrieval_query: str | None = None


class Citation(BaseModel):
    """A citation to a specific source in the knowledge base."""

    source_document: str  # e.g., "NI Act 1881"
    section_ref: str  # e.g., "§138(1)"
    version_date: str  # e.g., "2024-01-15"
    chunk_text: str | None = None  # The actual text cited


class DeepModelOutput(BaseModel):
    """Output of the Deep Model after retrieval-augmented reasoning."""

    grounded_answer: str
    citations: list[Citation] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    answer_triage_fact: TriageFact | None = None  # Re-evaluation after reasoning


class Turn(BaseModel):
    """A single conversational turn (caller utterance + system response)."""

    turn_number: int
    utterance: Utterance
    triage_fact_snapshot: TriageFact
    scope_gate_decision: ScopeGateDecision
    response_text: str
    deep_model_used: bool = False
    citations: list[Citation] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class ExtractedDocument(BaseModel):
    """A document that has been processed and had fields extracted."""

    doc_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    doc_type: DocumentType = DocumentType.UNKNOWN_DOCUMENT
    extracted_fields: dict[str, Any] = Field(default_factory=dict)
    extraction_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    raw_text: str = ""
    contradictions: list[str] = Field(default_factory=list)
    uploaded_at: datetime = Field(default_factory=datetime.utcnow)


class Session(BaseModel):
    """Per-call session state managed by the Conversation Manager.

    This is the central mutable state object for a call. It accumulates
    facts, documents, and decisions across turns.
    """

    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    caller_phone_hash: str | None = None  # Hashed, never raw
    language: Language = Language.HINDI
    domain_lock: Domain | None = None  # Write-once: set by Scope Gate, never changed
    turn_history: list[Turn] = Field(default_factory=list)
    triage_fact: TriageFact = Field(default_factory=TriageFact)
    documents: list[ExtractedDocument] = Field(default_factory=list)
    scope_gate_decisions: list[ScopeGateDecision] = Field(default_factory=list)
    state: SessionState = SessionState.ACTIVE
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_activity: datetime = Field(default_factory=datetime.utcnow)

    # --- Invariant enforcement ---
    MAX_TURNS: int = 30
    MAX_DOCUMENTS: int = 3
    MAX_CLARIFICATION_LOOPS: int = 3

    def is_domain_locked(self) -> bool:
        """Check if a domain has been locked for this session."""
        return self.domain_lock is not None

    def can_add_turn(self) -> bool:
        """Check if the session can accept more turns."""
        return len(self.turn_history) < self.MAX_TURNS

    def can_add_document(self) -> bool:
        """Check if the session can accept more documents."""
        return len(self.documents) < self.MAX_DOCUMENTS


# ---------------------------------------------------------------------------
# Knowledge Base models
# ---------------------------------------------------------------------------


class RetrievalQuery(BaseModel):
    """A query to the Knowledge Base."""

    query_text: str
    domain_filter: Domain
    jurisdiction_filter: Jurisdiction = Jurisdiction.CENTRAL
    source_type_filter: list[str] | None = None  # e.g., ["statute", "case_law"]


class RetrievedChunk(BaseModel):
    """A chunk returned from the Knowledge Base."""

    chunk_id: str
    text: str
    source_document: str
    section_ref: str
    domain: Domain
    jurisdiction: Jurisdiction
    source_type: str  # "statute", "rule", "guideline", "case_law"
    version_date: str
    relevance_score: float = Field(default=0.0, ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# Routing models
# ---------------------------------------------------------------------------


class RoutingRequest(BaseModel):
    """A request to the Routing Engine for escalation target resolution."""

    target_category: RoutingTargetCategory
    domain: Domain
    caller_state: str | None = None
    caller_district: str | None = None
    context_summary: str = ""


class RoutingResult(BaseModel):
    """Resolved routing target with full contact details."""

    authority_name: str
    authority_type: str
    phone: str | None = None
    address: str | None = None
    portal_url: str | None = None
    what_to_bring: list[str] = Field(default_factory=list)
    filing_fee: str | None = None
    deadline_note: str | None = None


# ---------------------------------------------------------------------------
# Audit models
# ---------------------------------------------------------------------------


class AuditEvent(BaseModel):
    """An immutable audit log entry."""

    session_id: str
    event_type: str  # CALL_START, UTTERANCE, SCOPE_GATE_DECISION, etc.
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    payload: dict[str, Any] = Field(default_factory=dict)
