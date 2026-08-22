# Saarthi

**AI-powered phone guidance service for common people who may not know how to use ChatGPT or other AI tools.**

> Pick up your normal phone → Call Saarthi → Talk naturally in Hindi, Hinglish, or English → Get guidance

No app. No prompts. No technical knowledge. Just call and talk.

---

## How It Works

```
User's Normal Mobile Phone
          ↓
Calls Real Phone Number
          ↓
Vapi AI Voice Agent
          ↓
Live Two-Way Voice Conversation
          ↓
Call Data → Saarthi Dashboard
```

1. A user calls the Saarthi phone number using their normal mobile phone.
2. The call is answered by an AI voice agent (powered by Vapi).
3. The user speaks naturally — the AI understands and provides guidance.
4. After the call, the dashboard receives call data, transcript, and analysis.

## Dashboard Features

- **Real-time monitoring** — see active calls, total calls, unique callers
- **Call history** — browse all calls with search, filter by date/topic/risk/status
- **Call details** — view full transcript, AI analysis, summary, action items
- **Risk flagging** — high-risk calls are clearly flagged (medical emergency, self-harm, etc.)
- **Caller profiles** — track individual caller history across multiple calls
- **Topic classification** — automatic categorization of call topics

---

## Quick Start

### Prerequisites

- Python 3.12+
- pip

### Setup

```bash
# Clone
git clone https://github.com/developeranil65/Saarthi.git
cd Saarthi

# Create virtual environment and install
python -m venv .venv
source .venv/bin/activate      # Linux/Mac
# .venv\Scripts\activate       # Windows
pip install -e .
```

### Configure

```bash
# Copy environment template
cp .env.example .env

# Edit .env and set:
# - GEMINI_API_KEY (for call analysis)
# - VAPI_API_KEY (for webhook validation)
# - LIFELINE_PHONE_NUMBER (the real phone number)
```

### Run

```bash
python -m saarthi.main
```

Open `http://localhost:8000` in your browser to see the dashboard.

### Vapi Webhook

Configure your Vapi assistant's webhook URL to:

```
https://your-server-url/api/vapi/webhook
```

This receives call lifecycle events (call started, ended, transcript, analysis).

---

## Architecture

```
Vapi Voice Agent (external)
      ↓
Call Events / Call Data
      ↓
POST /api/vapi/webhook
      ↓
Saarthi Backend (FastAPI)
      ↓
├── Store caller (PostgreSQL)
├── Store call record
├── Store conversation messages
├── Analyze transcript (Gemini)
└── Display in Dashboard
```

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.12+ |
| Web Framework | FastAPI |
| Database | PostgreSQL (asyncpg) |
| AI Analysis | Google Gemini |
| Voice Agent | Vapi (external) |
| Dashboard | Vanilla HTML/CSS/JS |

---

## Project Structure

```
Saarthi/
├── src/saarthi/
│   ├── main.py             # FastAPI entrypoint (middleware, lifespan, app init)
│   ├── api/                # API Routes
│   │   ├── routes.py       # Dashboard REST endpoints
│   │   └── webhooks.py     # Vapi webhook handlers
│   ├── core/               # Core configurations
│   │   ├── config.py       # Environment configuration
│   │   ├── database.py     # PostgreSQL persistence layer
│   │   └── state.py        # Global app state singletons
│   ├── services/           # External integrations
│   │   └── call_analyzer.py# Gemini-based transcript analysis
│   ├── prompts/
│   │   └── analysis.py     # Call analysis prompt
│   ├── models/
│   │   ├── enums.py        # CallStatus, CallTopic, RiskLevel, MessageRole
│   │   └── core.py         # User, Call, ConversationMessage, CallAnalysis
│   └── static/
│       └── index.html      # Dashboard SPA
├── .env                    # Environment configuration
├── pyproject.toml          # Project dependencies
└── README.md
```

---

## License

This project is proprietary. All rights reserved.
