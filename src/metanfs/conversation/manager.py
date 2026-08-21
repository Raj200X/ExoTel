"""
Conversation Manager — the orchestrator for meta-NFS call sessions.

This module coordinates the per-call session lifecycle:
1. Receives utterances from the Voice Front-End
2. Routes them through Fast Model → Scope Gate → (Knowledge Base → Deep Model) → Response
3. Manages session state, domain locking, turn limits, and document merging
4. Enforces conversation-level invariants

For v1, this is a text-based pipeline (voice integration is Phase 6).
The interface is designed so that the Voice Front-End can be plugged in
without changing the Conversation Manager.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

from metanfs.models.core import (
    Citation,
    DeepModelOutput,
    ExtractedDocument,
    FastModelOutput,
    RetrievalQuery,
    RetrievedChunk,
    RoutingRequest,
    RoutingResult,
    ScopeGateDecision,
    Session,
    Turn,
    TriageFact,
    Utterance,
)
from metanfs.models.enums import (
    Domain,
    Jurisdiction,
    RoutingTargetCategory,
    ScopeGateAction,
    SessionState,
)
from metanfs.scope_gate.engine import ScopeGateEngine
from metanfs.routing.engine import RoutingEngine

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Model interfaces (Protocol classes for dependency injection)
# ---------------------------------------------------------------------------


class FastModelInterface(Protocol):
    """Interface for the Fast Model (fact extraction + conversational response)."""

    async def process(
        self,
        utterance_text: str,
        turn_history: list[Turn],
        current_triage_fact: TriageFact,
        domain_lock: Domain | None,
    ) -> FastModelOutput: ...


class DeepModelInterface(Protocol):
    """Interface for the Deep Model (retrieval-augmented reasoning)."""

    async def reason(
        self,
        retrieval_query: str,
        retrieved_chunks: list[RetrievedChunk],
        turn_history: list[Turn],
        triage_fact: TriageFact,
    ) -> DeepModelOutput: ...


class KnowledgeBaseInterface(Protocol):
    """Interface for the Knowledge Base (retrieval)."""

    async def search(self, query: RetrievalQuery) -> list[RetrievedChunk]: ...


# ---------------------------------------------------------------------------
# Conversation Manager
# ---------------------------------------------------------------------------


class ConversationManager:
    """Orchestrates the per-call session lifecycle.

    This is the central coordinator. It does not contain business logic
    (that's in the Scope Gate, models, and routing engine). It only
    orchestrates the flow between components.
    """

    def __init__(
        self,
        scope_gate: ScopeGateEngine,
        routing_engine: RoutingEngine,
        fast_model: FastModelInterface,
        deep_model: DeepModelInterface | None = None,
        knowledge_base: KnowledgeBaseInterface | None = None,
    ):
        self._scope_gate = scope_gate
        self._routing_engine = routing_engine
        self._fast_model = fast_model
        self._deep_model = deep_model
        self._knowledge_base = knowledge_base

        # Active sessions (in-memory for v1; Redis in production)
        self._sessions: dict[str, Session] = {}

    def create_session(self, session_id: str | None = None) -> Session:
        """Create a new call session."""
        session = Session()
        if session_id:
            session.session_id = session_id
        self._sessions[session.session_id] = session
        logger.info("Created session %s", session.session_id)
        return session

    def get_session(self, session_id: str) -> Session | None:
        """Retrieve an existing session."""
        return self._sessions.get(session_id)

    async def process_utterance(self, utterance: Utterance) -> TurnResult:
        """Process a single caller utterance through the full pipeline.

        This is the main entry point for each conversational turn.

        Returns a TurnResult with the response text and metadata.
        """
        session = self._sessions.get(utterance.session_id)
        if session is None:
            session = self.create_session(utterance.session_id)

        # --- Check session limits ---
        if not session.can_add_turn():
            return TurnResult(
                response_text=(
                    "We've covered a lot of ground. Let me summarize what we discussed "
                    "and point you to the right next step."
                ),
                is_final=True,
                scope_gate_decision=ScopeGateDecision(
                    action=ScopeGateAction.HARD_STOP,
                    reason="Turn limit reached",
                    rule_id="ENGINE_TURN_LIMIT",
                ),
            )

        if session.state == SessionState.ENDED:
            return TurnResult(
                response_text="This session has ended. Please call again if you need further help.",
                is_final=True,
                scope_gate_decision=ScopeGateDecision(
                    action=ScopeGateAction.HARD_STOP,
                    reason="Session already ended",
                    rule_id="ENGINE_SESSION_ENDED",
                ),
            )

        # --- Step 1: Fast Model — fact extraction + response candidate ---
        try:
            fast_output = await self._fast_model.process(
                utterance_text=utterance.text,
                turn_history=session.turn_history[-20:],  # Last 20 turns
                current_triage_fact=session.triage_fact,
                domain_lock=session.domain_lock,
            )
        except Exception as e:
            logger.error("Fast Model failed: %s", e)
            return TurnResult(
                response_text=(
                    "I'm having some trouble processing that. "
                    "Let me connect you with someone who can help."
                ),
                is_final=True,
                scope_gate_decision=ScopeGateDecision(
                    action=ScopeGateAction.HARD_STOP,
                    reason=f"Fast Model failure: {e}",
                    rule_id="ENGINE_MODEL_FAILURE",
                ),
            )

        # Update session's triage fact
        session.triage_fact = fast_output.updated_triage_fact

        # --- Step 2: Domain locking ---
        if (
            not session.is_domain_locked()
            and session.triage_fact.detected_domain != Domain.UNKNOWN
            and session.triage_fact.domain_confidence >= 0.7
        ):
            session.domain_lock = session.triage_fact.detected_domain
            logger.info(
                "Session %s: domain locked to %s (confidence: %.2f)",
                session.session_id,
                session.domain_lock,
                session.triage_fact.domain_confidence,
            )

        # --- Step 3: Scope Gate evaluation ---
        decision = self._scope_gate.evaluate(session.triage_fact)
        session.scope_gate_decisions.append(decision)

        # --- Step 4: Act on Scope Gate decision ---
        response_text: str
        citations: list[Citation] = []
        deep_model_used = False

        if decision.action == ScopeGateAction.HARD_STOP:
            response_text = self._build_routing_response(decision, session)
            session.state = SessionState.ROUTING

        elif decision.action == ScopeGateAction.SOFT_STOP:
            # Deliver guidance + routing recommendation
            response_text = fast_output.response_candidate
            routing_info = self._build_routing_response(decision, session)
            response_text += f"\n\n{routing_info}"

        elif decision.action == ScopeGateAction.CLARIFY:
            session.triage_fact.clarification_attempts += 1
            session.state = SessionState.CLARIFYING
            # Use the Scope Gate's clarification question if available,
            # otherwise use the Fast Model's response
            if decision.clarification_question:
                response_text = decision.clarification_question
            else:
                response_text = fast_output.response_candidate

        elif decision.action == ScopeGateAction.PROCEED:
            if fast_output.needs_retrieval and self._knowledge_base and self._deep_model:
                # --- Retrieval path ---
                session.state = SessionState.RETRIEVING
                try:
                    response_text, citations, deep_model_used = (
                        await self._do_retrieval_and_reasoning(
                            fast_output, session
                        )
                    )
                except Exception as e:
                    logger.error("Retrieval/reasoning failed: %s", e)
                    response_text = fast_output.response_candidate
                    response_text += (
                        "\n\nI'd recommend verifying this with the relevant authority."
                    )
            else:
                response_text = fast_output.response_candidate
            session.state = SessionState.ACTIVE
        else:
            # Shouldn't happen, but fail-safe
            response_text = fast_output.response_candidate

        # --- Step 5: Record the turn ---
        turn = Turn(
            turn_number=len(session.turn_history) + 1,
            utterance=utterance,
            triage_fact_snapshot=session.triage_fact.model_copy(),
            scope_gate_decision=decision,
            response_text=response_text,
            deep_model_used=deep_model_used,
            citations=citations,
        )
        session.turn_history.append(turn)

        is_final = decision.action == ScopeGateAction.HARD_STOP
        if is_final:
            session.state = SessionState.ENDED

        return TurnResult(
            response_text=response_text,
            is_final=is_final,
            scope_gate_decision=decision,
            citations=citations,
            session=session,
        )

    async def _do_retrieval_and_reasoning(
        self, fast_output: FastModelOutput, session: Session
    ) -> tuple[str, list[Citation], bool]:
        """Run the retrieval → deep model → answer-check pipeline."""
        assert self._knowledge_base is not None
        assert self._deep_model is not None
        assert fast_output.retrieval_query is not None

        # Determine jurisdiction filter
        jurisdiction = Jurisdiction.CENTRAL
        if session.triage_fact.state:
            try:
                jurisdiction = Jurisdiction(session.triage_fact.state.lower())
            except ValueError:
                jurisdiction = Jurisdiction.CENTRAL

        # Retrieve
        query = RetrievalQuery(
            query_text=fast_output.retrieval_query,
            domain_filter=session.triage_fact.detected_domain,
            jurisdiction_filter=jurisdiction,
        )
        chunks = await self._knowledge_base.search(query)

        if not chunks:
            return (
                "I couldn't find specific legal provisions for your exact situation. "
                "Let me connect you with someone who can help.",
                [],
                False,
            )

        # Deep Model reasoning
        deep_output = await self._deep_model.reason(
            retrieval_query=fast_output.retrieval_query,
            retrieved_chunks=chunks,
            turn_history=session.turn_history[-10:],
            triage_fact=session.triage_fact,
        )

        # Answer-check: run deep model's answer_triage_fact through Scope Gate
        if deep_output.answer_triage_fact:
            answer_decision = self._scope_gate.evaluate(deep_output.answer_triage_fact)
            if answer_decision.action in {ScopeGateAction.HARD_STOP, ScopeGateAction.SOFT_STOP}:
                logger.warning(
                    "Deep Model answer triggered Scope Gate %s (rule: %s). Suppressing answer.",
                    answer_decision.action,
                    answer_decision.rule_id,
                )
                routing_response = self._build_routing_response(answer_decision, session)
                return (
                    "After looking into this more carefully, I think you should speak with "
                    f"a specialist. {routing_response}",
                    [],
                    True,
                )

        # Confidence check on deep model output
        if deep_output.confidence < 0.4:
            return (
                f"{deep_output.grounded_answer}\n\n"
                "I'm not fully confident in this guidance. I'd recommend verifying "
                "with the relevant authority or a lawyer.",
                deep_output.citations,
                True,
            )

        return deep_output.grounded_answer, deep_output.citations, True

    def _build_routing_response(
        self, decision: ScopeGateDecision, session: Session
    ) -> str:
        """Build a caller-facing routing response with resolved authority details."""
        if not decision.routing_target:
            return "I'd recommend speaking with a lawyer or your nearest legal aid center."

        # Resolve routing target using the Routing Engine
        request = RoutingRequest(
            target_category=decision.routing_target.category,
            domain=session.triage_fact.detected_domain,
            caller_state=session.triage_fact.state,
            caller_district=session.triage_fact.district,
        )
        resolved = self._routing_engine.resolve(request)

        # Build caller-facing message
        parts = [f"I'd recommend contacting: **{resolved.authority_name}**"]
        if resolved.phone:
            parts.append(f"📞 Phone: {resolved.phone}")
        if resolved.address:
            parts.append(f"📍 Address: {resolved.address}")
        if resolved.portal_url:
            parts.append(f"🌐 Online: {resolved.portal_url}")
        if resolved.what_to_bring:
            items = ", ".join(resolved.what_to_bring[:5])  # Cap at 5 for voice readability
            parts.append(f"📋 Documents to bring: {items}")
        if resolved.filing_fee:
            parts.append(f"💰 Filing fee: {resolved.filing_fee}")
        if resolved.deadline_note:
            parts.append(f"⏰ Important: {resolved.deadline_note}")

        return "\n".join(parts)

    def merge_document(
        self, session_id: str, extracted_doc: ExtractedDocument
    ) -> str | None:
        """Merge an extracted document into the session's TriageFact.

        Returns an error message if the document can't be accepted, None on success.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return "Session not found."

        if not session.can_add_document():
            return "Maximum number of documents (3) already uploaded."

        # Merge extracted fields into triage_fact.key_facts
        for key, value in extracted_doc.extracted_fields.items():
            # Check for contradictions with existing spoken facts
            if key in session.triage_fact.key_facts:
                existing = session.triage_fact.key_facts[key]
                if str(existing).lower() != str(value).lower():
                    contradiction = (
                        f"Field '{key}': spoken='{existing}', document='{value}'"
                    )
                    session.triage_fact.document_contradiction_details.append(contradiction)
                    extracted_doc.contradictions.append(contradiction)

            session.triage_fact.key_facts[key] = value

        session.triage_fact.document_present = True
        if extracted_doc.contradictions:
            session.triage_fact.document_contradicts_spoken = True

        session.documents.append(extracted_doc)
        logger.info(
            "Session %s: merged document %s (%d fields, %d contradictions)",
            session_id,
            extracted_doc.doc_type,
            len(extracted_doc.extracted_fields),
            len(extracted_doc.contradictions),
        )
        return None


# ---------------------------------------------------------------------------
# Turn result (returned to the caller / Voice Front-End)
# ---------------------------------------------------------------------------


class TurnResult:
    """The result of processing a single conversational turn."""

    def __init__(
        self,
        response_text: str,
        is_final: bool = False,
        scope_gate_decision: ScopeGateDecision | None = None,
        citations: list[Citation] | None = None,
        session: Session | None = None,
    ):
        self.response_text = response_text
        self.is_final = is_final
        self.scope_gate_decision = scope_gate_decision
        self.citations = citations or []
        self.session = session
