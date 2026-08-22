"""Twilio Voice & Media Streams WebSocket Integration for Saarthi.

Handles incoming calls from Twilio and connects them to a real-time
two-way conversational AI pipeline:
Twilio (G.711 μ-law) ⇄ PCM ⇄ Sarvam STT ⇄ Gemini 2.0 ⇄ Sarvam TTS ⇄ μ-law ⇄ Twilio.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import struct
from datetime import datetime
from typing import Any

try:
    import audioop
except ImportError:
    import audioop_lts as audioop  # type: ignore

import httpx
from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect
from google import genai
from google.genai import types

from saarthi.core import state
from saarthi.models.core import Call
from saarthi.models.enums import CallStatus

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Twilio Voice & Media Streams"])

# Twilio Audio Specs: 8000 Hz, 8-bit mu-law, Mono
TWILIO_SAMPLE_RATE = 8000
SILENCE_RMS_THRESHOLD = 300
SILENCE_DURATION_MS = 1400
CHUNK_DURATION_MS = 20  # Twilio sends ~20ms frames (160 bytes mu-law)

# Pre-cached greeting in mu-law format
_CACHED_GREETING_MULAW: bytes | None = None
_GREETING_TEXT = (
    "नमस्ते! साथी कानूनी सहायता लाइन में आपका स्वागत है। "
    "कृपया अपनी कानूनी समस्या बताएं।"
)


@router.api_route("/api/twilio/incoming", methods=["GET", "POST"])
async def twilio_incoming_voice(request: Request) -> Response:
    """TwiML Webhook endpoint for Twilio phone numbers.

    Returns TwiML instructing Twilio to connect a bidirectional WebSocket stream.
    """
    # Detect public host
    host = os.getenv("PUBLIC_DOMAIN", "")
    if not host:
        host = request.headers.get("host", "exotel-vml4.onrender.com")

    ws_url = f"wss://{host}/ws/twilio"
    logger.info("📞 Twilio Incoming Call — Connecting to stream: %s", ws_url)

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{ws_url}" />
    </Connect>
</Response>"""

    return Response(content=twiml, media_type="application/xml")


@router.websocket("/ws/twilio")
async def twilio_websocket(websocket: WebSocket):
    """Twilio Media Streams WebSocket handler."""
    await websocket.accept()

    stream_sid = ""
    call_sid = ""
    session: dict[str, Any] = {
        "audio_buffer_pcm": bytearray(),
        "turns": [],
        "silence_chunks": 0,
        "is_speaking": False,
        "processing": False,
    }

    logger.info("🔌 Twilio WebSocket connected")

    try:
        greeting_sent = False

        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            event = msg.get("event", "")

            if event == "start":
                start_data = msg.get("start", {})
                stream_sid = msg.get("streamSid", start_data.get("streamSid", ""))
                call_sid = start_data.get("callSid", "")

                logger.info(
                    "🎙️ Twilio START — streamSid=%s  callSid=%s",
                    stream_sid, call_sid,
                )

                session["stream_sid"] = stream_sid
                session["call_sid"] = call_sid

                # Persist call in DB
                try:
                    if state.db is not None and call_sid:
                        from_number = start_data.get("customParameters", {}).get("from", "Twilio Caller")
                        user = await state.db.create_or_get_user(from_number)
                        call = Call(
                            exotel_call_sid=call_sid,
                            user_id=user.id,
                            status=CallStatus.ACTIVE,
                        )
                        await state.db.create_call(call)
                except Exception as e:
                    logger.debug("DB call record error: %s", e)

                # Send initial greeting immediately
                if not greeting_sent:
                    greeting_sent = True
                    asyncio.create_task(_send_twilio_greeting(websocket, session, stream_sid))

            elif event == "media":
                if session["processing"]:
                    continue

                media = msg.get("media", {})
                payload = media.get("payload", "")

                if payload:
                    mulaw_bytes = base64.b64decode(payload)
                    # Convert 8kHz mu-law to 16-bit linear PCM
                    pcm_bytes = audioop.ulaw2lin(mulaw_bytes, 2)
                    session["audio_buffer_pcm"].extend(pcm_bytes)

                    # Silence / Voice Activity Detection
                    rms = _calculate_rms(pcm_bytes)
                    if rms < SILENCE_RMS_THRESHOLD:
                        session["silence_chunks"] += 1
                    else:
                        session["silence_chunks"] = 0
                        session["is_speaking"] = True

                    silence_ms = session["silence_chunks"] * CHUNK_DURATION_MS
                    min_audio = TWILIO_SAMPLE_RATE * 2 * 1  # 1 sec

                    if (
                        session["is_speaking"]
                        and silence_ms >= SILENCE_DURATION_MS
                        and len(session["audio_buffer_pcm"]) >= min_audio
                        and not session["processing"]
                    ):
                        session["processing"] = True
                        session["is_speaking"] = False

                        audio_pcm = bytes(session["audio_buffer_pcm"])
                        session["audio_buffer_pcm"] = bytearray()
                        session["silence_chunks"] = 0

                        asyncio.create_task(
                            _process_twilio_turn(websocket, session, stream_sid, audio_pcm)
                        )

            elif event == "stop":
                logger.info("🛑 Twilio STOP event (stream=%s)", stream_sid)
                break

    except WebSocketDisconnect:
        logger.info("🔌 Twilio WebSocket disconnected (stream=%s)", stream_sid)
    except Exception as e:
        logger.error("Twilio WebSocket error: %s", e)
    finally:
        logger.info("🔌 Twilio session ended (stream=%s)", stream_sid)


