# Kairon — Complete System Walkthrough

## Table of Contents
1. [Quick Start](#quick-start)
2. [System Architecture](#system-architecture)
3. [Getting Started](#getting-started)
4. [Running the System](#running-the-system)
5. [User Workflow](#user-workflow)
6. [Development Guide](#development-guide)
7. [Troubleshooting](#troubleshooting)

## Quick Start

**Three ways to run Kairon:**

### Method 1: Full Automation (Production) 🚀
```bash
# 1. Start all services with n8n automation
docker compose up -d

# 2. Import n8n workflow (one-time setup)
# Open http://localhost:5678 → Workflows → Import → n8n/workflow.json
# Activate workflow with 12-hour schedule

# 3. Test bot in Telegram
# Send /start to your bot
# Briefing will arrive automatically at your delivery time
```

### Method 2: Manual Triggering (Development) 💻
```bash
# 1. Start services without n8n
docker compose up -d kairon-api

# 2. Trigger manual briefings
# PowerShell version:
$secret = "your_n8n_trigger_secret"
$headers = @{
    "Content-Type" = "application/json"
    "X-N8n-Secret" = $secret
}
$body = @{
    run_id = "demo_001"
    triggered_at = (Get-Date).ToUniversalTime().ToString("s")
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri "http://localhost:8000/trigger" `
    -Headers $headers -Body $body

# 3. Briefing arrives in ~2-3 minutes
```

### Method 3: Development Mode (Local) 🛠️
```bash
# 1. Start API server only
python -m api.webhook

# 2. Run bot in polling mode (separate terminal)
python -m bot.telegram_bot

# 3. Trigger pipeline manually
# Use curl commands above
```

## System Architecture

### Data Flow
```
Data Sources → Pipeline Analysis → User Personalization → Delivery
     ↓              ↓                    ↓                  ↓
  APIs + Scrapers → Multi-Agent System → Feedback System → Telegram Bot
     ↓              ↓                    ↓                  ↓
 31 Articles/Run  LangGraph Routing  User Preferences  Clean Briefings
     ↓              ↓                    ↓                  ↓
Deduplication  Supervisor Pattern  Topic Weighting 12-Hour Schedule
```

### Multi-Agent Pipeline (LangGraph)

**3-Agent Supervisor Pattern:**

1. **Fetch Node**
   - Fetches from NewsAPI, GNews concurrently
   - Scrapes Hacker News, Ars Technica concurrently
   - Merges and deduplicates by URL
   - Returns 31+ unique articles

2. **Analyze Node**
   - Analyzes articles with local LLM (qwen3.5:4b)
   - Extracts signal metadata (type, entities, relevance)
   - Calculates coverage score for retry decisions
   - Filters low-relevance articles

3. **Synthesize Node**
   - Gets user feedback for personalization
   - Generates briefing with cloud LLM (NVIDIA NIM)
   - Applies user preferences from past interactions
   - Returns Telegram-formatted briefing

### Supervisor Routing Logic

```
┌─────────────────────────────────────────────────────────────┐
│                    fetch_node                             │
│              /              │                          │
│         / retry? < 5     │                          │
│        / yes                │                          │
│       /                    │                          │
│    retry_count++             │                          │
│       │                    │                          │
│      ↓                   │                          │
│                    analyze_node                          │
│              /              │                          │
│         / retry? < 0.3    │                          │
│        / yes                │                          │
│       /                    │                          │
│    retry_count++             │                          │
│       │                    │                          │
│      ↓                   │                          │
│                synthesize_node                       │
└─────────────────────────────────────────────────────────────┘
```

## Getting Started

### Prerequisites Checklist

- [x] **Python 3.11+** installed
- [x] **Docker Desktop** installed
- [x] **Ollama** running with `qwen3.5:4b` model
- [ ] **Free API Keys**:
  - [ ] NewsAPI key from https://newsapi.org/register
  - [ ] GNews key from https://gnews.io/
  - [ ] NVIDIA NIM key from https://build.nvidia.com/
- [ ] **Telegram Bot Token** from @BotFather
- [ ] **Storage files created** (preferences.json, delivery_log.json, feedback_log.json)

### Environment Setup

1. **Clone Repository**
```bash
git clone https://github.com/Siddharth-lal-13/kairon.git
cd kairon
```

2. **Configure Environment**
```bash
# Copy example
cp .env.example .env

# Edit .env with your keys
notepad .env  # On Windows
nano .env      # On Linux/Mac
```

Required variables in `.env`:
```bash
# News APIs
NEWSAPI_KEY=your_newsapi_key
GNEWS_KEY=your_gnews_key

# Telegram
TELEGRAM_BOT_TOKEN=your_bot_token_from_botfather

# AI Services
NIM_API_KEY=your_nim_api_key
NIM_BASE_URL=https://integrate.api.nvidia.com/v1
NIM_MODEL=meta/llama-3.3-70b-instruct
NIM_MAX_TOKENS=900

# Local LLM
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen3.5:4b
MAX_CONCURRENT_LLM=2
RELEVANCE_THRESHOLD=0.4

# n8n Secret
N8N_TRIGGER_SECRET=your_secret_here
```

3. **Create Storage Files**
```bash
mkdir -p storage

# Create initial files
echo '{}' > storage/preferences.json
echo '[]' > storage/delivery_log.json
echo '[]' > storage/feedback_log.json
```

## Running the System

### Full Automation Setup

```bash
# Start everything with n8n automation
docker compose up -d

# Check all services are running
docker ps

# Should show:
# kairon-api (running)
# kairon-n8n (running)
```

### Import n8n Workflow

1. **Open n8n Dashboard**
```
http://localhost:5678
```

2. **Login** (admin / changeme by default)

3. **Import Workflow**
```
Workflows → ··· → Import from file → n8n/workflow.json
```

4. **Activate Workflow**
```
Click "Activate" on the imported workflow
```

5. **Set Schedule**
```
Edit the "Every 12 Hours" node to customize timing
```

### Verify System Health

```bash
# Test API health
curl http://localhost:8000/health

# Expected response:
{
  "status": "ok",
  "timestamp": "2026-04-15T12:00:00Z"
}

# Test bot registration
# In Telegram, send /start to your bot

# Check containers
docker ps

# Check logs
docker logs kairon-api --tail 20
docker logs kairon-n8n --tail 20
```

## User Workflow

### First-Time User Experience

```
1. Send /start to bot
   ↓
2. Bot welcomes and saves preferences
   ↓
3. At delivery time (e.g., 7 AM), system triggers:
   - Fetches articles from 4 sources
   - Analyzes with local LLM
   - Synthesizes personalized briefing
   - Sends to Telegram with inline buttons
   ↓
4. User receives briefing with:
   - 👍 Upvote (for high-signal items)
   - 👎 Downvote (for low-signal items)
   - 🔍 Deep Dive (for detailed analysis)
   ↓
5. System learns from feedback
   - Future briefings are personalized
   - Topics, entities are weighted accordingly
```

### Topic Management

```bash
# Check current topics
/topics

# Update topics (space-separated)
/set ai tech automation

# See full status
/status

# Expected response:
Your Kairon Preferences

Topics: ai tech
Delivery: 07:00 UTC
Active: ✅
```

### Feedback Interaction

```bash
# After receiving briefing:
1. Click 👍 on articles you like
2. Click 👎 on articles you dislike
3. Click 🔍 for more details

# Feedback is saved to storage/feedback_log.json
# System learns your preferences over time
# Future briefings are automatically personalized
```

## Development Guide

### Local Development Setup

```bash
# 1. Create virtual environment
python -m venv venv
source venv/bin/activate  # Linux/Mac
venv\Scripts\activate     # Windows

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run API server
uvicorn api.webhook:app --reload --port 8000

# 4. Test API (separate terminal)
curl http://localhost:8000/health
```

### Running Bot in Development Mode

```bash
# In another terminal (still in venv)
python -m bot.telegram_bot

# Bot starts in polling mode
# Check Telegram for updates every 3 seconds
# Useful for testing without n8n
```

### Manual Pipeline Testing

```bash
# Test individual agents
python -m agents.scraper      # Test scraping only
python -m agents.pipeline      # Test full pipeline

# Test with specific topics
curl -X POST http://localhost:8000/trigger \
  -H "Content-Type: application/json" \
  -H "X-N8n-Secret: test_secret" \
  -d '{
    "run_id": "test_001",
    "triggered_at": "2026-04-15T12:00:00Z",
    "target_user_id": YOUR_CHAT_ID
  }'
```

### Debugging Pipeline Issues

```bash
# 1. Check API health
curl http://localhost:8000/health

# 2. Check logs
docker logs kairon-api --follow

# 3. Check individual components
python -m agents.pipeline      # Run pipeline test
python -m agents.analyzer      # Test analyzer
python -m agents.synthesizer    # Test synthesizer

# 4. Check data sources
# NewsAPI: Check if key is valid
# GNews: Check if key is valid
# Scrapers: Check if sites are accessible

# 5. Check LLM connections
# Ollama: ollama list
# NIM: curl https://integrate.api.nvidia.com/v1/models
```

## Troubleshooting

### Common Issues

**Bot Not Responding**
```bash
# 1. Check if bot is running
docker ps | grep kairon

# 2. Check if bot token is correct
grep TELEGRAM_BOT_TOKEN .env

# 3. Test bot manually
python -m bot.telegram_bot

# 4. Check logs
docker logs kairon-api --tail 50
```

**No Briefing Arriving**
```bash
# 1. Check if n8n workflow is active
# Open http://localhost:5678
# Check if workflow is "Active"

# 2. Check n8n execution history
# Click on the workflow in n8n UI
# Look at "Executions" tab

# 3. Test API directly
curl http://localhost:8000/health

# 4. Trigger manually
curl -X POST http://localhost:8000/trigger \
  -H "Content-Type: application/json" \
  -H "X-N8n-Secret: test_secret" \
  -d '{"run_id":"debug_001","triggered_at":"2026-04-15T12:00:00Z"}'
```

**Articles Not Being Analyzed**
```bash
# 1. Check if Ollama is running
ollama list

# 2. Test Ollama connection
curl http://localhost:11434/api/tags

# 3. Check model availability
ollama show qwen3.5:4b

# 4. Check analyzer logs
docker logs kairon-api | grep -i "analyzer\|LLM"
```

**Briefing Quality Issues**
```bash
# 1. Check NIM API key validity
# Make sure NIM_API_KEY is valid

# 2. Test NIM connection
curl https://integrate.api.nvidia.com/v1/models

# 3. Check model in use
grep NIM_MODEL .env

# 4. Adjust prompt if needed
# Edit agents/synthesizer.py
# Modify prompt instructions
```

### System Performance

**Pipeline Performance**
```bash
# Typical performance (31 articles):
- Scraping: ~4 seconds
- Analysis: ~30-60 seconds (2 parallel LLM calls)
- Synthesis: ~5-10 seconds (NIM API)
- Total: ~40-75 seconds per briefing

# Monitor logs for timing
docker logs kairon-api | grep -i "completed\|duration"
```

**Resource Usage**
```bash
# Ollama memory: ~4GB VRAM required
# Parallel LLM calls: Controlled by MAX_CONCURRENT_LLM
# API rate limits: Free tiers have daily limits
# NIM free tier: ~2 briefings/day

# Monitor Docker resources
docker stats kairon-api
```

### Advanced Configuration

**Adjusting Article Count**
```bash
# Edit .env
MAX_CONCURRENT_LLM=4        # Increase parallelism
RELEVANCE_THRESHOLD=0.3      # Lower threshold = more articles
```

**Customizing Scheduling**
```bash
# Edit n8n/workflow.json
# Change "hoursInterval": 12 to desired hours
# Example: 6 for twice daily, 24 for once daily
```

**Adding New Topics**
```bash
# Send in Telegram:
/set ai tech automation startups

# Topics available: ai, automation, startups, tech
```

## Next Steps

### For Users

1. **Set up your topics** based on your interests
2. **Provide feedback** using inline buttons
3. **Refine preferences** over time
4. **Enjoy personalized briefings**!

### For Developers

1. **Explore the codebase**:
   - `agents/` - Multi-agent pipeline
   - `bot/` - Telegram bot integration
   - `api/` - FastAPI webhook layer
   - `storage/` - Data persistence layer
   - `models/` - Pydantic schemas

2. **Extend functionality**:
   - Add new data sources
   - Improve personalization algorithms
   - Add new Telegram commands
   - Enhance error handling

3. **Contribute**:
   - Report issues on GitHub
   - Submit pull requests
   - Improve documentation
   - Share your enhancements

---

## Quick Reference

**Essential Commands:**
```bash
# Start services
docker compose up -d

# Stop services
docker compose down

# Check logs
docker logs kairon-api --follow

# Test API
curl http://localhost:8000/health

# Import n8n workflow
# Open http://localhost:5678 → Workflows → Import
```

**Telegram Commands:**
```
/start    - Register and setup
/topics   - Check subscriptions
/set <topics> - Update topics
/status   - Full status
/help     - Command reference
```

**File Locations:**
```bash
# Configuration
.env                    # Environment variables
docker-compose.yml        # Docker services
n8n/workflow.json        # n8n automation

# Storage
storage/preferences.json  # User settings
storage/delivery_log.json # Delivery history
storage/feedback_log.json # User feedback

# Code
agents/               # Multi-agent pipeline
bot/                  # Telegram bot
api/                  # FastAPI layer
models/               # Data schemas
storage/              # Persistence layer
```

**Getting Help:**
- README.md - Complete documentation
- GitHub Issues - Report problems
- Discord - Community support (if available)
```

---

**Ready to explore Kairon!** 🚀

Start with Method 1 (Full Automation) for the complete experience, or Method 2 (Manual Triggering) for development and testing.
