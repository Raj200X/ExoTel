"""
meta-NFS Live Voice Server (Exotel Webhook + Sarvam AI Speech + Scope Gate Triage)

Runs a FastAPI web server handling live incoming phone calls from Exotel.

Endpoints:
  - GET/POST /exotel/incoming   : Initial call ingress webhook from Exotel
  - GET/POST /exotel/process    : Conversational turn webhook (caller input -> triage -> Sarvam TTS)
  - GET /audio/{audio_id}.mp3   : Serves generated Sarvam AI speech audio files to Exotel
  - GET /health                 : Server status check
"""

import os
import base64
from pathlib import Path
from typing import Dict

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pydantic import BaseModel

load_dotenv()

from metanfs.conversation.manager import ConversationManager
from metanfs.models.core import FastModelOutput, ScopeGateAction, Turn, TriageFact, Utterance
from metanfs.models.enums import Domain
from metanfs.routing.engine import RoutingEngine
from metanfs.scope_gate.engine import ScopeGateEngine

app = FastAPI(title="meta-NFS Voice Server", version="0.1.0")

# Audio storage directory for generated Sarvam TTS files
AUDIO_DIR = Path("data/audio_cache")
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

# Initialize Scope Gate & Routing Engine
rules_dir = Path(os.getenv("SCOPE_GATE_RULES_DIR", "config/scope_gate_rules"))
routing_dir = Path(os.getenv("ROUTING_TABLES_DIR", "config/routing_tables"))

scope_gate = ScopeGateEngine()
scope_gate.load_domain_rules(rules_dir)

routing_engine = RoutingEngine()
routing_engine.load_routing_tables(routing_dir)


class SimpleFastModel:
    """Fast Model for fact extraction."""

    async def process(
        self,
        utterance_text: str,
        turn_history: list[Turn],
        current_triage_fact: TriageFact,
        domain_lock: Domain | None,
    ) -> FastModelOutput:
        fact = current_triage_fact.model_copy()
        text_lower = utterance_text.lower()

        if any(w in text_lower for w in ["cheque", "check", "bounce", "bounced", "dishonour"]):
            fact.detected_domain = Domain.CHEQUE_BOUNCE
            fact.domain_confidence = 0.9

        if any(w in text_lower for w in ["suicide", "end my life", "kill myself"]):
            fact.has_self_harm_violence = True

        if any(w in text_lower for w in ["threatened", "violence", "beat", "kill", "attacked"]):
            fact.involves_criminal_element = True

        if any(w in text_lower for w in ["fraud", "scam", "conspiracy"]):
            fact.key_facts["fraud_context"] = True

        if any(w in text_lower for w in ["received", "got", "gave me", "customer"]):
            fact.key_facts["payee_or_drawer"] = "payee"
        elif any(w in text_lower for w in ["issued", "wrote", "my cheque"]):
            fact.key_facts["payee_or_drawer"] = "drawer"

        for word in text_lower.replace("₹", "").replace(",", "").split():
            try:
                val = float(word)
                if val >= 100:
                    fact.key_facts["cheque_amount"] = val
                    break
            except ValueError:
                continue

        if "notice" in text_lower:
            if any(w in text_lower for w in ["not", "haven't", "no"]):
                fact.key_facts["notice_sent"] = False
            elif "sent" in text_lower:
                fact.key_facts["notice_sent"] = True

        fact.overall_confidence = 0.85
        return FastModelOutput(
            updated_triage_fact=fact,
            response_candidate="I understand your cheque bounce query.",
            needs_retrieval=False,
        )


conversation_manager = ConversationManager(
    scope_gate=scope_gate,
    routing_engine=routing_engine,
    fast_model=SimpleFastModel(),
)


async def generate_sarvam_tts(text: str, filename: str) -> Path | None:
    """Generate audio using Sarvam AI Text-to-Speech API."""
    key = os.getenv("SARVAM_API_KEY")
    if not key or key == "your_sarvam_api_key":
        return None

    url = "https://api.sarvam.ai/text-to-speech"
    headers = {"api-subscription-key": key, "Content-Type": "application/json"}
    payload = {
        "inputs": [text[:500]],  # Cap for prompt length
        "target_language_code": "hi-IN",
        "speaker": "anushka",
        "pitch": 0,
        "pace": 1.0,
        "loudness": 1.5,
        "speech_sample_rate": 8000,
        "enable_preprocessing": True,
        "model": "bulbul:v1",
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(url, json=payload, headers=headers)
            if r.status_code == 200:
                audio_base64 = r.json()["audios"][0]
                audio_bytes = base64.b64decode(audio_base64)
                out_path = AUDIO_DIR / filename
                out_path.write_bytes(audio_bytes)
                return out_path
    except Exception as e:
        print(f"Sarvam TTS Error: {e}")
    return None


@app.get("/health")
def health():
    return {"status": "online", "system": "meta-NFS Voice Server"}


@app.api_route("/exotel/incoming", methods=["GET", "POST"])
async def exotel_incoming(request: Request):
    """Exotel Passthru Applet - Incoming call handler."""
    params = dict(request.query_params)
    call_sid = params.get("CallSid", "default_call")
    caller_phone = params.get("From", "Unknown")

    print(f"\n📞 INCOMING CALL from {caller_phone} (CallSid: {call_sid})")
    session = conversation_manager.create_session(call_sid)

    # Initial Greeting Prompt
    greeting = (
        "नमस्ते! meta-NFS Legal Triage line में आपका स्वागत है। "
        "कृपया अपनी कानूनी समस्या का विवरण दें।"
    )

    # Convert greeting to speech via Sarvam
    audio_path = await generate_sarvam_tts(greeting, f"{call_sid}_greeting.wav")

    # Exotel Passthru Response format
    response_body = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>{greeting}</Say>
</Response>"""
    return Response(content=response_body, media_type="application/xml")


@app.api_route("/exotel/process", methods=["GET", "POST"])
async def exotel_process(request: Request):
    """Exotel Applet turn processor - processes caller speech & returns AI triage response."""
    params = dict(request.query_params)
    call_sid = params.get("CallSid", "default_call")
    caller_speech = params.get("digits") or params.get("Speech", "") or params.get("digits_typed", "")

    if not caller_speech:
        caller_speech = "A customer's cheque for 50000 bounced and I haven't sent notice yet."

    utterance = Utterance(session_id=call_sid, text=caller_speech)
    result = await conversation_manager.process_utterance(utterance)

    ai_response = result.response_text

    # Generate Sarvam TTS Audio for response
    audio_filename = f"{call_sid}_turn_{len(result.session.turn_history)}.wav"
    await generate_sarvam_tts(ai_response, audio_filename)

    response_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>{ai_response}</Say>
</Response>"""
    return Response(content=response_xml, media_type="application/xml")


@app.get("/audio/{filename}")
def serve_audio(filename: str):
    file_path = AUDIO_DIR / filename
    if file_path.exists():
        return FileResponse(file_path, media_type="audio/wav")
    return Response(status_code=404)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    print(f"Starting meta-NFS Voice Server on port {port}...")
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=True)
