"""Application state and singletons.

This module holds the initialized instances of core application components,
such as the configuration, database pool, and external service clients.
They are initialized during the FastAPI lifespan startup event.
"""

from __future__ import annotations

from typing import Optional

from saarthi.core.config import AppConfig
from saarthi.core.database import Database
from saarthi.services.call_analyzer import CallAnalyzer

# ---------------------------------------------------------------------------
# Global State Singletons
# ---------------------------------------------------------------------------

config: Optional[AppConfig] = None
"""Global application configuration instance."""

db: Optional[Database] = None
"""Global database connection pool instance."""

analyzer: Optional[CallAnalyzer] = None
"""Global CallAnalyzer instance for processing transcripts."""
