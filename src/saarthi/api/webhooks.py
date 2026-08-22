"""Vapi Webhook endpoints for Saarthi.

Handles incoming call lifecycle events from the external Vapi voice agent,
such as call started, status updates, transcripts, and end-of-call reports.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from saarthi.core import state
from saarthi.models.core import Call, ConversationMessage
from saarthi.models.enums import CallStatus, CallTopic, MessageRole, RiskLevel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/vapi", tags=["Webhooks"])


@router.post("/webhook")
async def vapi_webhook(request: Request) -> JSONResponse:
    """Receive and process Vapi call lifecycle events.

    Handles the following core flows:
    1. 'call-started' / 'status-update' (in-progress): Creates or updates an active call record.
    2. 'end-of-call-report' / 'hang': Finalizes the call, processes the transcript, and triggers AI analysis.

    Args:
        request: The incoming FastAPI request containing the JSON payload from Vapi.

    Returns:
        JSONResponse indicating success or failure. Always returns 200 on handled logic
        errors to prevent Vapi from continuously retrying the same payload.
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    # Vapi sends events wrapped in a "message" object depending on the webhook type
    message = payload.get("message", payload)
    event_type = message.get("type", "")

    logger.info("Vapi webhook received: type=%s", event_type)

    if state.db is None:
        logger.error("Database is not initialized. Cannot process webhook.")
        return JSONResponse({"status": "error", "detail": "Database unavailable"})

    try:
        if event_type in ("call-started", "status-update"):
            await _handle_call_started(message)
        elif event_type in ("end-of-call-report", "hang"):
            await _handle_call_ended(message)
        elif event_type == "transcript":
            # Real-time transcript updates can be handled here if needed in the future
            pass
        else:
            logger.debug("Unhandled Vapi event type: %s", event_type)
    except Exception as e:
        logger.error("Error processing Vapi webhook: %s", e, exc_info=True)
        # Return 200 even on error to prevent Vapi from retrying
        return JSONResponse({"status": "error", "detail": str(e)})

    return JSONResponse({"status": "ok"})


async def _handle_call_started(message: dict[str, Any]) -> None:
    """Handle a call-started or status-update event from Vapi.

    Creates a new user if they don't exist, and initializes a new call record
    marked as ACTIVE.

    Args:
        message: The webhook message payload.
    """
    call_data = message.get("call", message)
    vapi_call_id = call_data.get("id", "")
    if not vapi_call_id:
        logger.warning("No call ID in call-started event")
        return

    # Check if call already exists to prevent duplicates
    # state.db is guaranteed to be initialized here due to the check in the main route
    existing = await state.db.get_call_by_vapi_id(vapi_call_id) # type: ignore
    if existing:
        # Update status to active
        await state.db.update_call(vapi_call_id, status=CallStatus.ACTIVE) # type: ignore
        return

    # Extract caller phone number from the nested customer object
    customer = call_data.get("customer", {})
    phone_number = customer.get("number", "unknown")

    # Create or get user
    user = await state.db.create_or_get_user(phone_number) # type: ignore

    # Parse start time, fallback to current UTC time if parsing fails
    started_at = call_data.get("startedAt", call_data.get("createdAt", ""))
    try:
        start_time = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        start_time = datetime.utcnow()

    # Create call record
    call = Call(
        vapi_call_id=vapi_call_id,
        user_id=user.id,
        status=CallStatus.ACTIVE,
        start_time=start_time,
    )
    await state.db.create_call(call) # type: ignore
    await state.db.increment_user_calls(user.id) # type: ignore

    logger.info("Call started: vapi_id=%s, caller=%s", vapi_call_id, user.masked_phone)


