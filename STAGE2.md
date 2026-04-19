# Kairon — Stage 2 Roadmap

Stage 2 is the commercial evolution of Kairon into a multi-user, adaptive intelligence platform under **Pentaspark**. This document captures the full planned architecture so nothing gets lost between builds.

> Stage 1 hooks are already in the codebase: `mempalace_wing` fields in `UserPreferences` and `Briefing`, commented-out deps in `requirements.txt`, and `feedback_log.json` collecting interaction data from day one.

---

## 1. True Multi-Agent Architecture (LangGraph Upgrade)

The current pipeline is a **supervisor-routed sequential flow**. Stage 2 upgrades it to a genuinely agentic system where routing decisions are made by an LLM planner, not hardcoded conditionals.

### 1a. Planner Agent
Replace the conditional edge functions (`should_retry_fetch`, `should_retry_analyze`) with an LLM-backed planner that receives a pipeline state summary and returns a structured routing decision.

```
PlannerDecision {
  action: "proceed_analyze" | "refetch_targeted" | "proceed_synthesize"
  reasoning: str
  targeted_topics: list[str]
  enrichment_needed: bool
}
```

The planner calls Ollama directly via `httpx` (no LangChain overhead) and returns a `PlannerDecision`. On parse failure it defaults to `proceed_*` to keep the pipeline safe.

**After fetch:** decides whether article count and quality are sufficient, or whether to re-fetch with tighter topic targeting.  
**After analyze:** evaluates coverage score and signal distribution, decides whether to synthesize or re-fetch.

### 1b. Time-Gating Layer
Before analysis, every article gets enriched with:
- `age_hours` — computed from `published_at`
- `source_credibility` — float based on known outlet lists (1.0 / 0.5 / 0.3)
- `verification_tier` — `"breaking"` / `"light"` / `"full"` based on age + credibility

```
age < 6h              → "breaking" (no external verification, flagged in briefing)
6-24h, cred >= 1.0    → "light" (HN + RSS only)
6-24h, cred < 1.0     → "light" (lower trust)
> 24h                 → "full" (all enrichment tools)
```

### 1c. Enrichment Agent
Runs concurrently after analysis for eligible articles. Four async tools, each with a 10-second timeout:

| Tool | Trigger | Source | Credibility Boost |
|------|---------|--------|-------------------|
| `search_hackernews_rss` | all tiers except breaking | `hnrss.org` (≥10 points) | +0.2 |
| `search_github_rss` | product_launch / research | GitHub Atom commits | +0.4 |
| `search_arxiv_rss` | research only | arXiv query API | +0.5 |
| `search_investor_rss` | funding only | TechCrunch VC / Crunchbase / StrictlyVC | +0.3 |

Confirmed sources boost the article's `relevance_score`, lifting borderline articles over the threshold. The synthesizer marks each article with its verification status: `✓ Confirmed`, `~ Light`, or `⚡ Breaking — unverified`.

---

## 2. MemPalace — Persistent Per-User Signal Memory

The current feedback system writes to `feedback_log.json` and applies simple topic-weight adjustments. MemPalace replaces this with a proper per-user memory structure.

### Storage schema (per user wing)
```json
{
  "user_id": 1234567890,
  "wing_id": "wing_user_1234567890",
  "signal_weights": {
    "funding": 1.0,
    "product_launch": 1.0,
    "research": 1.0,
    "acquisition": 1.0,
    "trend": 1.0,
    "opinion": 1.0,
    "regulation": 1.0,
    "other": 1.0
  },
  "entity_memory": {},
  "platform_memory": {},
  "last_updated": "ISO datetime"
}
```

### Feedback mechanics
- **👍 Upvote:** `signal_weights[signal_type] *= 1.15` (cap: 2.0), increment entity engagement
- **👎 Downvote:** `signal_weights[signal_type] *= 0.85` (floor: 0.3)

### Article re-scoring before synthesis
1. Multiply each article's `relevance_score` by its signal type's weight
2. Add 0.05 per matched entity from `entity_memory` (cap: +0.2 total)
3. Clamp to `[0.0, 1.0]`, re-sort descending
4. Pass `personalization_context` (preferred signals, avoided signals, key entities) to the synthesizer prompt

### Synthesizer prompt addition
```
USER MEMORY CONTEXT (learned from past interactions):
Preferred signal types: [funding, research]
Signals to minimize: [opinion]
Entities this user tracks: [OpenAI, Anthropic, Mistral]
Weight your section coverage and depth accordingly.
```

