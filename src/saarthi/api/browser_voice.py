"""Browser Voice Call WebSocket endpoint for Saarthi.

Allows users to talk to the AI directly from their web browser (Web Audio API)
without needing any telephony provider like Exotel or Twilio.
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

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from google import genai
from google.genai import types

from saarthi.core import state

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Browser Voice Call"])

SAMPLE_RATE = 16000  # Browser typically captures at 16kHz easily
SILENCE_THRESHOLD = 300
SILENCE_DURATION_MS = 1400

@router.websocket("/ws/browser")
async def browser_websocket(websocket: WebSocket):
    """Handle direct browser microphone streaming."""
    await websocket.accept()

    session: dict[str, Any] = {
        "audio_buffer": bytearray(),
        "turns": [],
        "silence_chunks": 0,
        "is_speaking": False,
        "processing": False,
        "last_rms": 0,
    }

    logger.info("🌐 Browser Voice WebSocket connected")

    # Send initial greeting
    asyncio.create_task(_send_greeting(websocket, session))

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            event = msg.get("event")

            if event == "media":
                if session["processing"]:
                    continue

                payload = msg.get("media", {}).get("payload", "")
                if payload:
                    pcm_bytes = base64.b64decode(payload)
                    session["audio_buffer"].extend(pcm_bytes)

                    rms = _calculate_rms(pcm_bytes)
                    session["last_rms"] = rms
                    
                    if rms < SILENCE_THRESHOLD:
                        session["silence_chunks"] += 1
                    else:
                        session["silence_chunks"] = 0
                        session["is_speaking"] = True

                    # Each chunk of 4096 samples at 16000Hz is exactly 256ms
                    silence_ms = session["silence_chunks"] * 256
                    buffer_duration_ms = (len(session["audio_buffer"]) / 2 / SAMPLE_RATE) * 1000

                    if not session["processing"]:
                        should_process = False
                        
                        # Trigger if speaking stopped for 1.4s, AND we have at least 1s of audio
                        if session["is_speaking"] and silence_ms >= SILENCE_DURATION_MS and buffer_duration_ms >= 1000:
                            should_process = True
                        # OR force trigger if buffer reaches 8 seconds to prevent indefinite waiting
                        elif buffer_duration_ms >= 8000:
                            should_process = True
                            
                        if should_process:
                            session["processing"] = True
                            session["is_speaking"] = False
                            
                            audio_data = bytes(session["audio_buffer"])
                            session["audio_buffer"] = bytearray()
                            session["silence_chunks"] = 0
                            
                            # Notify UI that AI is thinking
                            try:
                                await websocket.send_text(json.dumps({"event": "status", "status": "processing"}))
                            except Exception:
                                pass

                            asyncio.create_task(
                                _process_turn(websocket, session, audio_data)
                            )

            elif event == "stop":
                break

    except WebSocketDisconnect:
        logger.info("🌐 Browser WebSocket disconnected")
    except Exception as e:
        logger.error("Browser WebSocket error: %s", e)


async def _send_greeting(websocket: WebSocket, session: dict[str, Any]):
    text = "नमस्ते! साथी कानूनी सहायता लाइन में आपका स्वागत है। कृपया अपनी समस्या बताएं।"
    logger.info("🗣️ Sending browser greeting...")
    try:
        pcm = await _tts(text)
        if pcm:
            session["turns"].append({"role": "assistant", "content": text, "time": datetime.utcnow().isoformat()})
            await _stream_to_browser(websocket, pcm)
    except Exception as e:
        logger.error("Browser greeting error: %s", e)


async def _process_turn(websocket: WebSocket, session: dict[str, Any], audio_data: bytes):
    try:
        user_text = await _stt(audio_data)
        if not user_text:
            logger.info("STT returned empty.")
            # Tell UI to resume listening
            try:
                await websocket.send_text(json.dumps({"event": "status", "status": "listening"}))
            except Exception:
                pass
            return

        session["turns"].append({"role": "user", "content": user_text, "time": datetime.utcnow().isoformat()})
        try:
            await websocket.send_text(json.dumps({"event": "transcript", "role": "user", "text": user_text}))
        except Exception:
            pass
            
        ai_response = await _generate_gemini_response(session)
        session["turns"].append({"role": "assistant", "content": ai_response, "time": datetime.utcnow().isoformat()})
        try:
            await websocket.send_text(json.dumps({"event": "transcript", "role": "assistant", "text": ai_response}))
        except Exception:
            pass

        pcm = await _tts(ai_response)
        if pcm:
            await _stream_to_browser(websocket, pcm)

    except Exception as e:
        logger.error("Browser processing error: %s", e)
        try:
            await websocket.send_text(json.dumps({"event": "status", "status": "listening"}))
        except Exception:
            pass
    finally:
        session["processing"] = False


async def _stream_to_browser(websocket: WebSocket, pcm_data: bytes):
    chunk_size = 3200  # 100ms at 16kHz
    for i in range(0, len(pcm_data), chunk_size):
        chunk = pcm_data[i : i + chunk_size]
        msg = {
            "event": "media",
            "media": {"payload": base64.b64encode(chunk).decode("utf-8")}
        }
        try:
            await websocket.send_text(json.dumps(msg))
        except Exception:
            return
        await asyncio.sleep(0.09)


async def _tts(text: str) -> bytes | None:
    sarvam_key = os.getenv("SARVAM_API_KEY", "")
    if not sarvam_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            res = await client.post(
                "https://api.sarvam.ai/text-to-speech",
                headers={"api-subscription-key": sarvam_key},
                json={
                    "inputs": [text[:500]],
                    "target_language_code": "hi-IN",
                    "speaker": "anushka",
                    "speech_sample_rate": SAMPLE_RATE,
                    "model": "bulbul:v2",
                }
            )
            if res.status_code == 200:
                b64 = res.json()["audios"][0]
                wav = base64.b64decode(b64)
                idx = wav.find(b"data")
                return wav[idx + 8 :] if idx != -1 else wav
    except Exception as e:
        logger.error("TTS error: %s", e)
    return None


async def _stt(pcm: bytes) -> str:
    sarvam_key = os.getenv("SARVAM_API_KEY", "")
    if not sarvam_key:
        return ""
    data_size = len(pcm)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_size, b"WAVE", b"fmt ", 16, 1,
        1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16,
        b"data", data_size,
    )
    wav = header + pcm
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            res = await client.post(
                "https://api.sarvam.ai/speech-to-text",
                headers={"api-subscription-key": sarvam_key},
                files={"file": ("audio.wav", wav, "audio/wav")},
                data={"language_code": "hi-IN", "model": "saarika:v2.5", "with_timestamps": "false"}
            )
            if res.status_code == 200:
                return res.json().get("transcript", "")
            else:
                logger.error("STT API 400 Error: %s", res.text)
    except Exception as e:
        logger.error("STT error: %s", e)
    return ""


async def _generate_gemini_response(session: dict[str, Any]) -> str:
    api_key = state.config.gemini_api_key if state.config else os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return "AI offline."
    try:
        client = genai.Client(api_key=api_key)
        history = "\n".join(f"{t['role']}: {t['content']}" for t in session.get("turns", [])[-8:])
        res = await asyncio.to_thread(
            client.models.generate_content,
            model="gemini-1.5-flash",
            contents=history,
            config=types.GenerateContentConfig(
                system_instruction="You are Saarthi, a Hindi legal assistant. Respond in 1 sentence only in Devanagari.",
                temperature=0.4,
            ),
        )
        return res.text.strip() if res and res.text else ""
    except Exception:
        return "मैं समझ नहीं पा रही हूँ।"


def _calculate_rms(pcm: bytes) -> float:
    n = len(pcm) // 2
    if n == 0: return 0.0
    try:
        samples = struct.unpack(f"<{n}h", pcm[:n * 2])
        return (sum(s * s for s in samples) / n) ** 0.5
    except struct.error:
        return 0.0