async def _handle_call_ended(message: dict[str, Any]) -> None:
    """Handle end-of-call-report or hang event from Vapi.

    Finalizes the call duration, parses the transcript messages, and runs the
    AI CallAnalyzer to generate a summary, topic, risk level, and action items.

    Args:
        message: The webhook message payload containing the end-of-call data.
    """
    call_data = message.get("call", message)
    vapi_call_id = call_data.get("id", "")
    if not vapi_call_id:
        logger.warning("No call ID in call-ended event")
        return

    # Ensure call record exists (in case we missed the call-started webhook)
    existing = await state.db.get_call_by_vapi_id(vapi_call_id) # type: ignore
    if not existing:
        # Create a minimal record if we missed the start event
        customer = call_data.get("customer", {})
        phone_number = customer.get("number", "unknown")
        user = await state.db.create_or_get_user(phone_number) # type: ignore

        started_at = call_data.get("startedAt", call_data.get("createdAt", ""))
        try:
            start_time = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            start_time = datetime.utcnow()

        call = Call(
            vapi_call_id=vapi_call_id,
            user_id=user.id,
            status=CallStatus.PROCESSING,
            start_time=start_time,
        )
        await state.db.create_call(call) # type: ignore
        await state.db.increment_user_calls(user.id) # type: ignore

    # Calculate duration
    ended_at = call_data.get("endedAt", "")
    started_at = call_data.get("startedAt", "")
    duration = 0.0

    try:
        if ended_at and started_at:
            end_dt = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
            start_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            duration = (end_dt - start_dt).total_seconds()
        else:
            duration = call_data.get("duration", call_data.get("costBreakdown", {}).get("duration", 0.0))
    except (ValueError, AttributeError):
        pass

    # Extract transcript and individual messages
    transcript = ""
    messages_list = message.get("messages", message.get("artifact", {}).get("messages", []))

    conversation_messages = []
    if messages_list:
        transcript_parts = []
        for msg in messages_list:
            role_str = msg.get("role", "system")
            content = msg.get("content", msg.get("message", ""))
            if not content:
                continue

            # Map the Vapi role string to our internal MessageRole enum
            if role_str in ("user", "customer"):
                role = MessageRole.USER
                transcript_parts.append(f"USER: {content}")
            elif role_str in ("assistant", "bot", "ai"):
                role = MessageRole.ASSISTANT
                transcript_parts.append(f"AI: {content}")
            else:
                role = MessageRole.SYSTEM
                transcript_parts.append(f"SYSTEM: {content}")

            # Parse timestamp of the individual message
            msg_time_str = msg.get("time", msg.get("timestamp", ""))
            try:
                msg_time = datetime.fromisoformat(str(msg_time_str).replace("Z", "+00:00")) if msg_time_str else datetime.utcnow()
            except (ValueError, TypeError):
                msg_time = datetime.utcnow()

            # Append to conversation messages list to be saved in DB
            call_record = await state.db.get_call_by_vapi_id(vapi_call_id) # type: ignore
            if call_record:
                conversation_messages.append(
                    ConversationMessage(
                        call_id=call_record.id,
                        role=role,
                        content=content,
                        timestamp=msg_time,
                    )
                )

        transcript = "\n".join(transcript_parts)

    # Fallback to direct transcript string if message array was empty
    if not transcript:
        transcript = (
            message.get("transcript", "")
            or message.get("artifact", {}).get("transcript", "")
            or call_data.get("transcript", "")
        )

    # Extract recording URL
    recording_url = (
        message.get("recordingUrl", "")
        or message.get("artifact", {}).get("recordingUrl", "")
        or call_data.get("recordingUrl", "")
        or None
    )

    # Extract any pre-existing analysis from Vapi (if available)
    vapi_analysis = message.get("analysis", message.get("artifact", {}).get("analysis", {}))
    summary = ""
    topic = CallTopic.OTHER
    risk_level = RiskLevel.LOW
    action_items = []

    if vapi_analysis:
        summary = vapi_analysis.get("summary", "")
        structured = vapi_analysis.get("structuredData", {})
        if structured:
            topic_str = structured.get("topic", "")
            risk_str = structured.get("risk_level", structured.get("riskLevel", ""))
            action_items = structured.get("action_items", structured.get("actionItems", []))
            try:
                topic = CallTopic(topic_str.lower())
            except (ValueError, AttributeError):
                pass
            try:
                risk_level = RiskLevel(risk_str.lower())
            except (ValueError, AttributeError):
                pass

    # Initial update of call record with duration and transcript
    update_data = {
        "status": CallStatus.PROCESSING,
        "duration_seconds": duration,
        "transcript": transcript,
    }

    if ended_at:
        try:
            update_data["end_time"] = datetime.fromisoformat(ended_at.replace("Z", "+00:00")) # type: ignore
        except (ValueError, AttributeError):
            update_data["end_time"] = datetime.utcnow() # type: ignore
    else:
        update_data["end_time"] = datetime.utcnow() # type: ignore

    if recording_url:
        update_data["recording_url"] = recording_url

    await state.db.update_call(vapi_call_id, **update_data) # type: ignore

    # Store individual conversation messages
    if conversation_messages:
        await state.db.create_messages(conversation_messages) # type: ignore

    # Run AI analysis via Gemini if we don't have a summary from Vapi
    if not summary and transcript and state.analyzer:
        try:
            analysis = await state.analyzer.analyze(transcript)
            summary = analysis.summary
            topic = analysis.topic
            risk_level = analysis.risk_level
            action_items = analysis.action_items
        except Exception as e:
            logger.error("Call analysis failed: %s", e)

    # Final update with AI analysis results
    analysis_data = {
        "status": CallStatus.COMPLETED,
        "summary": summary,
        "topic": topic,
        "risk_level": risk_level,
        "action_items": action_items,
    }
    await state.db.update_call(vapi_call_id, **analysis_data) # type: ignore

    logger.info(
        "Call completed: vapi_id=%s, duration=%.0fs, topic=%s, risk=%s",
        vapi_call_id,
        duration,
        topic.value,
        risk_level.value,
    )
