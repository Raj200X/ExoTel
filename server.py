"""
meta-NFS Live Voice Server (Supports Exotel Voicebot / Stream Applet & Passthru Webhook)

This server supports two Exotel integration patterns:
1. Voicebot / Stream Applet: WebSockets / HTTP for direct AI voicebot conversation (No call forwarding)
2. Passthru Applet: Native Exotel JSON response (< 500ms response time to prevent personal phone fallback)
"""

import os
import base64
from pathlib import Path
from typing import Dict

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pydantic import BaseModel

load_dotenv()

from metanfs.conversation.manager import ConversationManager
from metanfs.models.core import FastModelOutput, ScopeGateAction, Turn, TriageFact, Utterance
from metanfs.models.enums import Domain
from metanfs.routing.engine import RoutingEngine
from metanfs.scope_gate.engine import ScopeGateEngine

app = FastAPI(title="meta-NFS Voice Server", version="0.2.0")

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
    return {"status": "online", "system": "meta-NFS Voice Server", "version": "0.2.0"}


# ---------------------------------------------------------------------------
# Pattern A: Exotel Voicebot / Stream Applet (WebSockets / JSON Voicebot API)
# ---------------------------------------------------------------------------


@app.api_route("/exotel/voicebot", methods=["GET", "POST"])
async def exotel_voicebot(request: Request):
    """Voicebot Applet Endpoint — Returns Voicebot JSON configuration."""
    return JSONResponse(
        {
            "status": "success",
            "greeting": "नमस्ते! meta-NFS Legal Triage line में आपका स्वागत है। कृपया अपनी समस्या बताएं।",
            "voicebot_url": f"wss://{request.headers.get('host', 'localhost')}/exotel/ws",
        }
    )


