# Kairon 🤖

**AI-powered signal briefing bot delivered via Telegram.**

Kairon fetches articles from multiple free news APIs, runs a local LangChain pipeline to extract signal intelligence, synthesizes a daily briefing using a cloud LLM, and delivers it to your Telegram — on a 12-hour schedule orchestrated by n8n.

> Built by [Siddharth Lal](https://linkedin.com/in/siddharth-lal-606128200) · [Pentaspark](https://github.com/Siddharth-lal-13)

---

## What it does

Every 12 hours, n8n triggers the LangGraph multi-agent pipeline:

```
NewsAPI + GNews + HN Scraping + Ars Scraping → LangGraph Pipeline (qwen3.5:4b) → NVIDIA NIM (meta/llama-3.3-70b-instruct) → Telegram
  [Data Sources]   [Multi-Agent Routing]         [Analyze]           [Synthesize]        [Deliver]
```

You receive a clean, analyst-style briefing on topics you've chosen: **AI**, **Automation**, **Startups**, **Tech**.

The Telegram bot is two-way — you can update your topic preferences any time without touching config files.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  n8n (Docker, port 5678)                                    │
│  12-hour ScheduleTrigger → POST /trigger                    │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│  FastAPI (Uvicorn, port 8000)                               │
│  /trigger  — pipeline entrypoint (background task)          │
│  /bot      — Telegram webhook receiver                      │
│  /health   — liveness probe                                 │
└──────┬───────────────────────────────────────┬──────────────┘
       │                                       │
┌──────▼──────────┐                 ┌──────────▼──────────────┐
│  Agent Pipeline │                 │  Telegram Bot (PTB v21) │
│                 │                 │                         │
│  1. Fetcher     │                 │  /start  /topics        │
│     NewsAPI     │                 │  /set    /status        │
│     GNews       │                 │  /help                  │
│                 │                 └─────────────────────────┘
│  2. Analyzer    │
│     LangChain   │
│     LCEL chain  │
│     qwen2.5:3b  │◄── Ollama (local, host machine)
│     via Ollama  │
│                 │
│  3. Synthesizer │
│     Llama-3.1   │◄── NVIDIA NIM free API
│     70B via NIM │
└──────┬──────────┘
       │
┌──────▼──────────┐
│  Storage        │
│  preferences    │  flat JSON (Stage 1)
│  delivery_log   │  flat JSON (Stage 1)
└─────────────────┘
```

### Tech stack

| Component | Technology | Notes |
|-----------|-----------|-------|
| Orchestration | n8n (self-hosted Docker) | 12-hour cron, importable `workflow.json` |
| News sources | NewsAPI + GNews | Free tiers stacked for volume |
| Multi-Agent Routing | LangGraph 0.2+ | Supervisor pattern with retry routing |
| Web Scraping | crawl4ai + BeautifulSoup | Hacker News + Ars Technica scrapers |
| Analysis LLM | qwen3.5:4b via Ollama | Local inference, 4GB VRAM safe |
| Synthesis LLM | meta/llama-3.3-70b-instruct via NVIDIA NIM | Free API tier |
| Bot delivery | python-telegram-bot v21 | Two-way interaction |
| API layer | FastAPI + Uvicorn | Webhook bridge for n8n + Telegram |
| Data models | Pydantic v2 | End-to-end type safety |
| Storage | JSON flat files (Stage 1) | PostgreSQL + MemPalace in Stage 2 |
| Containerisation | Docker + Docker Compose | Single `docker compose up` |

---

## Prerequisites

- **Python 3.11+**
- **Docker Desktop** (for n8n + Kairon API)
- **Ollama** installed on your host machine with `qwen2.5:3b` pulled

```bash
ollama pull qwen2.5:3b
```

- Free API keys for: [NewsAPI](https://newsapi.org/register), [GNews](https://gnews.io/), [NVIDIA NIM](https://build.nvidia.com/)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

---

## Quick start

```bash
# 1. Clone
git clone https://github.com/Siddharth-lal-13/kairon.git
cd kairon

# 2. Set up environment
cp .env.example .env
# Edit .env and fill in all API keys

# 3. Create storage files
mkdir -p storage
echo '{}' > storage/preferences.json
echo '[]' > storage/delivery_log.json
echo '[]' > storage/feedback_log.json

# 4. Start services
docker compose up -d

# 5. Import the n8n workflow (optional - can trigger manually too)
# Open http://localhost:5678, log in (admin / changeme),
# go to Workflows → Import → select n8n/workflow.json
# Activate the workflow.

# 6. Test the bot
# Open Telegram, find your bot, send /start
```

## 📱 Live Demo

**Try the bot on Telegram:** Find your bot and send `/start`

### Portfolio Demonstration Strategy

**Option 1: Full Automation (Recommended)**
- Start Docker with `docker compose up -d`
- Import n8n workflow for automated 12-hour delivery
- Demonstrates: scheduling, automation, production readiness

**Option 2: Manual Triggering (For Development)**
- Start API only: `docker compose up -d kairon-api`
- Trigger briefings manually with curl commands
- Demonstrates: API architecture, debugging capabilities

**Both modes work together** - this flexibility shows understanding of different deployment paradigms and production requirements.

### Development mode (no Docker)

```bash
# Install dependencies
pip install -r requirements.txt

# Run the API server
uvicorn api.webhook:app --reload --port 8000

# In a separate terminal, run the bot in polling mode
python -m bot.telegram_bot

# Trigger the pipeline manually (skip n8n)
curl -X POST http://localhost:8000/trigger \
  -H "Content-Type: application/json" \
  -H "X-N8n-Secret: your_n8n_trigger_secret" \
  -d '{"run_id": "test-001", "triggered_at": "2025-01-01T07:00:00Z", "target_user_id": YOUR_CHAT_ID}'
```

---

## Telegram commands

| Command | Description |
|---------|-------------|
| `/start` | Register and receive your first briefing setup |
| `/topics` | Show your current topic subscriptions |
| `/set ai automation` | Update subscriptions (space-separated) |
| `/status` | Full preference summary |
| `/help` | Command reference |

Available topics: `ai` `automation` `startups` `tech`

### Two-way interaction

The Telegram bot includes inline buttons for user feedback on each article in your briefing:

- **👍 Upvote**: Mark articles as high-signal (affects future personalization)
- **👎 Downvote**: Mark articles as low-signal (affects future personalization)
- **🔍 Deep Dive**: Request detailed analysis of specific articles

Feedback is stored in `storage/feedback_log.json` and used to personalize future briefings by:
- Weighting coverage toward upvoted signal types and entities
- Minimizing coverage of downvoted signal types
- Improving relevance scoring for articles matching your preferences

---

## Project structure

```
kairon/
├── agents/
│   ├── fetcher.py          # NewsAPI + GNews fetcher (async, deduped)
│   ├── scraper.py          # Hacker News + Ars Technica scraper (crawl4ai)
│   ├── analyzer.py         # LangChain LCEL chain → qwen3.5:4b/Ollama
│   ├── pipeline.py          # LangGraph supervisor pattern (multi-agent routing)
│   └── synthesizer.py      # NVIDIA NIM synthesis agent
├── bot/
│   └── telegram_bot.py     # python-telegram-bot v21 (two-way)
├── api/
│   └── webhook.py          # FastAPI — /trigger + /bot + /health
├── models/
│   └── schemas.py          # Pydantic v2 data models
├── storage/
│   ├── store.py            # Flat-file storage layer (Stage 1)
│   ├── preferences.json    # User topic preferences (git-ignored)
│   ├── delivery_log.json   # Delivery audit trail (git-ignored)
│   └── feedback_log.json   # User feedback for personalization (git-ignored)
├── n8n/
│   └── workflow.json       # Importable n8n workflow
├── .env.example
├── .gitignore
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── README.md
```

---

## Configuration

All configuration is via environment variables. See `.env.example` for the full reference.

Key variables:

| Variable | Description |
|----------|-------------|
| `NEWSAPI_KEY` | NewsAPI free tier key |
| `GNEWS_KEY` | GNews free tier key |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `NIM_API_KEY` | NVIDIA NIM free API key |
| `OLLAMA_MODEL` | Local model name (default: `qwen2.5:3b`) |
| `MAX_CONCURRENT_LLM` | Parallel Ollama calls (default: `2`, safe for 4GB VRAM) |
| `RELEVANCE_THRESHOLD` | Articles below this score are dropped (default: `0.4`) |
| `N8N_TRIGGER_SECRET` | Shared secret between n8n and FastAPI |

---

## Data Sources

Kairon uses a hybrid approach for maximum signal coverage:

### API-Based Sources (Primary)
- **NewsAPI**: Aggregates articles from thousands of sources
- **GNews**: Tech-focused news aggregation

### Web Scraping (Supplemental)
- **Hacker News**: Front page stories with engagement metrics (points, comment count)
- **Ars Technica**: Deep tech journalism and analysis

### Why Both?
APIs provide structured metadata and reliability. Scrapers provide fresh content and engagement signals. The system merges both sources, deduplicates by URL, and delivers the best of both worlds.

---

## n8n workflow

The `n8n/workflow.json` can be imported directly into any n8n instance.

**What it does:**
1. Fires every 12 hours via ScheduleTrigger
2. Builds a trigger payload with a unique `run_id`
3. POSTs to `http://kairon-api:8000/trigger` with `X-N8n-Secret` header
4. Checks the response and logs success or failure

**To import:**
n8n UI → Workflows → ··· → Import from file → select `n8n/workflow.json`

**Environment variables n8n needs** (set in docker-compose.yml):
- `KAIRON_API_URL` — `http://kairon-api:8000`
- `N8N_TRIGGER_SECRET` — must match the FastAPI env var

## 📊 Portfolio Showcase

### System Capabilities Demonstrated

**Multi-Agent Architecture:**
- ✅ **Supervisor Pattern**: LangGraph routing with intelligent retry logic
- ✅ **Independent Agent Decisions**: Each node makes data-quality assessments
- ✅ **Self-Healing Pipeline**: Recovers from transient failures automatically
- ✅ **Production-Ready Design**: Error handling, logging, type safety throughout

**Data Source Integration:**
- ✅ **Four Sources**: NewsAPI, GNews, Hacker News, Ars Technica
- ✅ **Concurrent Fetching**: Parallel API calls and web scraping
- ✅ **Intelligent Merging**: Deduplication by URL with source tracking
- ✅ **Quality Filtering**: Relevance scoring with LLM analysis

**User Experience & Personalization:**
- ✅ **Two-Way Interaction**: Telegram bot with inline feedback buttons
- ✅ **Smart Learning**: System learns from user upvotes/downvotes
- ✅ **Preference Management**: Users can update topics anytime
- ✅ **Feedback System**: Records interactions for future personalization

**Production Infrastructure:**
- ✅ **Containerized Deployment**: Docker Compose with networking
- ✅ **FastAPI Layer**: Webhook bridge for n8n + Telegram
- ✅ **Automation Integration**: n8n workflow for 12-hour scheduling
- ✅ **Multi-Modal Operation**: Supports automated and manual triggering
- ✅ **Monitoring**: Health checks, logging, error tracking

### Deployment Modes Demonstrated

**1. Automated Production Mode (Docker + n8n):**
- Full 12-hour automated delivery
- n8n workflow orchestration
- Self-healing with retry logic
- Production-ready error handling

**2. Manual Triggering Mode (Docker + API):**
- Flexible manual triggering via API
- Perfect for development and testing
- Demonstrates clean API architecture

**3. Development Mode (Local):**
- Direct bot execution for debugging
- Individual agent testing
- Rapid development cycle

### Technical Skills Demonstrated

**Backend Engineering:**
- Async/await patterns for concurrent operations
- LangGraph state machine implementation
- Pydantic v2 data models and validation
- FastAPI with background tasks
- Thread-safe file operations with filelocks

**AI/ML Integration:**
- Local LLM integration (Ollama with qwen3.5:4b)
- Cloud LLM integration (NVIDIA NIM with Llama-3.1-70b)
- LangChain LCEL chains for structured output
- JSON parsing and error handling
- Model selection and parameter tuning

**System Architecture:**
- Multi-agent supervisor pattern
- State management and data flow
- Error handling and retry logic
- API integration and rate limiting
- Web scraping with crawl4ai

**DevOps & Deployment:**
- Docker containerization
- Docker Compose orchestration
- Service networking and communication
- Environment variable management
- Health checks and monitoring
- n8n workflow integration

The `n8n/workflow.json` can be imported directly into any n8n instance.

**What it does:**
1. Fires every 12 hours via ScheduleTrigger
2. Builds a trigger payload with a unique `run_id`
3. POSTs to `http://kairon-api:8000/trigger` with the `X-N8n-Secret` header
4. Checks the response and logs success or failure

**To import:**  
n8n UI → Workflows → ··· → Import from file → select `n8n/workflow.json`

**Environment variables n8n needs** (set in docker-compose.yml):
- `KAIRON_API_URL` — `http://kairon-api:8000`
- `N8N_TRIGGER_SECRET` — must match the FastAPI env var

---

## Stage 2 roadmap

Stage 2 (Pentaspark commercial product) will add:

- **MemPalace** persistent memory — per-user wing/room structure for cross-session personalization (feedback system already implemented in Stage 1)
- **PostgreSQL** replacing flat JSON storage
- **Multi-user subscription system** with Telegram payment API
- **Real-time alerts** for high-signal breaking news
- **Deep trend analysis** across sessions
- **Additional signal sources** (RSS, job boards, GitHub trending)

Stage 2 hooks are already present in the codebase: `mempalace_wing` fields in `UserPreferences` and `Briefing`, commented-out deps in `requirements.txt`.

---

## License

Creative Commons Attribution-NonCommercial 4.0 International (CC BY-NC 4.0).

You may use, share, and adapt this project for non-commercial purposes with attribution.  
Commercial use requires written permission from the author.

See [LICENSE](LICENSE) for full terms.

---

## Author

**Siddharth Lal** — Python backend and automation engineer  
[GitHub](https://github.com/Siddharth-lal-13) · [LinkedIn](https://linkedin.com/in/siddharth-lal-606128200)
