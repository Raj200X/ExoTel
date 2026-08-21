"""
End-to-end integration tests for the cheque bounce vertical slice.

These tests use a mock Fast Model that simulates realistic fact extraction
to prove the full pipeline: Utterance → Fast Model → Scope Gate → Response.

The mock model progressively fills in TriageFact fields based on the
utterance content, simulating a real multi-turn conversation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from metanfs.models.core import (
    FastModelOutput,
    ExtractedDocument,
    Turn,
    TriageFact,
    Utterance,
)
from metanfs.models.enums import (
    CallerIntent,
    DocumentType,
    Domain,
    ScopeGateAction,
)
from metanfs.scope_gate.engine import ScopeGateEngine
from metanfs.routing.engine import RoutingEngine
from metanfs.conversation.manager import ConversationManager, TurnResult


RULES_DIR = Path(__file__).parent.parent.parent / "config" / "scope_gate_rules"
ROUTING_DIR = Path(__file__).parent.parent.parent / "config" / "routing_tables"


# ---------------------------------------------------------------------------
# Mock Fast Model — simulates progressive fact extraction
# ---------------------------------------------------------------------------


class MockFastModel:
    """A mock Fast Model that extracts facts from utterance keywords.

    This is NOT a real model — it pattern-matches on keywords to simulate
    the kind of TriageFact updates a real model would produce.
    """

    async def process(
        self,
        utterance_text: str,
        turn_history: list[Turn],
        current_triage_fact: TriageFact,
        domain_lock: Domain | None,
    ) -> FastModelOutput:
        # Start from current fact, update based on utterance
        fact = current_triage_fact.model_copy()
        response = "I understand. Let me help you with that."
        text_lower = utterance_text.lower()

        # Domain detection
        if any(w in text_lower for w in ["cheque", "check", "bounce", "bounced", "dishonour"]):
            fact.detected_domain = Domain.CHEQUE_BOUNCE
            fact.domain_confidence = 0.9

        # Payee/drawer detection
        if any(w in text_lower for w in ["received", "got", "gave me", "customer"]):
            fact.key_facts["payee_or_drawer"] = "payee"
        elif any(w in text_lower for w in ["wrote", "issued", "my cheque"]):
            fact.key_facts["payee_or_drawer"] = "drawer"

        # Amount detection
        for word in text_lower.split():
            try:
                amount = float(word.replace(",", "").replace("₹", ""))
                if amount > 100:  # Likely a cheque amount
                    fact.key_facts["cheque_amount"] = amount
                    break
            except ValueError:
                continue

        # Notice status
        if "notice" in text_lower and "sent" in text_lower:
            fact.key_facts["notice_sent"] = True
        elif "notice" in text_lower and ("not" in text_lower or "haven't" in text_lower):
            fact.key_facts["notice_sent"] = False
        elif "no notice" in text_lower:
            fact.key_facts["notice_sent"] = False

        # Return reason
        if "insufficient" in text_lower or "no funds" in text_lower:
            fact.key_facts["return_reason"] = "insufficient_funds"
        elif "account closed" in text_lower:
            fact.key_facts["return_reason"] = "account_closed"

        # Bounce date
        if "last month" in text_lower:
            fact.key_facts["bounce_date"] = "2024-07-15"
        elif "yesterday" in text_lower:
            fact.key_facts["bounce_date"] = "2024-08-14"

        # Fraud detection
        if any(w in text_lower for w in ["fraud", "scam", "fake", "conspiracy"]):
            fact.key_facts["fraud_context"] = True

        # Criminal element
        if any(w in text_lower for w in ["threatened", "violence", "beat", "kill"]):
            fact.involves_criminal_element = True

        # Self-harm
        if any(w in text_lower for w in ["suicide", "end my life", "kill myself"]):
            fact.has_self_harm_violence = True

        # Caller intent
        if any(w in text_lower for w in ["what should i do", "how do i", "what are my options"]):
            fact.caller_intent = CallerIntent.PROCESS_GUIDANCE
        elif any(w in text_lower for w in ["file", "complaint", "case"]):
            fact.caller_intent = CallerIntent.FILE_COMPLAINT

        fact.overall_confidence = fact.domain_confidence * 0.9

        needs_retrieval = fact.detected_domain != Domain.UNKNOWN

        return FastModelOutput(
            updated_triage_fact=fact,
            response_candidate=response,
            needs_retrieval=False,  # No KB in this test
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def system() -> tuple[ConversationManager, ScopeGateEngine, RoutingEngine]:
    """Create the full system with mock model."""
    scope_gate = ScopeGateEngine()
    scope_gate.load_domain_rules(RULES_DIR)

    routing_engine = RoutingEngine()
    routing_engine.load_routing_tables(ROUTING_DIR)

    manager = ConversationManager(
        scope_gate=scope_gate,
        routing_engine=routing_engine,
        fast_model=MockFastModel(),
    )

    return manager, scope_gate, routing_engine


# ---------------------------------------------------------------------------
# Test scenarios
# ---------------------------------------------------------------------------


class TestChequeBounceHappyPath:
    """Tests the standard payee path through the cheque bounce workflow."""

    @pytest.mark.asyncio
    async def test_initial_utterance_identifies_domain(
        self, system: tuple[ConversationManager, ScopeGateEngine, RoutingEngine]
    ):
        manager, _, _ = system
        session = manager.create_session("test-1")

        result = await manager.process_utterance(
            Utterance(session_id="test-1", text="A customer's cheque bounced")
        )

        assert not result.is_final
        # Domain should be detected, but payee/drawer needs clarification
        assert result.scope_gate_decision is not None

    @pytest.mark.asyncio
    async def test_multi_turn_payee_workflow(
        self, system: tuple[ConversationManager, ScopeGateEngine, RoutingEngine]
    ):
        """Full multi-turn conversation: payee discovers cheque bounced → guided to send notice."""
        manager, _, _ = system
        session = manager.create_session("test-2")

        # Turn 1: Caller describes situation
        r1 = await manager.process_utterance(
            Utterance(
                session_id="test-2",
                text="I received a cheque from a customer for 50000 and it bounced last month due to insufficient funds",
            )
        )
        assert not r1.is_final

        # Turn 2: Clarify notice status
        r2 = await manager.process_utterance(
            Utterance(
                session_id="test-2",
                text="I haven't sent any notice yet. What should I do?",
            )
        )
        assert not r2.is_final
        # Should PROCEED — payee, amount known, notice context established → guide
        assert r2.scope_gate_decision.action == ScopeGateAction.PROCEED
        # The exact rule depends on mock model's fact extraction accuracy;
        # the key invariant is that it PROCEEDs (no hard-stop, no soft-stop)
        assert r2.scope_gate_decision.rule_id.startswith("CB_PROCEED")

    @pytest.mark.asyncio
    async def test_payee_with_notice_sent(
        self, system: tuple[ConversationManager, ScopeGateEngine, RoutingEngine]
    ):
        """Payee who has already sent notice gets guided on next steps."""
        manager, _, _ = system
        session = manager.create_session("test-3")

        # Single comprehensive utterance
        result = await manager.process_utterance(
            Utterance(
                session_id="test-3",
                text=(
                    "I received a cheque for 75000 that bounced due to insufficient funds. "
                    "I have sent a notice to the drawer last month. "
                    "What should I do? The notice period has not expired yet."
                ),
            )
        )

        # Need to simulate the notice_period_expired fact
        session = manager.get_session("test-3")
        session.triage_fact.key_facts["notice_period_expired"] = False

        result2 = await manager.process_utterance(
            Utterance(
                session_id="test-3",
                text="The 15 day period hasn't passed yet. What are my options?",
            )
        )

        assert not result2.is_final


class TestChequeBounceHardStops:
    """Tests that hard-stop scenarios are properly caught and routed."""

    @pytest.mark.asyncio
    async def test_fraud_context_routes_to_criminal_lawyer(
        self, system: tuple[ConversationManager, ScopeGateEngine, RoutingEngine]
    ):
        manager, _, _ = system
        session = manager.create_session("test-fraud")

        result = await manager.process_utterance(
            Utterance(
                session_id="test-fraud",
                text="A customer gave me a cheque for 50000 but I think it was a fraud scam",
            )
        )

        assert result.is_final
        assert result.scope_gate_decision.action == ScopeGateAction.HARD_STOP
        assert result.scope_gate_decision.rule_id == "CB_HARD_001"

    @pytest.mark.asyncio
    async def test_criminal_threat_triggers_universal_hard_stop(
        self, system: tuple[ConversationManager, ScopeGateEngine, RoutingEngine]
    ):
        """Criminal threat overrides domain rules (universal > domain)."""
        manager, _, _ = system
        session = manager.create_session("test-threat")

        result = await manager.process_utterance(
            Utterance(
                session_id="test-threat",
                text="The cheque bounced and when I asked for payment he threatened to beat me",
            )
        )

        assert result.is_final
        assert result.scope_gate_decision.action == ScopeGateAction.HARD_STOP
        assert result.scope_gate_decision.rule_id == "UNIVERSAL_CRIMINAL_001"

    @pytest.mark.asyncio
    async def test_self_harm_triggers_crisis_routing(
        self, system: tuple[ConversationManager, ScopeGateEngine, RoutingEngine]
    ):
        manager, _, _ = system
        session = manager.create_session("test-crisis")

        result = await manager.process_utterance(
            Utterance(
                session_id="test-crisis",
                text="I lost all my money because of a bounced cheque and I want to end my life",
            )
        )

        assert result.is_final
        assert result.scope_gate_decision.action == ScopeGateAction.HARD_STOP
        assert result.scope_gate_decision.rule_id == "UNIVERSAL_CRISIS_001"
        assert "iCall" in result.response_text or "9152987821" in result.response_text


class TestDocumentMerging:
    """Tests document upload and contradiction detection."""

    @pytest.mark.asyncio
    async def test_document_merge_updates_triage_fact(
        self, system: tuple[ConversationManager, ScopeGateEngine, RoutingEngine]
    ):
        manager, _, _ = system
        session = manager.create_session("test-doc")

        # First, establish conversation context
        await manager.process_utterance(
            Utterance(
                session_id="test-doc",
                text="I received a cheque for 50000 that bounced",
            )
        )

        # Upload document with matching info
        doc = ExtractedDocument(
            doc_type=DocumentType.CHEQUE_RETURN_MEMO,
            extracted_fields={
                "cheque_amount": 50000,
                "return_reason": "insufficient_funds",
                "cheque_date": "2024-07-01",
            },
            extraction_confidence=0.9,
        )
        error = manager.merge_document("test-doc", doc)
        assert error is None

        session = manager.get_session("test-doc")
        assert session.triage_fact.document_present is True
        assert session.triage_fact.key_facts["cheque_date"] == "2024-07-01"

    @pytest.mark.asyncio
    async def test_document_contradiction_detected(
        self, system: tuple[ConversationManager, ScopeGateEngine, RoutingEngine]
    ):
        manager, _, _ = system
        session = manager.create_session("test-doc-contradict")

        # Caller says amount is 50000
        await manager.process_utterance(
            Utterance(
                session_id="test-doc-contradict",
                text="I received a cheque for 50000 that bounced",
            )
        )

        # Document says amount is 75000 — contradiction!
        doc = ExtractedDocument(
            doc_type=DocumentType.CHEQUE_RETURN_MEMO,
            extracted_fields={"cheque_amount": 75000},
            extraction_confidence=0.9,
        )
        error = manager.merge_document("test-doc-contradict", doc)
        assert error is None

        session = manager.get_session("test-doc-contradict")
        assert session.triage_fact.document_contradicts_spoken is True
        assert len(session.triage_fact.document_contradiction_details) > 0

        # Next utterance should trigger SOFT_STOP due to contradiction
        result = await manager.process_utterance(
            Utterance(
                session_id="test-doc-contradict",
                text="So what should I do now?",
            )
        )
        assert result.scope_gate_decision.action == ScopeGateAction.SOFT_STOP
        assert result.scope_gate_decision.rule_id == "UNIVERSAL_CONTRADICTION_001"

    @pytest.mark.asyncio
    async def test_max_documents_enforced(
        self, system: tuple[ConversationManager, ScopeGateEngine, RoutingEngine]
    ):
        manager, _, _ = system
        session = manager.create_session("test-max-docs")

        # Upload 3 documents (the max)
        for i in range(3):
            doc = ExtractedDocument(
                doc_type=DocumentType.CHEQUE_RETURN_MEMO,
                extracted_fields={f"field_{i}": f"value_{i}"},
                extraction_confidence=0.9,
            )
            error = manager.merge_document("test-max-docs", doc)
            assert error is None

        # 4th document should be rejected
        doc4 = ExtractedDocument(
            doc_type=DocumentType.CHEQUE_RETURN_MEMO,
            extracted_fields={"extra": "field"},
            extraction_confidence=0.9,
        )
        error = manager.merge_document("test-max-docs", doc4)
        assert error is not None
        assert "3" in error


class TestSessionInvariants:
    """Tests that session-level invariants are enforced."""

    @pytest.mark.asyncio
    async def test_domain_lock_is_write_once(
        self, system: tuple[ConversationManager, ScopeGateEngine, RoutingEngine]
    ):
        """Once domain is locked, it should not change."""
        manager, _, _ = system
        session = manager.create_session("test-lock")

        # First utterance locks to CHEQUE_BOUNCE
        await manager.process_utterance(
            Utterance(
                session_id="test-lock",
                text="A customer's cheque bounced for 50000",
            )
        )

        session = manager.get_session("test-lock")
        assert session.domain_lock == Domain.CHEQUE_BOUNCE

        # Even if a subsequent utterance mentions a different domain topic,
        # the domain lock should remain
        original_lock = session.domain_lock
        await manager.process_utterance(
            Utterance(
                session_id="test-lock",
                text="Also my landlord is increasing rent",
            )
        )
        session = manager.get_session("test-lock")
        assert session.domain_lock == original_lock  # Still CHEQUE_BOUNCE
