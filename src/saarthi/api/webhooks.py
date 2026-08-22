"""Exotel Webhook endpoints for Saarthi.

Handles incoming call lifecycle events from Exotel's telephony platform.
Supports the Passthru applet (fast 200 OK acknowledgement) and the
Gather applet (multi-turn AI conversation via Gemini).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

from google import genai
from google.genai import types
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from saarthi.core import state
from saarthi.models.core import Call, ConversationMessage
from saarthi.models.enums import CallStatus, MessageRole

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/webhooks", tags=["Exotel Webhooks"])

# ---------------------------------------------------------------------------
# In-memory conversation state for active calls
# ---------------------------------------------------------------------------
_active_calls: dict[str, dict[str, Any]] = {}


def _extract_params(request: Request, body: dict | None = None) -> dict[str, str]:
    """Merge query params and body params into a single dict."""
    params = dict(request.query_params)
    if body:
        params.update({k: str(v) for k, v in body.items()})
    return params


async def _parse_body(request: Request) -> dict | None:
    """Parse POST body as JSON or form data."""
    if request.method != "POST":
        return None
    content_type = request.headers.get("content-type", "")
    if "json" in content_type:
        try:
            return await request.json()
        except Exception:
            return {}
    try:
        form = await request.form()
        return dict(form)
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# 1. Passthru Endpoint — Exotel hits this first
# ---------------------------------------------------------------------------


@router.api_route("/exotel", methods=["GET", "POST"])
async def exotel_passthru(request: Request) -> JSONResponse:
    """Exotel Passthru Webhook.

    This MUST return 200 OK within ~2 seconds or Exotel drops the call.
    We log the incoming call and immediately acknowledge.
    """
    body = await _parse_body(request)
    params = _extract_params(request, body)

    call_sid = params.get("CallSid", "unknown")
    call_from = params.get("CallFrom", params.get("From", "unknown"))
    call_to = params.get("CallTo", params.get("To", "unknown"))
    direction = params.get("Direction", "unknown")

    logger.info(
        "📞 EXOTEL INCOMING — CallSid=%s  From=%s  To=%s  Direction=%s",
        call_sid, call_from, call_to, direction,
    )

    # Store in-memory state for this call
    _active_calls[call_sid] = {
        "call_sid": call_sid,
        "from": call_from,
        "to": call_to,
        "direction": direction,
        "started_at": datetime.utcnow().isoformat(),
        "turns": [],
    }

    # Persist to database (non-blocking, best-effort)
    try:
        if state.db is not None:
            user = await state.db.create_or_get_user(call_from)
            call = Call(
                exotel_call_sid=call_sid,
                user_id=user.id,
                status=CallStatus.ACTIVE,
            )
            await state.db.create_call(call)
            await state.db.increment_user_calls(user.id)
            logger.info("Call record created in DB: %s", call_sid)
    except Exception as e:
        logger.error("Failed to persist call to DB: %s", e)

    # Return 200 OK immediately — critical for Exotel
    return JSONResponse({"status": "ok", "call_sid": call_sid})


# ---------------------------------------------------------------------------
# 2. Greeting Endpoint — plays initial message + starts gathering speech
# ---------------------------------------------------------------------------


@router.api_route("/exotel/greeting", methods=["GET", "POST"])
async def exotel_greeting(request: Request) -> Response:
    """Return plain text for Exotel's Greeting applet TTS.

    Exotel's Greeting applet, when configured with a URL, makes a GET request
    and uses the response as TTS text. It expects plain text (text/plain),
    NOT XML.
    """
    params = _extract_params(request)
    call_sid = params.get("CallSid", "unknown")

    logger.info("🎙️ Greeting for CallSid=%s", call_sid)

    greeting = (
        "नमस्ते! साथी कानूनी सहायता लाइन में आपका स्वागत है। "
        "कृपया अपनी कानूनी समस्या बताएं।"
    )
    return Response(content=greeting, media_type="text/plain; charset=utf-8")


# ---------------------------------------------------------------------------
# 3. Gather Endpoint — processes speech, returns AI response, loops
# ---------------------------------------------------------------------------


@router.api_route("/exotel/gather", methods=["GET", "POST"])
async def exotel_gather(request: Request) -> Response:
    """Process caller speech through Gemini AI and respond with XML."""
    body = await _parse_body(request)
    params = _extract_params(request, body)

    call_sid = params.get("CallSid", "unknown")
    speech = (
        params.get("speech_result", "")
        or params.get("SpeechResult", "")
        or params.get("digits", "")
        or params.get("Digits", "")
        or ""
    )

    logger.info("🗣️ Gather — CallSid=%s  Speech='%s'", call_sid, speech)

    ai_response = await _generate_ai_response(call_sid, speech)

    base_url = _get_base_url(request)
    gather_url = f"{base_url}/api/webhooks/exotel/gather"

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech" action="{gather_url}" method="POST" timeout="5" language="hi-IN">
        <Say language="hi-IN">{_escape_xml(ai_response)}</Say>
    </Gather>
    <Say language="hi-IN">आपसे बात करके अच्छा लगा। धन्यवाद।</Say>
</Response>"""
    return Response(content=xml, media_type="application/xml")


