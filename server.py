"""
meta-NFS Live Voice Server (Exotel Passthru Applet + Sarvam AI Speech + Scope Gate)

Serves XML & JSON Passthru responses for Exotel telephony.
"""

import os
import base64
from pathlib import Path

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

app = FastAPI(title="meta-NFS Voice Server", version="0.3.0")

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
    """Fast Model for legal triage fact extraction."""

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
            response_candidate="I understand your legal situation.",
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
        "inputs": [text[:500]],
        "target_language_code": "hi-IN",
        "speaker": "anushka",
        "pitch": 0,
        "pace": 1.0,
        "loudness": 1.5,
        "speech_sample_rate": 8000,
        "enable_preprocessing": True,
        "model": "bulbul:v2",
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
    return {"status": "online", "system": "meta-NFS Voice Server", "version": "0.3.0"}


# ---------------------------------------------------------------------------
# Exotel Passthru Ingress (Returns Standard Exotel XML & Audio Commands)
# ---------------------------------------------------------------------------


@app.api_route("/exotel/incoming", methods=["GET", "POST"])
async def exotel_incoming(request: Request):
    """Exotel Passthru Webhook — Returns Exotel Response XML."""
    params = dict(request.query_params)
    call_sid = params.get("CallSid", "default_call")
    caller_phone = params.get("From", "Unknown")

    print(f"\n📞 INCOMING CALL from {caller_phone} (CallSid: {call_sid})")
    conversation_manager.create_session(call_sid)

    greeting = (
        "Namaste! Welcome to meta-NFS Legal Triage line. "
        "Please speak your cheque bounce legal issue."
    )

    # Standard Exotel Passthru XML Markup (Exotel speaks this greeting)
    response_body = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>{greeting}</Say>
</Response>"""
    return Response(content=response_body, media_type="application/xml")


@app.api_route("/exotel/process", methods=["GET", "POST"])
async def exotel_process(request: Request):
    """Exotel Turn Processor."""
    params = dict(request.query_params)
    call_sid = params.get("CallSid", "default_call")
    caller_speech = params.get("digits") or params.get("Speech", "") or params.get("digits_typed", "")

    if not caller_speech:
        caller_speech = "A customer cheque for 50000 bounced and I haven't sent notice yet."

    utterance = Utterance(session_id=call_sid, text=caller_speech)
    result = await conversation_manager.process_utterance(utterance)

    ai_response = result.response_text

    response_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>{ai_response}</Say>
</Response>"""
    return Response(content=response_xml, media_type="application/xml")


# ---------------------------------------------------------------------------
# Localhost Web UI & Audio Server
# ---------------------------------------------------------------------------


class TestTurnPayload(BaseModel):
    session_id: str = "localhost-session"
    text: str


@app.post("/api/test_turn")
async def api_test_turn(payload: TestTurnPayload):
    utterance = Utterance(session_id=payload.session_id, text=payload.text)
    result = await conversation_manager.process_utterance(utterance)

    audio_filename = f"{payload.session_id}_turn_{len(result.session.turn_history)}.wav"
    audio_path = await generate_sarvam_tts(result.response_text, audio_filename)

    audio_url = f"/audio/{audio_filename}" if audio_path else None

    return {
        "action": result.scope_gate_decision.action,
        "rule_id": result.scope_gate_decision.rule_id,
        "reason": result.scope_gate_decision.reason,
        "response_text": result.response_text,
        "audio_url": audio_url,
        "is_final": result.is_final,
        "triage_fact": result.session.triage_fact.model_dump(),
    }


@app.get("/audio/{filename}")
def serve_audio(filename: str):
    file_path = AUDIO_DIR / filename
    if file_path.exists():
        return FileResponse(file_path, media_type="audio/wav")
    return Response(status_code=404)


@app.get("/", response_class=HTMLResponse)
def index():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>meta-NFS — Localhost Voice Triage Test</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&display=swap" rel="stylesheet">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: 'Outfit', sans-serif; background: #0f172a; color: #f8fafc; padding: 2rem; }
        .container { max-width: 800px; margin: 0 auto; background: rgba(30, 41, 59, 0.7); border-radius: 20px; padding: 2rem; }
        h1 { color: #38bdf8; margin-bottom: 1rem; }
        .chat-box { background: #090d16; border-radius: 12px; padding: 1rem; height: 350px; overflow-y: auto; margin-bottom: 1rem; }
        .message { padding: 0.8rem 1.2rem; border-radius: 14px; margin-bottom: 0.5rem; max-width: 85%; }
        .user-msg { background: #312e81; margin-left: auto; }
        .ai-msg { background: #1e293b; border: 1px solid #334155; }
        input[type="text"] { width: 80%; padding: 0.8rem; background: #090d16; border: 1px solid #334155; color: #fff; border-radius: 8px; }
        button { padding: 0.8rem 1.5rem; background: #6366f1; color: white; border: none; border-radius: 8px; cursor: pointer; }
    </style>
</head>
<body>
    <div class="container">
        <h1>meta-NFS Localhost Test</h1>
        <div class="chat-box" id="chatBox">
            <div class="message ai-msg">Namaste! Welcome to meta-NFS Legal Triage line.</div>
        </div>
        <input type="text" id="userInput" placeholder="Type situation...">
        <button onclick="sendMessage()">Send</button>
    </div>
    <script>
        async function sendMessage() {
            const input = document.getElementById('userInput');
            const text = input.value.trim();
            if (!text) return;
            const chatBox = document.getElementById('chatBox');
            chatBox.innerHTML += `<div class="message user-msg">${text}</div>`;
            input.value = '';
            const res = await fetch('/api/test_turn', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ session_id: 'browser-test', text: text })
            });
            const data = await res.json();
            chatBox.innerHTML += `<div class="message ai-msg"><b>[${data.action}]</b> ${data.response_text}</div>`;
        }
    </script>
</body>
</html>
"""


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    print(f"Starting meta-NFS Voice Server on port {port}...")
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=True)
