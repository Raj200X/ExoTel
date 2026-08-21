"""
Core domain enumerations for meta-NFS.

These enums define the bounded vocabulary for domains, jurisdictions,
routing targets, and other categorical values used across all components.
They are the single source of truth — adding a new domain starts here.
"""

from enum import StrEnum


class Domain(StrEnum):
    """Legal domains supported by meta-NFS.

    Adding a new domain requires:
    1. Add enum value here
    2. Add scope gate rules in config/scope_gate_rules/<domain>.yaml
    3. Add routing table entries in config/routing_tables/
    4. Ingest knowledge base documents into data/knowledge_base/<domain>/
    5. Add ≥80 eval cases in eval/<domain>/
    """

    CONSUMER = "consumer"
    TENANCY = "tenancy"
    EMPLOYMENT = "employment"
    INSURANCE = "insurance"
    POLICE_FIR = "police_fir"
    CHEQUE_BOUNCE = "cheque_bounce"
    UNKNOWN = "unknown"


class Jurisdiction(StrEnum):
    """Jurisdictions supported for state-level law variation."""

    CENTRAL = "central"
    MAHARASHTRA = "maharashtra"
    UTTAR_PRADESH = "uttar_pradesh"
    KARNATAKA = "karnataka"


class CallerIntent(StrEnum):
    """Detected intent of the caller's query."""

    UNDERSTAND_RIGHTS = "understand_rights"
    FILE_COMPLAINT = "file_complaint"
    DISPUTE_RESOLUTION = "dispute_resolution"
    CHECK_ELIGIBILITY = "check_eligibility"
    PROCESS_GUIDANCE = "process_guidance"
    OTHER = "other"


class ScopeGateAction(StrEnum):
    """Actions the Scope Gate can take. Ordered by severity."""

    PROCEED = "proceed"
    CLARIFY = "clarify"
    SOFT_STOP = "soft_stop"
    HARD_STOP = "hard_stop"


class RoutingTargetCategory(StrEnum):
    """Categories of routing targets for escalation."""

    # Domain-specific
    CONSUMER_FORUM_DISTRICT = "consumer_forum_district"
    CONSUMER_FORUM_STATE = "consumer_forum_state"
    CONSUMER_FORUM_NATIONAL = "consumer_forum_national"
    NATIONAL_CONSUMER_HELPLINE = "national_consumer_helpline"
    RENT_CONTROLLER = "rent_controller"
    LABOUR_COMMISSIONER = "labour_commissioner"
    LABOUR_COURT = "labour_court"
    EPFO_REGIONAL = "epfo_regional"
    INSURANCE_OMBUDSMAN = "insurance_ombudsman"
    IRDAI_GRIEVANCE = "irdai_grievance"
    MACT = "mact"
    MAGISTRATE_COURT_138 = "magistrate_court_138"
    POLICE_SP = "police_sp"
    POLICE_HELPLINE = "police_helpline"

    # Cross-domain
    CRIMINAL_LAWYER = "criminal_lawyer"
    FAMILY_COURT = "family_court"
    LEGAL_AID_DLSA = "legal_aid_dlsa"
    NALSA_HELPLINE = "nalsa_helpline"
    NCW = "ncw"
    SHE_BOX = "she_box"

    # Crisis
    CRISIS_ICALL = "crisis_icall"
    CRISIS_VANDREVALA = "crisis_vandrevala"
    EMERGENCY_POLICE = "emergency_police"

    # Fallback
    GENERIC_LAWYER = "generic_lawyer"


class DocumentType(StrEnum):
    """Types of documents the system can process."""

    RENT_AGREEMENT = "rent_agreement"
    EMPLOYMENT_OFFER = "employment_offer"
    TERMINATION_LETTER = "termination_letter"
    INSURANCE_POLICY = "insurance_policy"
    INSURANCE_REJECTION = "insurance_rejection"
    FIR_COPY = "fir_copy"
    CONSUMER_COMPLAINT = "consumer_complaint"
    CHEQUE_RETURN_MEMO = "cheque_return_memo"
    LEGAL_NOTICE = "legal_notice"
    GOVERNMENT_ORDER = "government_order"
    UNKNOWN_DOCUMENT = "unknown_document"


class SessionState(StrEnum):
    """States of a caller session."""

    ACTIVE = "active"
    CLARIFYING = "clarifying"
    RETRIEVING = "retrieving"
    ROUTING = "routing"
    ENDED = "ended"


class Language(StrEnum):
    """Supported languages for v1."""

    HINDI = "hi"
    ENGLISH = "en"
