"""
Kairon — Synthesizer Agent

Takes a list of AnalyzedArticle objects and produces a single Briefing
using NVIDIA NIM's free API (meta/llama-3.1-70b-instruct via OpenAI-compat SDK).

Why NIM here instead of Ollama?
  The synthesizer task requires narrative coherence across many articles —
  writing a briefing that reads like a human analyst wrote it, not a list of
  summaries pasted together.  Llama-3.1-70B at NIM quality is significantly
  better at this than qwen3.5:4b running locally.  NIM's free tier gives
  enough quota for 2 briefings/day without hitting limits.

Design decisions:
  - OpenAI-compatible client pointed at NIM endpoint (api.nvidiacloud.net)
  - Articles are grouped by topic before being sent to the LLM so the
    briefing has a natural section structure.
  - Output is Telegram MarkdownV2-safe: we avoid all special chars that
    MarkdownV2 requires escaping (*_[]()~`>#+-=|{}.!) outside intentional
    formatting.  The LLM is instructed to use only bold (**) and plain text.
  - Max output: ~1500 chars to stay within Telegram's 4096-char limit even
    for users subscribed to all four topics.
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone
from itertools import groupby

from dotenv import load_dotenv
from openai import OpenAI

# Load environment variables from .env file
load_dotenv()

from models.schemas import AnalyzedArticle, Briefing, Topic

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

NIM_API_KEY: str = os.getenv("NIM_API_KEY", "")
NIM_BASE_URL: str = os.getenv(
    "NIM_BASE_URL", "https://integrate.api.nvidia.com/v1"
)
NIM_MODEL: str = os.getenv("NIM_MODEL")
NIM_MAX_TOKENS: int = int(os.getenv("NIM_MAX_TOKENS", "900"))

# How many articles per topic to include in the briefing prompt.
# Keeps the prompt lean for the 70B model on NIM free tier.
MAX_ARTICLES_PER_TOPIC = 4


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _briefing_id(user_id: int, date: datetime) -> str:
    raw = f"{user_id}:{date.date().isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def _escape_mdv2(text: str) -> str:
    """
    Escape Telegram MarkdownV2 special characters in plain text spans.
    Only apply this to text that should NOT be formatted.
    """
    special = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in text)


def _build_prompt(
    user_id: int,
    topics: list[Topic],
    articles_by_topic: dict[Topic, list[AnalyzedArticle]],
    feedback_summary: dict | None = None,   # NEW optional param
) -> str:
    """Construct the synthesis prompt from grouped analyzed articles."""
    sections: list[str] = []

    for topic in topics:
        topic_articles = articles_by_topic.get(topic, [])[:MAX_ARTICLES_PER_TOPIC]
        if not topic_articles:
            continue

        section_lines = [f"## {topic.value.upper()} SIGNALS"]
        for i, art in enumerate(topic_articles, 1):
            section_lines.append(
                f"{i}. [{art.source_name}] {art.title}\n"
                f"   Summary: {art.one_line_summary}\n"
                f"   Why it matters: {art.why_it_matters}\n"
                f"   Signal type: {art.signal_type.value}\n"
                f"   Entities: {', '.join(art.key_entities)}\n"
                f"   URL: {art.url}"
            )
        sections.append("\n".join(section_lines))

    articles_block = "\n\n".join(sections)

    # Build personalization block conditionally from feedback_summary
    personalization_block = ""
    if feedback_summary and any(feedback_summary.values()):
        upvoted_topics = ", ".join(feedback_summary.get("upvoted_topics", []))
        upvoted_entities = ", ".join(feedback_summary.get("upvoted_entities", []))
        downvoted_topics = ", ".join(feedback_summary.get("downvoted_topics", []))
        personalization_block = f"""
USER PREFERENCES (from past feedback):
Upvoted signal types: {upvoted_topics}
Entities this user engages with: {upvoted_entities}
Downvoted signal types: {downvoted_topics}

Weight your coverage toward user's upvoted preferences.
Minimize coverage of downvoted signal types unless the story
is exceptionally significant.
"""

    return f"""You are Kairon, a sharp AI signal analyst delivering a daily tech briefing via Telegram.