@app.websocket("/exotel/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Real-time Voicebot WebSocket stream for Exotel Stream/Voicebot applet."""
    await websocket.accept()
    session = conversation_manager.create_session()
    print(f"WebSocket connected for AI session {session.session_id}")

    try:
        while True:
            data = await websocket.receive_text()
            utterance = Utterance(session_id=session.session_id, text=data)
            result = await conversation_manager.process_utterance(utterance)

            await websocket.send_json(
                {
                    "event": "media",
                    "text": result.response_text,
                    "action": result.scope_gate_decision.action,
                    "rule_id": result.scope_gate_decision.rule_id,
                }
            )
    except WebSocketDisconnect:
        print(f"WebSocket session {session.session_id} disconnected.")


# ---------------------------------------------------------------------------
# Pattern B: Exotel Passthru Applet (Fast JSON Response < 500ms)
# ---------------------------------------------------------------------------


@app.api_route("/exotel/incoming", methods=["GET", "POST"])
async def exotel_incoming(request: Request):
    """Exotel Passthru Webhook — Fast JSON Response to prevent forwarding to personal phone."""
    params = dict(request.query_params)
    call_sid = params.get("CallSid", "default_call")
    caller_phone = params.get("From", "Unknown")

    print(f"\n📞 INCOMING CALL from {caller_phone} (CallSid: {call_sid})")
    conversation_manager.create_session(call_sid)

    # Exotel Passthru Native JSON Format
    # Prevents fallback to personal phone number
    return JSONResponse(
        {
            "select": "play_and_gather",
            "body": "नमस्ते! meta-NFS Legal Triage line में आपका स्वागत है। कृपया अपनी समस्या बताएं।",
            "action_url": f"https://{request.headers.get('host', 'localhost')}/exotel/process",
        }
    )


@app.api_route("/exotel/process", methods=["GET", "POST"])
async def exotel_process(request: Request):
    """Exotel Passthru Turn Processor."""
    params = dict(request.query_params)
    call_sid = params.get("CallSid", "default_call")
    caller_speech = params.get("digits") or params.get("Speech", "") or params.get("digits_typed", "")

    if not caller_speech:
        caller_speech = "A customer's cheque for 50000 bounced and I haven't sent notice yet."

    utterance = Utterance(session_id=call_sid, text=caller_speech)
    result = await conversation_manager.process_utterance(utterance)

    ai_response = result.response_text

    return JSONResponse(
        {
            "select": "play",
            "body": ai_response,
        }
    )


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
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>meta-NFS — Localhost Voice Triage Test</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&display=swap" rel="stylesheet">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Outfit', sans-serif;
            background: #0f172a;
            color: #f8fafc;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            align-items: center;
            padding: 2rem;
        }
        .container {
            max-width: 800px;
            width: 100%;
            background: rgba(30, 41, 59, 0.7);
            backdrop-filter: blur(12px);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 20px;
            padding: 2rem;
            box-shadow: 0 20px 50px rgba(0, 0, 0, 0.5);
        }
        h1 {
            font-size: 2rem;
            font-weight: 700;
            background: linear-gradient(135deg, #38bdf8, #818cf8);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 0.5rem;
        }
        p.subtitle { color: #94a3b8; font-size: 0.95rem; margin-bottom: 1.5rem; }
        .chat-box {
            background: #090d16;
            border-radius: 12px;
            padding: 1rem;
            height: 350px;
            overflow-y: auto;
            border: 1px solid #1e293b;
            margin-bottom: 1rem;
            display: flex;
            flex-direction: column;
            gap: 1rem;
        }
        .message {
            padding: 0.8rem 1.2rem;
            border-radius: 14px;
            max-width: 85%;
            font-size: 0.95rem;
            line-height: 1.5;
        }
        .user-msg {
            background: #312e81;
            color: #e0e7ff;
            align-self: flex-end;
            border-bottom-right-radius: 2px;
        }
        .ai-msg {
            background: #1e293b;
            color: #f1f5f9;
            align-self: flex-start;
            border-bottom-left-radius: 2px;
            border: 1px solid #334155;
        }
        .badge {
            display: inline-block;
            font-size: 0.75rem;
            font-weight: 700;
            padding: 2px 8px;
            border-radius: 6px;
            margin-bottom: 4px;
            text-transform: uppercase;
        }
        .badge-proceed { background: #065f46; color: #34d399; }
        .badge-clarify { background: #854d0e; color: #fde047; }
        .badge-hardstop { background: #991b1b; color: #fca5a5; }
        .badge-softstop { background: #9a3412; color: #fdba74; }
        
        .input-row {
            display: flex;
            gap: 0.5rem;
        }
        input[type="text"] {
            flex: 1;
            background: #090d16;
            border: 1px solid #334155;
            padding: 0.8rem 1.2rem;
            border-radius: 12px;
            color: #fff;
            font-size: 1rem;
            outline: none;
        }
        input[type="text"]:focus { border-color: #6366f1; }
        button {
            background: linear-gradient(135deg, #4f46e5, #6366f1);
            color: white;
            border: none;
            padding: 0.8rem 1.5rem;
            border-radius: 12px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
        }
        button:hover { opacity: 0.9; transform: translateY(-1px); }
        audio { width: 100%; margin-top: 6px; height: 32px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>meta-NFS Localhost Test</h1>
        <p class="subtitle">Simulate phone calls live on localhost. Scope Gate + Sarvam AI Hindi Speech active.</p>

        <div class="chat-box" id="chatBox">
            <div class="message ai-msg">
                <span class="badge badge-proceed">System Ready</span><br>
                नमस्ते! meta-NFS Legal Triage line में आपका स्वागत है। कृपया अपनी समस्या बताएं।
            </div>
        </div>

        <div class="input-row">
            <input type="text" id="userInput" placeholder="Type your situation (e.g. A cheque for 50000 bounced...)" onkeypress="if(event.key==='Enter') sendMessage()">
            <button onclick="sendMessage()">Send & Listen</button>
        </div>
    </div>

    <script>
        async function sendMessage() {
            const input = document.getElementById('userInput');
            const text = input.value.trim();
            if (!text) return;

            const chatBox = document.getElementById('chatBox');
            chatBox.innerHTML += `<div class="message user-msg">${text}</div>`;
            input.value = '';
            chatBox.scrollTop = chatBox.scrollHeight;

            try {
                const res = await fetch('/api/test_turn', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ session_id: 'browser-test', text: text })
                });

                const data = await res.json();
                let badgeClass = 'badge-proceed';
                if (data.action === 'clarify') badgeClass = 'badge-clarify';
                if (data.action === 'hard_stop') badgeClass = 'badge-hardstop';
                if (data.action === 'soft_stop') badgeClass = 'badge-softstop';

                let audioHtml = '';
                if (data.audio_url) {
                    audioHtml = `<audio controls autoplay src="${data.audio_url}"></audio>`;
                }

                chatBox.innerHTML += `
                    <div class="message ai-msg">
                        <span class="badge ${badgeClass}">${data.action} | Rule: ${data.rule_id}</span><br>
                        ${data.response_text}
                        ${audioHtml}
                    </div>
                `;
                chatBox.scrollTop = chatBox.scrollHeight;
            } catch (err) {
                console.error(err);
            }
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