# ---------------------------------------------------------------------------
# 4. Status Callback — called when the call ends
# ---------------------------------------------------------------------------


@router.api_route("/exotel/status", methods=["GET", "POST"])
async def exotel_status(request: Request) -> JSONResponse:
    """Handle Exotel call-end status callbacks.

    Finalizes the call in the database, builds the transcript from
    conversation turns, and triggers Gemini analysis.
    """
    body = await _parse_body(request)
    params = _extract_params(request, body)

    call_sid = params.get("CallSid", "unknown")
    status = params.get("Status", params.get("DialCallStatus", "unknown"))
    duration = params.get("DialCallDuration", params.get("Duration", "0"))

    logger.info("📊 Status — CallSid=%s  Status=%s  Duration=%s", call_sid, status, duration)

    # Build transcript from in-memory turns
    call_data = _active_calls.pop(call_sid, {"turns": []})
    transcript_parts = []
    messages = []

    for turn in call_data.get("turns", []):
        role_label = "USER" if turn["role"] == "user" else "AI"
        transcript_parts.append(f"{role_label}: {turn['content']}")

    transcript = "\n".join(transcript_parts)

    # Update DB
    try:
        if state.db is not None:
            update = {
                "status": CallStatus.PROCESSING,
                "duration_seconds": float(duration),
                "transcript": transcript,
                "end_time": datetime.utcnow(),
            }
            await state.db.update_call(call_sid, **update)

            # Run Gemini analysis on the transcript
            if transcript and state.analyzer:
                try:
                    analysis = await state.analyzer.analyze(transcript)
                    await state.db.update_call(
                        call_sid,
                        status=CallStatus.COMPLETED,
                        summary=analysis.summary,
                        topic=analysis.topic,
                        risk_level=analysis.risk_level,
                        action_items=analysis.action_items,
                    )
                except Exception as e:
                    logger.error("Call analysis failed: %s", e)
                    await state.db.update_call(call_sid, status=CallStatus.COMPLETED)
            else:
                await state.db.update_call(call_sid, status=CallStatus.COMPLETED)
    except Exception as e:
        logger.error("Failed to finalize call in DB: %s", e)

    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _generate_ai_response(call_sid: str, user_speech: str) -> str:
    """Generate a conversational AI response using Gemini."""
    if not user_speech or not user_speech.strip():
        return "मुझे आपकी बात सुनाई नहीं दी। कृपया दोबारा बताएं।"

    call_data = _active_calls.get(call_sid, {"turns": []})
    call_data.setdefault("turns", []).append({
        "role": "user",
        "content": user_speech,
        "time": datetime.utcnow().isoformat(),
    })

    try:
        api_key = state.config.gemini_api_key if state.config else os.getenv("GEMINI_API_KEY", "")
        if api_key:
            client = genai.Client(api_key=api_key)

            system_prompt = (
                "You are Saarthi, a helpful Hindi-speaking legal assistant on a phone call. "
                "You help callers understand their legal rights, especially regarding "
                "cheque bounce cases (NI Act Section 138), property disputes, consumer "
                "complaints, and other civil matters. "
                "Respond in Hindi (Devanagari script). Keep responses concise (2-3 sentences) "
                "since this is a phone call. Be empathetic and professional. "
                "If someone mentions self-harm or violence, advise calling 112 immediately. "
                "Always clarify you provide general legal information, not legal advice."
            )

            history = []
            for turn in call_data.get("turns", [])[-6:]:
                label = "Caller" if turn["role"] == "user" else "Saarthi"
                history.append(f"{label}: {turn['content']}")

            prompt = "\n".join(history)

            import asyncio
            response = await asyncio.to_thread(
                client.models.generate_content,
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=0.4,
                ),
            )

            if response and response.text:
                ai_text = response.text.strip()
                call_data["turns"].append({
                    "role": "assistant",
                    "content": ai_text,
                    "time": datetime.utcnow().isoformat(),
                })
                _active_calls[call_sid] = call_data
                logger.info("🤖 Gemini → %s: %s", call_sid, ai_text[:80])
                return ai_text

    except Exception as e:
        logger.error("Gemini error: %s", e, exc_info=True)

    fallback = (
        "मैं आपकी बात समझ रहा हूँ। कृपया अपनी कानूनी समस्या के बारे में "
        "और विस्तार से बताएं।"
    )
    call_data["turns"].append({
        "role": "assistant",
        "content": fallback,
        "time": datetime.utcnow().isoformat(),
    })
    _active_calls[call_sid] = call_data
    return fallback


def _get_base_url(request: Request) -> str:
    """Reconstruct the public base URL from request headers or env."""
    base = os.getenv("PUBLIC_URL", "").rstrip("/")
    if base:
        return base
    scheme = request.headers.get("x-forwarded-proto", "https")
    host = request.headers.get("host", "localhost:8000")
    return f"{scheme}://{host}"


def _escape_xml(text: str) -> str:
    """Escape XML special characters."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
