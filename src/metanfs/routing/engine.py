"""
Routing Engine — resolves escalation targets to specific authorities with contact details.

Given a routing category, domain, and caller location, the engine looks up
the most specific routing target available (district > state > national > fallback).

Data source: static JSON files in config/routing_tables/.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from metanfs.models.core import RoutingRequest, RoutingResult
from metanfs.models.enums import RoutingTargetCategory

logger = logging.getLogger(__name__)


class RoutingEngine:
    """Resolves routing categories to specific authority contact details.

    Resolution order (most specific to least):
    1. Domain-specific + state-specific entry
    2. Domain-specific + national entry
    3. National fallback table entry
    4. Generic DLSA fallback
    """

    def __init__(self):
        self._domain_tables: dict[str, dict[str, Any]] = {}
        self._national_table: dict[str, Any] = {}

    def load_routing_tables(self, tables_dir: Path) -> None:
        """Load all routing table JSON files from a directory."""
        if not tables_dir.is_dir():
            logger.warning("Routing tables directory does not exist: %s", tables_dir)
            return

        for table_file in tables_dir.glob("*.json"):
            try:
                with open(table_file) as f:
                    data = json.load(f)

                if table_file.stem == "national":
                    self._national_table = data.get("targets", {})
                    logger.info(
                        "Loaded national routing table with %d targets",
                        len(self._national_table),
                    )
                else:
                    self._domain_tables[table_file.stem] = data.get("targets", {})
                    logger.info(
                        "Loaded routing table for domain '%s' with %d target categories",
                        table_file.stem,
                        len(data.get("targets", {})),
                    )
            except Exception as e:
                logger.error("Failed to load routing table %s: %s", table_file, e)

    def resolve(self, request: RoutingRequest) -> RoutingResult:
        """Resolve a routing request to a specific authority.

        Tries domain-specific tables first, then national fallback.
        """
        category_key = request.target_category.value

        # 1. Try domain-specific table
        domain_key = request.domain.value
        if domain_key in self._domain_tables:
            domain_targets = self._domain_tables[domain_key]
            if category_key in domain_targets:
                target_data = domain_targets[category_key]
                result = self._resolve_from_target_data(
                    target_data, request.caller_state, category_key
                )
                if result:
                    return result

        # 2. Try national fallback table
        if category_key in self._national_table:
            target_data = self._national_table[category_key]
            result = self._build_result_from_dict(target_data)
            if result:
                return result

        # 3. Ultimate fallback — DLSA
        logger.warning(
            "No routing entry found for category=%s, domain=%s. Using DLSA fallback.",
            category_key,
            domain_key,
        )
        return RoutingResult(
            authority_name="District Legal Services Authority (DLSA)",
            authority_type="Legal Aid",
            phone="15100",
            portal_url="https://nalsa.gov.in/lsam/",
            what_to_bring=["All relevant documents", "ID proof"],
        )

    def _resolve_from_target_data(
        self,
        target_data: dict[str, Any],
        caller_state: str | None,
        category_key: str,
    ) -> RoutingResult | None:
        """Resolve from a domain-specific target entry, preferring state-specific data."""

        # If target_data has jurisdiction-specific sub-entries (e.g., "maharashtra", "national")
        if "authority_name" in target_data:
            # Simple flat entry — no jurisdiction breakdown
            return self._build_result_from_dict(target_data)

        # Has jurisdiction sub-entries
        # Try state-specific first
        if caller_state and caller_state in target_data:
            state_entry = target_data[caller_state]
            # Merge with national entry for any missing fields
            national_entry = target_data.get("national", {})
            merged = {**national_entry, **state_entry}
            return self._build_result_from_dict(merged)

        # Fall back to national entry within the domain table
        if "national" in target_data:
            return self._build_result_from_dict(target_data["national"])

        return None

    @staticmethod
    def _build_result_from_dict(data: dict[str, Any]) -> RoutingResult:
        """Build a RoutingResult from a routing table dict entry."""
        return RoutingResult(
            authority_name=data.get("authority_name", "Unknown Authority"),
            authority_type=data.get("authority_type", "Unknown"),
            phone=data.get("phone"),
            address=data.get("address"),
            portal_url=data.get("portal_url"),
            what_to_bring=data.get("what_to_bring", []),
            filing_fee=data.get("filing_fee"),
            deadline_note=data.get("deadline_note"),
        )