async def _send_twilio_greeting(
    websocket: WebSocket,
    session: dict[str, Any],
    stream_sid: str,
):
    """Send pre-cached greeting in Twilio mu-law format."""
    logger.info("🗣️ Sending Twilio greeting audio...")
    try:
        mulaw_bytes = await _get_cached_twilio_greeting()
        if mulaw_bytes:
            session["turns"].append({
                "role": "assistant",
                "content": _GREETING_TEXT,
                "time": datetime.utcnow().isoformat(),
            })
            await _stream_mulaw_to_twilio(websocket, stream_sid, mulaw_bytes)
            logger.info("✅ Twilio greeting delivered")
    except Exception as e:
        logger.error("Twilio greeting send error: %s", e)


async def _get_cached_twilio_greeting() -> bytes | None:
    """Get or generate pre-cached mu-law audio for greeting."""
    global _CACHED_GREETING_MULAW
    if _CACHED_GREETING_MULAW is None:
        pcm = await _tts_pcm(_GREETING_TEXT)
        if pcm:
            _CACHED_GREETING_MULAW = audioop.lin2ulaw(pcm, 2)
    return _CACHED_GREETING_MULAW


async def _process_twilio_turn(
    websocket: WebSocket,
    session: dict[str, Any],
    stream_sid: str,
    audio_pcm: bytes,
):
    """Process a user spoken turn: STT -> Gemini -> TTS -> Twilio."""
    try:
        logger.info("🎧 Running STT on %d bytes of PCM audio...", len(audio_pcm))
        user_text = await _stt(audio_pcm)

        if not user_text or not user_text.strip():
            logger.info("🔇 No speech detected, skipping turn")
            session["processing"] = False
            return

        logger.info("🗣️ Twilio Caller: '%s'", user_text)
        session["turns"].append({
            "role": "user",
            "content": user_text,
            "time": datetime.utcnow().isoformat(),
        })

        ai_response = await _generate_gemini_response(session)
        logger.info("🤖 AI response: '%s'", ai_response[:80])
        session["turns"].append({
            "role": "assistant",
            "content": ai_response,
            "time": datetime.utcnow().isoformat(),
        })

        # Generate TTS PCM and convert to mu-law for Twilio
        response_pcm = await _tts_pcm(ai_response)
        if response_pcm:
            response_mulaw = audioop.lin2ulaw(response_pcm, 2)
            await _stream_mulaw_to_twilio(websocket, stream_sid, response_mulaw)

    except Exception as e:
        logger.error("Twilio turn processing error: %s", e, exc_info=True)
    finally:
        session["processing"] = False


async def _stream_mulaw_to_twilio(websocket: WebSocket, stream_sid: str, mulaw_data: bytes):
    """Stream mu-law audio chunks to Twilio (~20ms per packet = 160 bytes)."""
    chunk_size = 320  # ~40ms per packet

    for i in range(0, len(mulaw_data), chunk_size):
        chunk = mulaw_data[i : i + chunk_size]
        payload = base64.b64encode(chunk).decode("utf-8")

        msg = {
            "event": "media",
            "streamSid": stream_sid,
            "media": {
                "payload": payload,
            },
        }

        try:
            await websocket.send_text(json.dumps(msg))
        except Exception:
            return

        await asyncio.sleep(0.035)