### Stage 2b: PostgreSQL migration
Replace `storage/mempalace.json` with a proper Postgres schema:
- `user_wings` table
- `signal_weights` table (indexed by user_id + signal_type)
- `entity_memory` table
- `briefing_history` table (for trend analysis across sessions)

---

## 3. Multi-User Subscription System

Stage 1 is single-user (your own Telegram ID hardcoded or set in env). Stage 2 opens this to multiple subscribers.

### Telegram Payment API integration
- `/subscribe` — initiates payment flow via Telegram Stars or Stripe via bot
- `/unsubscribe` — cancels, stops deliveries
- Free tier: 1 briefing/day, 2 topics max
- Paid tier: 12-hour delivery, all topics, MemPalace personalization, Deep Dive requests

### User isolation
Each user gets their own `wing_id` in MemPalace. Briefing delivery is parallelised across active subscribers. Rate limiting per user on the `/trigger` path.

---

## 4. Real-Time Breaking News Alerts

In addition to scheduled briefings, a separate lightweight watcher process monitors high-signal RSS feeds and fires an alert to subscribed users when a qualifying story breaks.

Threshold criteria:
- Source credibility >= 1.0
- HN points >= 50 within 2 hours of publication
- Signal type in user's preferred list

Delivered as a short Telegram message with a single-tap Deep Dive button.

---

## 5. Deep Trend Analysis

Currently each briefing is stateless — it has no memory of previous cycles. Stage 2 adds cross-session trend detection using `briefing_history`.

Planned features:
- Entity frequency tracking across briefings (detect rising entities before they peak)
- Signal type velocity (funding activity accelerating in a sector)
- Weekly digest: summary of the week's top signals ranked by recurrence
- Anomaly detection: flag when a usually quiet signal type spikes

---

## 6. Additional Signal Sources

| Source | Signal value | Implementation |
|--------|-------------|----------------|
| GitHub Trending | Product launches, OSS momentum | RSS at `github.com/trending` |
| arXiv daily | Research signals | Already planned in enrichment tools |
| Job boards (LinkedIn, Greenhouse) | Company hiring signals (proxy for growth/funding) | Playwright scraper |
| Product Hunt | Launch signals | RSS feed |
| SEC EDGAR | Funding, M&A filings | EDGAR API |
| Substack newsletters | Analyst opinion signals | RSS per publication |

---

## 7. Reflexion Loop (Analyzer Quality Upgrade)

Replace the single-pass analysis with a critique-and-revise pattern:

1. Analyzer extracts signal from article (current behaviour)
2. Critic pass: same model reviews its own extraction — "did I identify the most important signal?"
3. If critique score < threshold, re-analyze with the critique as additional context
4. Max 2 revision rounds to avoid runaway costs

This is a LangGraph loop within the analyze node, not a new node. The outer pipeline structure stays the same.

---

## 8. Adaptive RAG for MemPalace

Replace flat signal weight lookups with a vector retrieval layer:

- Each past briefing is embedded and stored in a vector DB (pgvector or Qdrant)
- At synthesis time, retrieve the 3 most similar past briefings by topic embedding
- Use retrieved context to inform emphasis decisions in the synthesis prompt
- Gradually replaces the static `entity_memory` dict with semantic similarity

---

## Implementation order (when ready)

```
1. PostgreSQL schema + migration from JSON
2. MemPalace agent (weight mechanics, entity memory)
3. Planner agent (replaces conditional edges)
4. Time-gating layer (enriches RawArticle before analysis)
5. Enrichment tools (4 async tools, dispatcher)
6. Analyzer enrichment pass (boost scores from confirmed sources)
7. Synthesizer verification markers
8. Multi-user subscription system
9. Breaking news watcher
10. Reflexion loop
11. Trend analysis
12. Adaptive RAG
```

---

## Notes on commercial positioning

Kairon's differentiation is not just delivery (Telegram bots are common) but the **signal extraction and personalization layer**. The MemPalace weight mechanics and enrichment verification are the moat — they make the briefing genuinely better over time rather than just noisier.

Pricing model to validate: freemium via Telegram Stars, paid tier via Stripe, potential B2B angle (VC firms, startup operators) for a curated funding + research signal feed.


## rough list of functionalities:

1. News briefings 
2. Sentiment analysis
3. Wider topics of news
Attached sources of articles or videos
4. Ai makes decisions on what to show you based on your preference 
5. News/trending topics deep searching ability
6. Cross referencing of news articles to avoid biasness 
7. Typical news update once a day, can be requested on different frequencies