Write a concise daily briefing based on the signals below.

RULES:
- Start with: *🤖 Kairon Daily Briefing*  (date in YYYY-MM-DD)
- Use bold for section headers: *AI* *AUTOMATION* *STARTUPS* *TECH*
- For each section, write 2–3 tight sentences covering the key signals — no bullet lists
- End with a one-sentence "The Takeaway" that ties the day's themes together
- Total output: 300–500 words max
- Telegram formatting only: bold (*text*) and plain text. No markdown headers (##), no lists, no links in text
- Keep a human, analyst tone — confident, not hype
{personalization_block}
SIGNALS FOR TODAY:

{articles_block}

Write the briefing now:"""


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def synthesize_briefing(
    user_id: int,
    topics: list[Topic],
    articles: list[AnalyzedArticle],
    feedback_summary: dict | None = None,   # NEW optional param
) -> Briefing:
    """
    Synthesize a Telegram-ready daily briefing for one user.

    Args:
        user_id:  Telegram chat_id of the recipient.
        topics:   Topics the user is subscribed to (controls section order).
        articles: Analyzed and relevance-filtered articles from the analyzer.

    Returns:
        A Briefing object with telegram_text ready for delivery.

    Raises:
        RuntimeError: If NIM_API_KEY is not set or the API call fails.
    """
    if not NIM_API_KEY:
        raise RuntimeError(
            "NIM_API_KEY is not set. Add it to your .env file. "
            "Get a free key at https://build.nvidia.com/"
        )

    if not articles:
        # Graceful empty briefing — no API call needed
        now = datetime.now(timezone.utc)
        return Briefing(
            briefing_id=_briefing_id(user_id, now),
            user_id=user_id,
            generated_at=now,
            topics_covered=topics,
            article_count=0,
            telegram_text=(
                "*🤖 Kairon Daily Briefing*\n\n"
                "No significant signals found today\\. Check back tomorrow\\!"
            ),
        )

    # Group articles by their primary topic (first in topics_matched list)
    articles_by_topic: dict[Topic, list[AnalyzedArticle]] = {t: [] for t in topics}
    for art in articles:
        for t in art.topics:
            if t in articles_by_topic:
                articles_by_topic[t].append(art)
                break  # assign to first matching topic only

    prompt = _build_prompt(user_id, topics, articles_by_topic, feedback_summary)

    client = OpenAI(
        base_url=NIM_BASE_URL,
        api_key=NIM_API_KEY,
    )

    try:
        response = client.chat.completions.create(
            model=NIM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=NIM_MAX_TOKENS,
            temperature=0.65,   # slightly higher than analyzer for narrative variety
        )
        telegram_text = response.choices[0].message.content.strip()
    except Exception as exc:
        logger.error("NIM synthesis failed for user %d: %s", user_id, exc)
        raise RuntimeError(f"Synthesis failed: {exc}") from exc

    now = datetime.now(timezone.utc)
    briefing = Briefing(
        briefing_id=_briefing_id(user_id, now),
        user_id=user_id,
        generated_at=now,
        topics_covered=[t for t in topics if articles_by_topic.get(t)],
        article_count=len(articles),
        telegram_text=telegram_text,
    )

    logger.info(
        "Synthesizer: briefing %s generated for user %d (%d articles, %d chars)",
        briefing.briefing_id,
        user_id,
        briefing.article_count,
        len(briefing.telegram_text),
    )
    return briefing


# ---------------------------------------------------------------------------
# CLI smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import asyncio
    import logging as _logging

    from agents.analyzer import analyze_articles
    from agents.fetcher import fetch_articles

    _logging.basicConfig(level=logging.INFO)

    async def _get_articles():
        raw = await fetch_articles([Topic.AI, Topic.TECH])
        return await analyze_articles(raw[:3])

    articles = asyncio.run(_get_articles())
    briefing = synthesize_briefing(
        user_id=999999,
        topics=[Topic.AI, Topic.TECH],
        articles=articles,
    )
    print(briefing.telegram_text)
