"""
Scope Gate Rule Engine — the safety-critical core of meta-NFS.

This module implements a deterministic, code-based rule engine that evaluates
TriageFact objects against a set of rules to produce ScopeGateDecision outputs.

Design invariants:
1. ZERO model discretion — all decisions are rule-based.
2. Universal hard-stops always evaluate first and cannot be overridden.
3. Fail-safe default: if no rule matches, the decision is HARD_STOP (not PROCEED).
4. Adding a new domain is additive: new rule file, no engine code changes.
5. Rules are evaluated in priority order (lower number = higher priority).
6. First matching rule wins (short-circuit evaluation).

Rule evaluation order:
1. Universal hard-stops (built into engine code, not overridable by domain rules)
2. Domain classification check
3. Domain-specific rules (loaded from YAML config)
4. Completeness check (missing critical facts → CLARIFY)
5. Confidence check
6. Fail-safe default (HARD_STOP)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from metanfs.models.enums import Domain, RoutingTargetCategory, ScopeGateAction
from metanfs.models.core import (
    RoutingTarget,
    ScopeGateDecision,
    TriageFact,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Rule definitions (loaded from YAML)
# ---------------------------------------------------------------------------


@dataclass
class DomainRule:
    """A single Scope Gate rule for a specific domain.

    Rules are loaded from YAML config files. Each rule has:
    - A unique ID (for audit trail)
    - A condition expression (evaluated against TriageFact)
    - An action (PROCEED, CLARIFY, SOFT_STOP, HARD_STOP)
    - Optional routing target, clarification question
    - Priority (lower = evaluated first)
    """

    id: str
    description: str
    domain: Domain
    condition: str  # Python expression evaluated against triage_fact
    action: ScopeGateAction
    priority: int = 100
    routing_category: RoutingTargetCategory | None = None
    clarification_question: str | None = None
    missing_facts: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any], domain: Domain) -> DomainRule:
        """Create a DomainRule from a parsed YAML dict."""
        routing_cat = data.get("routing")
        if routing_cat:
            routing_cat = RoutingTargetCategory(routing_cat)

        return cls(
            id=data["id"],
            description=data["description"],
            domain=domain,
            condition=data["condition"],
            action=ScopeGateAction(data["action"]),
            priority=data.get("priority", 100),
            routing_category=routing_cat,
            clarification_question=data.get("clarification"),
            missing_facts=data.get("missing_facts", []),
        )


@dataclass
class DomainRuleSet:
    """Collection of rules for a single domain, sorted by priority."""

    domain: Domain
    rules: list[DomainRule] = field(default_factory=list)

    def __post_init__(self):
        self.rules.sort(key=lambda r: r.priority)


# ---------------------------------------------------------------------------
# Condition evaluator — sandboxed evaluation of rule conditions
# ---------------------------------------------------------------------------


class ConditionEvaluator:
    """Evaluates rule conditions against a TriageFact.

    Conditions are Python expressions that reference TriageFact fields.
    They are evaluated in a restricted namespace with only the TriageFact's
    data available — no builtins, no imports, no side effects.

    Examples of valid conditions:
        "key_facts.get('cheque_amount') is None"
        "involves_criminal_element == True"
        "domain_confidence >= 0.7"
        "key_facts.get('fraud_context') == True"
    """

    @staticmethod
    def evaluate(condition: str, triage_fact: TriageFact) -> bool:
        """Evaluate a condition expression against a TriageFact.

        Returns False on any error (fail-safe: don't match if condition
        evaluation fails).
        """
        # Build a restricted namespace from the TriageFact
        namespace = {
            # Direct TriageFact fields
            "detected_domain": triage_fact.detected_domain,
            "domain_confidence": triage_fact.domain_confidence,
            "country": triage_fact.country,
            "state": triage_fact.state,
            "district": triage_fact.district,
            "involves_criminal_element": triage_fact.involves_criminal_element,
            "involves_minor_children": triage_fact.involves_minor_children,
            "has_active_litigation": triage_fact.has_active_litigation,
            "has_court_deadline": triage_fact.has_court_deadline,
            "has_self_harm_violence": triage_fact.has_self_harm_violence,
            "document_present": triage_fact.document_present,
            "document_contradicts_spoken": triage_fact.document_contradicts_spoken,
            "caller_intent": triage_fact.caller_intent,
            "overall_confidence": triage_fact.overall_confidence,
            "key_facts": triage_fact.key_facts,
            "missing_critical_facts": triage_fact.missing_critical_facts,
            "clarification_attempts": triage_fact.clarification_attempts,
            # Safe builtins for conditions
            "len": len,
            "True": True,
            "False": False,
            "None": None,
        }

        try:
            result = eval(condition, {"__builtins__": {}}, namespace)  # noqa: S307
            return bool(result)
        except Exception as e:
            logger.warning(
                "Condition evaluation failed for '%s': %s. Defaulting to False (fail-safe).",
                condition,
                e,
            )
            return False


# ---------------------------------------------------------------------------
# Universal hard-stops — built into engine, cannot be overridden
# ---------------------------------------------------------------------------


def _check_universal_hard_stops(triage_fact: TriageFact) -> ScopeGateDecision | None:
    """Evaluate universal hard-stop conditions.

    These are checked BEFORE any domain-specific rules and cannot be
    overridden by domain rules. They are implemented in code (not YAML)
    because they are safety-critical invariants.

    Returns a ScopeGateDecision if a hard-stop triggers, None otherwise.
    """

    # 1. Self-harm / violence — highest priority, always first
    if triage_fact.has_self_harm_violence:
        return ScopeGateDecision(
            action=ScopeGateAction.HARD_STOP,
            reason="Caller disclosed self-harm or violence. Routing to crisis helpline.",
            rule_id="UNIVERSAL_CRISIS_001",
            routing_target=RoutingTarget(
                category=RoutingTargetCategory.CRISIS_ICALL,
                authority_name="iCall Psychosocial Helpline",
                phone="9152987821",
            ),
        )

    # 2. Active litigation
    if triage_fact.has_active_litigation:
        return ScopeGateDecision(
            action=ScopeGateAction.HARD_STOP,
            reason="Caller has active litigation. Must consult lawyer of record.",
            rule_id="UNIVERSAL_LITIGATION_001",
            routing_target=RoutingTarget(
                category=RoutingTargetCategory.GENERIC_LAWYER,
                authority_name="Your existing lawyer / legal aid",
            ),
        )

    # 3. Court deadline
    if triage_fact.has_court_deadline:
        return ScopeGateDecision(
            action=ScopeGateAction.HARD_STOP,
            reason="Caller has a live court deadline. Urgent lawyer consultation needed.",
            rule_id="UNIVERSAL_DEADLINE_001",
            routing_target=RoutingTarget(
                category=RoutingTargetCategory.LEGAL_AID_DLSA,
                authority_name="District Legal Services Authority (urgent)",
            ),
        )

    # 4. Criminal element
    if triage_fact.involves_criminal_element:
        return ScopeGateDecision(
            action=ScopeGateAction.HARD_STOP,
            reason="Criminal element detected. Routing to police/criminal lawyer.",
            rule_id="UNIVERSAL_CRIMINAL_001",
            routing_target=RoutingTarget(
                category=RoutingTargetCategory.POLICE_HELPLINE,
                authority_name="Police Helpline",
                phone="112",
            ),
        )

    # 5. Minor children in sensitive context
    if triage_fact.involves_minor_children and triage_fact.detected_domain in {
        Domain.TENANCY,
        Domain.EMPLOYMENT,
        Domain.UNKNOWN,
    }:
        return ScopeGateDecision(
            action=ScopeGateAction.HARD_STOP,
            reason="Minor children involved in sensitive legal context. Routing to legal aid.",
            rule_id="UNIVERSAL_MINOR_001",
            routing_target=RoutingTarget(
                category=RoutingTargetCategory.FAMILY_COURT,
                authority_name="Family Court / Legal Aid",
            ),
        )

    # 6. Document contradiction (soft-stop, not hard)
    if triage_fact.document_contradicts_spoken is True:
        return ScopeGateDecision(
            action=ScopeGateAction.SOFT_STOP,
            reason="Document contradicts spoken account. Flagging discrepancy.",
            rule_id="UNIVERSAL_CONTRADICTION_001",
            routing_target=RoutingTarget(
                category=RoutingTargetCategory.GENERIC_LAWYER,
                authority_name="Lawyer review recommended for document discrepancy",
            ),
        )

    # 7. Domain unknown after multiple attempts
    if (
        triage_fact.detected_domain == Domain.UNKNOWN
        and triage_fact.domain_confidence < 0.4
        and triage_fact.clarification_attempts >= 3
    ):
        return ScopeGateDecision(
            action=ScopeGateAction.HARD_STOP,
            reason="Unable to classify legal domain after 3 clarification attempts.",
            rule_id="UNIVERSAL_UNCLASSIFIED_001",
            routing_target=RoutingTarget(
                category=RoutingTargetCategory.LEGAL_AID_DLSA,
                authority_name="District Legal Services Authority",
            ),
        )

    return None


# ---------------------------------------------------------------------------
# Scope Gate Engine
# ---------------------------------------------------------------------------


class ScopeGateEngine:
    """The deterministic Scope Gate rule engine.

    Evaluates TriageFact objects against universal hard-stops and
    domain-specific rules to produce ScopeGateDecision outputs.

    Usage:
        engine = ScopeGateEngine()
        engine.load_domain_rules(Path("config/scope_gate_rules/"))
        decision = engine.evaluate(triage_fact)
    """

    def __init__(self):
        self._domain_rules: dict[Domain, DomainRuleSet] = {}
        self._evaluator = ConditionEvaluator()

    def load_domain_rules(self, rules_dir: Path) -> None:
        """Load all domain rule YAML files from a directory.

        Each file should be named <domain>.yaml and contain a `domain` key
        and a `rules` list.
        """
        if not rules_dir.is_dir():
            logger.warning("Rules directory does not exist: %s", rules_dir)
            return

        for rule_file in rules_dir.glob("*.yaml"):
            try:
                self._load_rule_file(rule_file)
            except Exception as e:
                logger.error("Failed to load rule file %s: %s", rule_file, e)
                raise  # Rule loading failures are fatal — safety-critical config

    def _load_rule_file(self, rule_file: Path) -> None:
        """Load a single domain rule YAML file."""
        with open(rule_file) as f:
            data = yaml.safe_load(f)

        domain = Domain(data["domain"])
        rules = [DomainRule.from_dict(r, domain) for r in data.get("rules", [])]
        rule_set = DomainRuleSet(domain=domain, rules=rules)
        self._domain_rules[domain] = rule_set
        logger.info(
            "Loaded %d rules for domain %s from %s",
            len(rules),
            domain,
            rule_file.name,
        )

    def register_domain_rules(self, domain: Domain, rules: list[DomainRule]) -> None:
        """Programmatically register rules for a domain (useful for testing)."""
        rule_set = DomainRuleSet(domain=domain, rules=rules)
        self._domain_rules[domain] = rule_set

    def evaluate(self, triage_fact: TriageFact) -> ScopeGateDecision:
        """Evaluate a TriageFact and return a ScopeGateDecision.

        Evaluation order (short-circuit):
        1. Universal hard-stops (code-based, not overridable)
        2. Domain classification check
        3. Domain-specific rules (YAML-based, priority-ordered)
        4. Completeness check (missing critical facts)
        5. Confidence check
        6. Fail-safe default (HARD_STOP)
        """

        # --- Step 1: Universal hard-stops ---
        universal_decision = _check_universal_hard_stops(triage_fact)
        if universal_decision is not None:
            logger.info(
                "Universal hard-stop triggered: %s (rule: %s)",
                universal_decision.reason,
                universal_decision.rule_id,
            )
            return universal_decision

        # --- Step 2: Domain classification check ---
        if triage_fact.detected_domain == Domain.UNKNOWN:
            if triage_fact.clarification_attempts < 3:
                return ScopeGateDecision(
                    action=ScopeGateAction.CLARIFY,
                    reason="Domain not yet identified. Asking caller to describe their situation.",
                    rule_id="ENGINE_DOMAIN_CLARIFY_001",
                    clarification_question=(
                        "Could you tell me a bit more about your situation? "
                        "For example, is this about a purchase or service issue, "
                        "a rental matter, a workplace issue, an insurance claim, "
                        "a police complaint, or a cheque that bounced?"
                    ),
                    missing_facts=["legal_domain"],
                )
            # Clarification exhausted but confidence >= 0.4 — try best guess
            # (if confidence < 0.4, universal hard-stop above already caught it)

        # --- Step 3: Domain-specific rules ---
        domain = triage_fact.detected_domain
        if domain in self._domain_rules:
            rule_set = self._domain_rules[domain]
            for rule in rule_set.rules:
                if self._evaluator.evaluate(rule.condition, triage_fact):
                    logger.info(
                        "Domain rule matched: %s (%s) → %s",
                        rule.id,
                        rule.description,
                        rule.action,
                    )
                    return self._build_decision_from_rule(rule)

        # --- Step 4: Completeness check ---
        if triage_fact.missing_critical_facts:
            if triage_fact.clarification_attempts < 3:
                missing = triage_fact.missing_critical_facts
                return ScopeGateDecision(
                    action=ScopeGateAction.CLARIFY,
                    reason=f"Missing critical facts: {', '.join(missing)}",
                    rule_id="ENGINE_COMPLETENESS_001",
                    clarification_question=None,  # Let the Fast Model formulate the question
                    missing_facts=missing,
                )

        # --- Step 5: Confidence check ---
        if triage_fact.overall_confidence < 0.5:
            return ScopeGateDecision(
                action=ScopeGateAction.SOFT_STOP,
                reason=(
                    f"Overall confidence too low ({triage_fact.overall_confidence:.2f}). "
                    "Routing to lawyer."
                ),
                rule_id="ENGINE_LOW_CONFIDENCE_001",
                routing_target=RoutingTarget(
                    category=RoutingTargetCategory.LEGAL_AID_DLSA,
                    authority_name="District Legal Services Authority",
                ),
            )

        # --- Step 6: Fail-safe default ---
        # If we reach here with no rule matched and no completeness/confidence issue,
        # this is a gap in rule coverage. Fail safe.
        if domain not in self._domain_rules:
            return ScopeGateDecision(
                action=ScopeGateAction.HARD_STOP,
                reason=f"No rules loaded for domain {domain}. Cannot safely proceed.",
                rule_id="ENGINE_NO_RULES_001",
                routing_target=RoutingTarget(
                    category=RoutingTargetCategory.LEGAL_AID_DLSA,
                    authority_name="District Legal Services Authority",
                ),
            )

        # Domain rules exist but none matched — this is a rule coverage gap.
        # Proceed cautiously with soft-stop if confidence is reasonable.
        if triage_fact.overall_confidence >= 0.7:
            return ScopeGateDecision(
                action=ScopeGateAction.PROCEED,
                reason="No specific rule matched but confidence is high. Proceeding with retrieval.",
                rule_id="ENGINE_DEFAULT_PROCEED_001",
            )

        return ScopeGateDecision(
            action=ScopeGateAction.SOFT_STOP,
            reason="No specific rule matched and confidence is moderate. Routing as precaution.",
            rule_id="ENGINE_DEFAULT_SOFT_STOP_001",
            routing_target=RoutingTarget(
                category=RoutingTargetCategory.LEGAL_AID_DLSA,
                authority_name="District Legal Services Authority",
            ),
        )

    @staticmethod
    def _build_decision_from_rule(rule: DomainRule) -> ScopeGateDecision:
        """Convert a matched DomainRule into a ScopeGateDecision."""
        routing_target = None
        if rule.routing_category:
            routing_target = RoutingTarget(category=rule.routing_category)

        return ScopeGateDecision(
            action=rule.action,
            reason=rule.description,
            rule_id=rule.id,
            routing_target=routing_target,
            clarification_question=rule.clarification_question,
            missing_facts=rule.missing_facts if rule.missing_facts else None,
        )
