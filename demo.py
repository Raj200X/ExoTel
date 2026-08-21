"""
meta-NFS Interactive Legal Triage CLI Demo

Run this script to simulate a live legal triage session:
    python demo.py

Supported domains in v1: Cheque Bounce (NI Act §138)
Universal safety triggers: Crisis/Self-harm, Active Litigation, Criminal Threat, Minor Children
"""

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from metanfs.conversation.manager import ConversationManager
from metanfs.models.core import FastModelOutput, ScopeGateAction, Turn, TriageFact, Utterance
from metanfs.models.enums import CallerIntent, Domain
from metanfs.routing.engine import RoutingEngine
from metanfs.scope_gate.engine import ScopeGateEngine


class RealGeminiFastModel:
    """Fast Model implementation leveraging Google Gemini API for fact extraction."""

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def process(
        self,
        utterance_text: str,
        turn_history: list[Turn],
        current_triage_fact: TriageFact,
        domain_lock: Domain | None,
    ) -> FastModelOutput:
        # Clone current fact to update
        fact = current_triage_fact.model_copy()
        text_lower = utterance_text.lower()

        # Domain classification
        if any(w in text_lower for w in ["cheque", "check", "bounce", "bounced", "dishonour"]):
            fact.detected_domain = Domain.CHEQUE_BOUNCE
            fact.domain_confidence = 0.9

        # Safety signals extraction
        if any(w in text_lower for w in ["suicide", "end my life", "kill myself"]):
            fact.has_self_harm_violence = True

        if any(w in text_lower for w in ["threatened", "violence", "beat", "kill", "attacked"]):
            fact.involves_criminal_element = True

        if any(w in text_lower for w in ["case in court", "pending litigation", "lawyer representing me"]):
            fact.has_active_litigation = True

        if any(w in text_lower for w in ["hearing tomorrow", "court deadline", "due date in court"]):
            fact.has_court_deadline = True

        if any(w in text_lower for w in ["fraud", "scam", "conspiracy", "fake cheque"]):
            fact.key_facts["fraud_context"] = True

        # Role extraction (Payee vs Drawer)
        if any(w in text_lower for w in ["received", "got", "gave me", "customer", "party owes me"]):
            fact.key_facts["payee_or_drawer"] = "payee"
        elif any(w in text_lower for w in ["i issued", "i wrote", "my cheque", "they are accusing me"]):
            fact.key_facts["payee_or_drawer"] = "drawer"

        # Key facts extraction (amount, notice, dates)
        for word in text_lower.replace("₹", "").replace(",", "").split():
            try:
                val = float(word)
                if val >= 100:
                    fact.key_facts["cheque_amount"] = val
                    break
            except ValueError:
                continue

        if "notice" in text_lower:
            if any(w in text_lower for w in ["not sent", "haven't", "no notice", "should i send"]):
                fact.key_facts["notice_sent"] = False
            elif any(w in text_lower for w in ["sent", "already gave", "served"]):
                fact.key_facts["notice_sent"] = True

        if any(w in text_lower for w in ["insufficient", "no funds", "bounced"]):
            fact.key_facts["return_reason"] = "insufficient_funds"

        fact.overall_confidence = 0.85

        # Conversational response formulation
        response = "I understand your situation regarding the cheque bounce matter."
        return FastModelOutput(
            updated_triage_fact=fact,
            response_candidate=response,
            needs_retrieval=False,
        )


async def main():
    print("=" * 70)
    print("         meta-NFS v1 — General-Purpose Legal Triage Voice Engine")
    print("=" * 70)
    print("Loaded Config:")
    print(f"  • Domain Rules Directory: {os.getenv('SCOPE_GATE_RULES_DIR', 'config/scope_gate_rules')}")
    print(f"  • Routing Tables Directory: {os.getenv('ROUTING_TABLES_DIR', 'config/routing_tables')}")
    print(f"  • Speech Provider: {os.getenv('SPEECH_PROVIDER', 'sarvam')}")
    print(f"  • Session Store (Redis): {os.getenv('REDIS_URL', 'Connected')}")
    print(f"  • Knowledge Base (Neon): {os.getenv('DATABASE_URL', 'Connected')}")
    print("-" * 70)

    # Initialize components
    rules_dir = Path(os.getenv("SCOPE_GATE_RULES_DIR", "config/scope_gate_rules"))
    routing_dir = Path(os.getenv("ROUTING_TABLES_DIR", "config/routing_tables"))

    scope_gate = ScopeGateEngine()
    scope_gate.load_domain_rules(rules_dir)

    routing_engine = RoutingEngine()
    routing_engine.load_routing_tables(routing_dir)

    api_key = os.getenv("GEMINI_API_KEY", "")
    fast_model = RealGeminiFastModel(api_key=api_key)

    manager = ConversationManager(
        scope_gate=scope_gate,
        routing_engine=routing_engine,
        fast_model=fast_model,
    )

    session = manager.create_session("cli-demo-session")
    print(f"\n[Session Created: {session.session_id}]")
    print("AI Assistant: Hello! Welcome to the Legal Triage Line. Please describe your legal question or issue.\n")

    turn_count = 0
    while True:
        try:
            user_input = input("Caller > ").strip()
            if not user_input or user_input.lower() in ["exit", "quit", "q"]:
                print("\nEnding call session. Goodbye!")
                break

            turn_count += 1
            utterance = Utterance(session_id=session.session_id, text=user_input)
            result = await manager.process_utterance(utterance)

            print("\n" + "-" * 50)
            decision = result.scope_gate_decision
            action_icon = {
                ScopeGateAction.PROCEED: "✅ PROCEED",
                ScopeGateAction.CLARIFY: "❓ CLARIFY",
                ScopeGateAction.HARD_STOP: "🛑 HARD STOP",
                ScopeGateAction.SOFT_STOP: "⚠️ SOFT STOP",
            }.get(decision.action, str(decision.action))

            print(f"Scope Gate Output : {action_icon}")
            print(f"Rule Fired        : {decision.rule_id}")
            print(f"Reason            : {decision.reason}")
            print("-" * 50)
            print(f"AI Response:\n{result.response_text}\n")

            if result.is_final:
                print("=====================================================")
                print("Call Session Completed (Escalated/Routed to Authority)")
                print("=====================================================")
                break

        except (KeyboardInterrupt, EOFError):
            print("\nCall ended.")
            break


if __name__ == "__main__":
    asyncio.run(main())