async def _tts_pcm(text: str) -> bytes | None:
    """Generate 8kHz 16-bit PCM audio via Sarvam TTS."""
    sarvam_key = os.getenv("SARVAM_API_KEY", "")
    if not sarvam_key:
        return None

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                "https://api.sarvam.ai/text-to-speech",
                headers={
                    "api-subscription-key": sarvam_key,
                    "Content-Type": "application/json",
                },
                json={
                    "inputs": [text[:500]],
                    "target_language_code": "hi-IN",
                    "speaker": "anushka",
                    "pitch": 0,
                    "pace": 1.0,
                    "loudness": 1.5,
                    "speech_sample_rate": TWILIO_SAMPLE_RATE,
                    "enable_preprocessing": True,
                    "model": "bulbul:v2",
                },
            )

            if response.status_code == 200:
                audio_b64 = response.json()["audios"][0]
                wav_bytes = base64.b64decode(audio_b64)
                # Skip WAV header (44 bytes) to get raw PCM
                idx = wav_bytes.find(b"data")
                return wav_bytes[idx + 8 :] if idx != -1 else wav_bytes
    except Exception as e:
        logger.error("TTS error: %s", e)
    return None


async def _stt(audio_pcm: bytes) -> str:
    """Convert PCM audio to text via Sarvam STT."""
    sarvam_key = os.getenv("SARVAM_API_KEY", "")
    if not sarvam_key:
        return ""

    # Wrap PCM in WAV container
    data_size = len(audio_pcm)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_size, b"WAVE", b"fmt ", 16, 1,
        1, TWILIO_SAMPLE_RATE, TWILIO_SAMPLE_RATE * 2, 2, 16,
        b"data", data_size,
    )
    wav_bytes = header + audio_pcm

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                "https://api.sarvam.ai/speech-to-text",
                headers={"api-subscription-key": sarvam_key},
                files={"file": ("audio.wav", wav_bytes, "audio/wav")},
                data={
                    "language_code": "hi-IN",
                    "model": "saarika:v2.5",
                    "with_timestamps": "false",
                },
            )
            if response.status_code == 200:
                return response.json().get("transcript", "")
    except Exception as e:
        logger.error("STT error: %s", e)
    return ""


async def _generate_gemini_response(session: dict[str, Any]) -> str:
    """Generate short Hindi response using Gemini."""
    api_key = state.config.gemini_api_key if state.config else os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return "माफ करें, अभी AI सेवा उपलब्ध नहीं है।"

    try:
        client = genai.Client(api_key=api_key)
        system_prompt = (
            "You are Saarthi, a helpful Hindi-speaking legal assistant on a phone call. "
            "You help callers understand their legal rights, especially regarding "
            "cheque bounce cases (NI Act Section 138), property disputes, consumer "
            "complaints, and other civil matters. "
            "Respond ONLY in Hindi (Devanagari script). Keep responses very concise "
            "(1-2 sentences max) since this is a live phone call. Be empathetic and professional. "
            "Always clarify you provide general legal information, not legal advice."
        )

        history = []
        for turn in session.get("turns", [])[-8:]:
            label = "Caller" if turn["role"] == "user" else "Saarthi"
            history.append(f"{label}: {turn['content']}")

        prompt = "\n".join(history)
        response = await asyncio.to_thread(
            client.models.generate_content,
            model="gemini-2.0-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.4,
            ),
        )
        if response and response.text:
            return response.text.strip()
    except Exception as e:
        logger.error("Gemini error: %s", e)

    return "मैं आपकी बात समझ रहा हूँ। कृपया अपनी समस्या के बारे में थोड़ा और बताएं।"


def _calculate_rms(pcm_data: bytes) -> float:
    """Calculate RMS energy of PCM audio."""
    n_samples = len(pcm_data) // 2
    if n_samples == 0:
        return 0.0
    try:
        samples = struct.unpack(f"<{n_samples}h", pcm_data[:n_samples * 2])
        sum_sq = sum(s * s for s in samples)
        return (sum_sq / n_samples) ** 0.5
    except struct.error:
        return 0.0
