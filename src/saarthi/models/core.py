"""
Core data models for Saarthi.

These Pydantic models define the data structures for users, calls,
conversation messages, and call analysis results.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from saarthi.models.enums import (
    CallStatus,
    CallTopic,
    MessageRole,
    RiskLevel,
)


# ---------------------------------------------------------------------------
# User model
# ---------------------------------------------------------------------------


class User(BaseModel):
    """A caller identified by their phone number."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    phone_number: str  # Raw phone number (masked in API responses)
    first_seen: datetime = Field(default_factory=datetime.utcnow)
    last_seen: datetime = Field(default_factory=datetime.utcnow)
    total_calls: int = 0

    @property
    def masked_phone(self) -> str:
        """Return a privacy-masked version of the phone number.

        Example: +919876543210 → +91XXXXXX3210
        """
        phone = self.phone_number
        if len(phone) >= 10:
            # Keep country code + last 4 digits, mask the middle
            if phone.startswith("+"):
                # e.g., +919876543210
                country_and_prefix = phone[:3]  # +91
                last_four = phone[-4:]
                masked_middle = "X" * (len(phone) - 7)
                return f"{country_and_prefix}{masked_middle}{last_four}"
            else:
                last_four = phone[-4:]
                masked_middle = "X" * (len(phone) - 4)
                return f"{masked_middle}{last_four}"
        return phone


# ---------------------------------------------------------------------------
# Conversation message
# ---------------------------------------------------------------------------


class ConversationMessage(BaseModel):
    """A single message in a call's conversation."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    call_id: str
    role: MessageRole
    content: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Call analysis
# ---------------------------------------------------------------------------


class CallAnalysis(BaseModel):
    """AI-generated analysis of a completed call."""

    summary: str = ""
    topic: CallTopic = CallTopic.OTHER
    risk_level: RiskLevel = RiskLevel.LOW
    action_items: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Call model
# ---------------------------------------------------------------------------


class Call(BaseModel):
    """A complete call record."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    vapi_call_id: str  # External Vapi call ID for deduplication
    user_id: str
    status: CallStatus = CallStatus.INCOMING
    start_time: datetime = Field(default_factory=datetime.utcnow)
    end_time: datetime | None = None
    duration_seconds: float = 0.0
    transcript: str = ""
    recording_url: str | None = None

    # Analysis fields
    summary: str = ""
    topic: CallTopic = CallTopic.OTHER
    risk_level: RiskLevel = RiskLevel.LOW
    action_items: list[str] = Field(default_factory=list)

    # Metadata
    created_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def duration_display(self) -> str:
        """Format duration as 'Xm Ys'."""
        if self.duration_seconds <= 0:
            return "0s"
        minutes = int(self.duration_seconds // 60)
        seconds = int(self.duration_seconds % 60)
        if minutes > 0:
            return f"{minutes}m {seconds:02d}s"
        return f"{seconds}s"


# ---------------------------------------------------------------------------
# API response models
# ---------------------------------------------------------------------------


class DashboardStats(BaseModel):
    """Dashboard statistics."""

    total_calls: int = 0
    today_calls: int = 0
    unique_callers: int = 0
    active_calls: int = 0
    high_risk_calls: int = 0


class CallListItem(BaseModel):
    """Abbreviated call info for list views."""

    id: str
    vapi_call_id: str
    caller_phone: str  # Masked
    topic: str
    duration: str
    risk_level: str
    status: str
    start_time: str
    user_id: str


class CallDetail(BaseModel):
    """Full call detail including messages."""

    call: Call
    caller_phone: str  # Masked
    messages: list[ConversationMessage] = Field(default_factory=list)


class UserProfile(BaseModel):
    """User profile with call history."""

    user: User
    masked_phone: str
    calls: list[CallListItem] = Field(default_factory=list)
