"""Exotel Voicebot WebSocket endpoint for Saarthi.

Handles real-time bidirectional audio streaming with Exotel's Voicebot applet.

Protocol:
- Exotel connects via WebSocket and sends JSON events: start, media, dtmf
- Audio format: 16-bit Linear PCM (s16le), 8000 Hz, mono, base64-encoded
- We accumulate caller audio, run STT (Sarvam AI), generate AI response (Gemini),
  convert to speech (Sarvam TTS), and stream back to Exotel.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import struct
from datetime import datetime
from typing import Any

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from google import genai
from google.genai import types

from saarthi.core import state

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Voicebot WebSocket"])

# Audio settings for Exotel
SAMPLE_RATE = 8000
CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit = 2 bytes

# Silence detection: ~2 seconds of low-energy audio = end of speech
SILENCE_THRESHOLD = 300  # RMS energy threshold
SILENCE_DURATION_MS = 1500  # ms of silence before we process
CHUNK_DURATION_MS = 100  # Exotel sends ~100ms chunks

# Conversation state per stream
_sessions: dict[str, dict[str, Any]] = {}


@router.websocket("/ws/voicebot")
async def voicebot_websocket(websocket: WebSocket):
    """Handle Exotel Voicebot WebSocket connection.

    Full-duplex audio streaming:
    - Receives caller audio (PCM base64) from Exotel
    - Detects end of speech via silence detection
    - Runs Sarvam STT → Gemini AI → Sarvam TTS pipeline
    - Streams AI response audio back to Exotel
    """
    await websocket.accept()

    stream_sid = ""
    call_sid = ""
    session: dict[str, Any] = {
        "audio_buffer": bytearray(),
        "turns": [],
        "seq_out": 1,
        "chunk_out": 1,
        "silence_chunks": 0,
        "is_speaking": False,
        "processing": False,
    }

    logger.info("🔌 Voicebot WebSocket connected")

    try:
        # Send initial greeting as soon as connected
        greeting_sent = False

        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            event = msg.get("event", "")

            if event == "start":
                start_data = msg.get("start", {})
                stream_sid = start_data.get("stream_sid", msg.get("stream_sid", ""))
                call_sid = start_data.get("call_sid", "")
                media_format = start_data.get("media_format", {})

                logger.info(
                    "🎙️ Voicebot START — stream=%s  call=%s  format=%s",
                    stream_sid, call_sid, media_format,
                )

                session["stream_sid"] = stream_sid
                session["call_sid"] = call_sid
                _sessions[stream_sid] = session

                # Send greeting audio
                if not greeting_sent:
                    greeting_sent = True
                    asyncio.create_task(
                        _send_greeting(websocket, session, stream_sid)
                    )

            elif event == "media":
                if session["processing"]:
                    # Skip incoming audio while we're generating a response
                    continue

                media = msg.get("media", {})
                payload = media.get("payload", "")

                if payload:
                    pcm_bytes = base64.b64decode(payload)
                    session["audio_buffer"].extend(pcm_bytes)

                    # Check for silence (end of speech detection)
                    rms = _calculate_rms(pcm_bytes)

                    if rms < SILENCE_THRESHOLD:
                        session["silence_chunks"] += 1
                    else:
                        session["silence_chunks"] = 0
                        session["is_speaking"] = True

                    # If caller was speaking and we detect enough silence → process
                    silence_ms = session["silence_chunks"] * CHUNK_DURATION_MS
                    buffer_len = len(session["audio_buffer"])
                    min_audio = SAMPLE_RATE * SAMPLE_WIDTH * 1  # At least 1 sec of audio

                    if (
                        session["is_speaking"]
                        and silence_ms >= SILENCE_DURATION_MS
                        and buffer_len >= min_audio
                        and not session["processing"]
                    ):
                        session["processing"] = True
                        session["is_speaking"] = False

                        # Extract audio and clear buffer
                        audio_data = bytes(session["audio_buffer"])
                        session["audio_buffer"] = bytearray()
                        session["silence_chunks"] = 0

                        # Process in background
                        asyncio.create_task(
                            _process_turn(
                                websocket, session, stream_sid, audio_data
                            )
                        )

            elif event == "dtmf":
                digit = msg.get("dtmf", {}).get("digit", "")
                logger.info("📱 DTMF: %s (stream=%s)", digit, stream_sid)

            elif event == "clear":
                logger.info("🧹 Clear event (stream=%s)", stream_sid)
                session["audio_buffer"] = bytearray()

    except WebSocketDisconnect:
        logger.info("🔌 Voicebot WebSocket disconnected (stream=%s)", stream_sid)
    except Exception as e:
        logger.error("Voicebot WebSocket error: %s", e, exc_info=True)
    finally:
        _sessions.pop(stream_sid, None)
        logger.info("🔌 Voicebot session cleaned up (stream=%s)", stream_sid)


# Cached greeting PCM audio (pre-generated)
_CACHED_GREETING_PCM: bytes | None = None
_GREETING_TEXT = (
    "नमस्ते! साथी कानूनी सहायता लाइन में आपका स्वागत है। "
    "कृपया अपनी कानूनी समस्या बताएं।"
)


async def get_greeting_audio() -> bytes | None:
    """Get or generate cached greeting PCM audio."""
    global _CACHED_GREETING_PCM
    if _CACHED_GREETING_PCM is None:
        logger.info("🎙️ Pre-generating greeting audio via Sarvam TTS...")
        _CACHED_GREETING_PCM = await _text_to_speech(_GREETING_TEXT)
        if _CACHED_GREETING_PCM:
            logger.info("✅ Greeting audio pre-cached (%d bytes)", len(_CACHED_GREETING_PCM))
    return _CACHED_GREETING_PCM


async def _send_greeting(
    websocket: WebSocket,
    session: dict[str, Any],
    stream_sid: str,
):
    """Send pre-cached greeting audio immediately upon start."""
    logger.info("🗣️ Sending greeting audio (instant)...")
    try:
        audio_bytes = await get_greeting_audio()
        if not audio_bytes:
            audio_bytes = await _text_to_speech(_GREETING_TEXT)

        if audio_bytes:
            session["turns"].append({
                "role": "assistant",
                "content": _GREETING_TEXT,
                "time": datetime.utcnow().isoformat(),
            })
            await _stream_audio_to_exotel(websocket, session, stream_sid, audio_bytes)
            logger.info("✅ Greeting stream finished")
        else:
            logger.warning("⚠️ TTS unavailable for greeting")
    except Exception as e:
        logger.error("Greeting send error: %s", e)


async def _process_turn(
    websocket: WebSocket,
    session: dict[str, Any],
    stream_sid: str,
    audio_data: bytes,
):
    """Process a complete user utterance: STT → AI → TTS → stream back."""
    try:
        # 1. Speech-to-Text
        logger.info("🎧 Running STT on %d bytes of audio...", len(audio_data))
        user_text = await _speech_to_text(audio_data)

        if not user_text or not user_text.strip():
            logger.info("🔇 No speech detected, skipping turn")
            session["processing"] = False
            return

        logger.info("🗣️ User said: '%s'", user_text)
        session["turns"].append({
            "role": "user",
            "content": user_text,
            "time": datetime.utcnow().isoformat(),
        })

        # 2. Generate AI response via Gemini
        ai_response = await _generate_ai_response(session)
        logger.info("🤖 AI response: '%s'", ai_response[:80])
        session["turns"].append({
            "role": "assistant",
            "content": ai_response,
            "time": datetime.utcnow().isoformat(),
        })

        # 3. Text-to-Speech
        response_audio = await _text_to_speech(ai_response)

        if response_audio:
            # 4. Stream audio back to Exotel
            await _stream_audio_to_exotel(
                websocket, session, stream_sid, response_audio
            )
        else:
            logger.warning("⚠️ TTS failed, no audio to send")

    except Exception as e:
        logger.error("Turn processing error: %s", e, exc_info=True)
    finally:
        session["processing"] = False


async def _speech_to_text(audio_data: bytes) -> str:
    """Convert raw PCM audio to text using Sarvam AI STT."""
    sarvam_key = os.getenv("SARVAM_API_KEY", "")
    if not sarvam_key:
        logger.warning("SARVAM_API_KEY not set, STT unavailable")
        return ""

    # Convert raw PCM to WAV format (Sarvam expects WAV)
    wav_bytes = _pcm_to_wav(audio_data, SAMPLE_RATE, CHANNELS, SAMPLE_WIDTH)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                "https://api.sarvam.ai/speech-to-text",
                headers={"api-subscription-key": sarvam_key},
                files={"file": ("audio.wav", wav_bytes, "audio/wav")},
                data={
                    "language_code": "hi-IN",
                    "model": "saarika:v2",
                    "with_timestamps": "false",
                },
            )

            if response.status_code == 200:
                result = response.json()
                transcript = result.get("transcript", "")
                logger.info("STT result: '%s'", transcript)
                return transcript
            else:
                logger.error("STT API error %d: %s", response.status_code, response.text)
                return ""
    except Exception as e:
        logger.error("STT error: %s", e)
        return ""


async def _text_to_speech(text: str) -> bytes | None:
    """Convert text to raw PCM audio using Sarvam AI TTS."""
    sarvam_key = os.getenv("SARVAM_API_KEY", "")
    if not sarvam_key:
        logger.warning("SARVAM_API_KEY not set, TTS unavailable")
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
                    "speech_sample_rate": SAMPLE_RATE,
                    "enable_preprocessing": True,
                    "model": "bulbul:v2",
                },
            )

            if response.status_code == 200:
                audio_b64 = response.json()["audios"][0]
                wav_bytes = base64.b64decode(audio_b64)
                # Extract raw PCM from WAV (skip 44-byte WAV header)
                pcm_bytes = _wav_to_pcm(wav_bytes)
                return pcm_bytes
            else:
                logger.error("TTS API error %d: %s", response.status_code, response.text)
                return None
    except Exception as e:
        logger.error("TTS error: %s", e)
        return None


async def _generate_ai_response(session: dict[str, Any]) -> str:
    """Generate a conversational AI response using Gemini."""
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
            "(1-2 sentences max) since this is a live phone call and TTS must be fast. "
            "Be empathetic and professional. "
            "If someone mentions self-harm or violence, advise calling 112 immediately. "
            "Always clarify you provide general legal information, not legal advice."
        )

        history = []
        for turn in session.get("turns", [])[-8:]:
            label = "Caller" if turn["role"] == "user" else "Saarthi"
            history.append(f"{label}: {turn['content']}")

        prompt = "\n".join(history)

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
            return response.text.strip()

    except Exception as e:
        logger.error("Gemini error: %s", e)

    return "मैं आपकी बात समझ रहा हूँ। कृपया और विस्तार से बताएं।"


async def _stream_audio_to_exotel(
    websocket: WebSocket,
    session: dict[str, Any],
    stream_sid: str,
    pcm_data: bytes,
):
    """Stream PCM audio back to Exotel in base64-encoded chunks."""
    # Send in chunks of 1600 bytes (100ms at 8000Hz 16-bit mono)
    chunk_size = 1600
    timestamp_ms = 0

    for i in range(0, len(pcm_data), chunk_size):
        chunk = pcm_data[i : i + chunk_size]

        msg = {
            "event": "media",
            "sequence_number": session["seq_out"],
            "stream_sid": stream_sid,
            "streamSid": stream_sid,
            "media": {
                "chunk": session["chunk_out"],
                "timestamp": str(timestamp_ms),
                "payload": base64.b64encode(chunk).decode("utf-8"),
            },
        }

        try:
            await websocket.send_text(json.dumps(msg))
        except Exception as e:
            logger.debug("Failed to send audio chunk: %s", e)
            return

        session["seq_out"] += 1
        session["chunk_out"] += 1
        timestamp_ms += (len(chunk) * 1000) // (SAMPLE_RATE * SAMPLE_WIDTH)

        # 80ms sleep for ~100ms audio chunk provides stable audio buffer
        await asyncio.sleep(0.08)


def _calculate_rms(pcm_data: bytes) -> float:
    """Calculate RMS energy of PCM audio for silence detection."""
    if len(pcm_data) < 2:
        return 0.0

    # Unpack 16-bit little-endian samples
    n_samples = len(pcm_data) // 2
    try:
        samples = struct.unpack(f"<{n_samples}h", pcm_data[:n_samples * 2])
        if not samples:
            return 0.0
        sum_sq = sum(s * s for s in samples)
        return (sum_sq / n_samples) ** 0.5
    except struct.error:
        return 0.0


def _pcm_to_wav(pcm_data: bytes, sample_rate: int, channels: int, sample_width: int) -> bytes:
    """Wrap raw PCM data in a WAV container."""
    data_size = len(pcm_data)
    file_size = 36 + data_size

    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        file_size,
        b"WAVE",
        b"fmt ",
        16,  # chunk size
        1,  # PCM format
        channels,
        sample_rate,
        sample_rate * channels * sample_width,  # byte rate
        channels * sample_width,  # block align
        sample_width * 8,  # bits per sample
        b"data",
        data_size,
    )
    return header + pcm_data


def _wav_to_pcm(wav_data: bytes) -> bytes:
    """Extract raw PCM data from a WAV file (skip header)."""
    # Find the 'data' chunk
    idx = wav_data.find(b"data")
    if idx == -1:
        # No WAV header found, assume raw PCM
        return wav_data

    # Skip 'data' marker (4 bytes) + data size (4 bytes) = 8 bytes
    data_start = idx + 8
    return wav_data[data_start:]
