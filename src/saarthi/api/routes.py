"""Dashboard and REST API routes for Saarthi.

Provides endpoints for the frontend dashboard to fetch statistics,
call history, user profiles, and active calls.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from saarthi.core import state

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["Dashboard"])


@router.get("/dashboard/stats")
async def get_dashboard_stats() -> dict[str, Any]:
    """Get high-level dashboard statistics.

    Returns:
        A dictionary containing total calls, today's calls, unique callers,
        active calls, and high-risk calls.
    """
    if state.db is None:
        raise HTTPException(status_code=503, detail="Database not initialized")

    stats = await state.db.get_dashboard_stats()
    return stats.model_dump()


@router.get("/calls")
async def list_calls(
    search: str | None = Query(None, description="Search by phone number"),
    topic: str | None = Query(None, description="Filter by call topic"),
    risk_level: str | None = Query(None, description="Filter by risk level"),
    status: str | None = Query(None, description="Filter by call status"),
    date_filter: str | None = Query(None, description="Filter by date (e.g., 'today', 'week')"),
    limit: int = Query(50, description="Max number of calls to return"),
    offset: int = Query(0, description="Pagination offset"),
) -> list[dict[str, Any]]:
    """List calls with optional filtering and pagination.

    Args:
        search: Optional string to search phone numbers.
        topic: Optional topic filter.
        risk_level: Optional risk level filter.
        status: Optional status filter.
        date_filter: Optional predefined date range.
        limit: Pagination limit.
        offset: Pagination offset.

    Returns:
        A list of call dictionaries matching the criteria.
    """
    if state.db is None:
        raise HTTPException(status_code=503, detail="Database not initialized")

    calls = await state.db.list_calls(
        search=search,
        topic=topic,
        risk_level=risk_level,
        status=status,
        date_filter=date_filter,
        limit=limit,
        offset=offset,
    )
    return [c.model_dump() for c in calls]


@router.get("/calls/active")
async def get_active_calls() -> list[dict[str, Any]]:
    """Get currently active calls.

    Returns:
        A list of active call dictionaries.
    """
    if state.db is None:
        raise HTTPException(status_code=503, detail="Database not initialized")

    calls = await state.db.get_active_calls()
    return [c.model_dump() for c in calls]


@router.get("/calls/{call_id}")
async def get_call_detail(call_id: str) -> dict[str, Any]:
    """Get full details for a specific call, including transcript messages.

    Args:
        call_id: The internal UUID of the call.

    Returns:
        A dictionary containing call metadata, caller phone, and messages.

    Raises:
        HTTPException: If the call is not found.
    """
    if state.db is None:
        raise HTTPException(status_code=503, detail="Database not initialized")

    call = await state.db.get_call(call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")

    messages = await state.db.get_messages_for_call(call_id)
    user = await state.db.get_user_by_id(call.user_id)

    masked_phone = user.masked_phone if user else "Unknown"

    return {
        "call": call.model_dump(),
        "caller_phone": masked_phone,
        "messages": [m.model_dump() for m in messages],
    }


@router.get("/users/{user_id}")
async def get_user_profile(user_id: str) -> dict[str, Any]:
    """Get user profile along with their complete call history.

    Args:
        user_id: The internal UUID of the user.

    Returns:
        A dictionary containing the user's profile and call list.

    Raises:
        HTTPException: If the user is not found.
    """
    if state.db is None:
        raise HTTPException(status_code=503, detail="Database not initialized")

    user = await state.db.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    calls = await state.db.get_user_calls(user_id)

    return {
        "user": user.model_dump(),
        "masked_phone": user.masked_phone,
        "calls": [c.model_dump() for c in calls],
    }


@router.get("/config/phone")
async def get_phone_number() -> dict[str, str]:
    """Get the configured Saarthi phone number for display on the dashboard.

    Returns:
        A dictionary containing the phone_number string.
    """
    if state.config is None:
        return {"phone_number": ""}

    return {
        "phone_number": state.config.saarthi_phone_number,
    }
