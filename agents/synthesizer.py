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

import asyncio
import hashlib
import logging
import os
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
from openai import OpenAI, RateLimitError, APIConnectionError, APIStatusError

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

# NIM API rate limiting and retry configuration
NIM_TIMEOUT = 120  # 2 minutes timeout for AI thinking time
NIM_RATE_LIMIT_INTERVAL = 30.0  # Minimum seconds between NIM calls
NIM_MAX_RETRIES = 3
NIM_INITIAL_RETRY_DELAY = 5.0  # Initial delay in seconds
NIM_MAX_RETRY_DELAY = 30.0  # Maximum delay in seconds


# ---------------------------------------------------------------------------
# NIM Rate Limiter
# ---------------------------------------------------------------------------

class NIMRateLimiter:
    """Rate limiter for NIM API calls with intelligent delays."""

    def __init__(self, min_interval: float = NIM_RATE_LIMIT_INTERVAL):
        self._lock = asyncio.Lock()
        self._last_request_time = 0.0
        self._min_interval = min_interval

    async def wait_if_needed(self) -> None:
        """Wait if we need to respect rate limits between NIM calls."""
        async with self._lock:
            now = time.time()
            elapsed = now - self._last_request_time
            if elapsed < self._min_interval:
                wait_time = self._min_interval - elapsed
                logger.info("NIM rate limiter: waiting %.1fs before next request", wait_time)
                await asyncio.sleep(wait_time)
            self._last_request_time = time.time()


_nim_rate_limiter = NIMRateLimiter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _briefing_id(user_id: int, date: datetime) -> str:
    raw = f"{user_id}:{date.date().isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def _to_html(text: str) -> str:
    """
    Escape HTML special characters while preserving <b> tags for bold formatting.

    HTML parse mode only requires escaping 3 characters: <, >, &
    This function protects <b> tags, escapes the special characters, then restores bold tags.

    Args:
        text: Raw text from LLM that should use <b> tags for bold

    Returns:
        HTML-safe text with <b> tags preserved
    """
    # First, protect <b> and </b> tags with temporary placeholders
    protected = text.replace("<b>", "\x00BOLD_OPEN\x00").replace("</b>", "\x00BOLD_CLOSE\x00")

    # Escape the 3 HTML special characters
    escaped = protected.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # Restore bold tags
    final = escaped.replace("\x00BOLD_OPEN\x00", "<b>").replace("\x00BOLD_CLOSE\x00", "</b>")

    return final


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

    # Build dynamic topic header instructions based on user's selected topics
    topic_headers = " ".join(f"<b>{topic.value.upper()}</b>" for topic in topics)

    return f"""You are Kairon, a sharp AI signal analyst delivering a daily tech briefing via Telegram.

Write a concise daily briefing based on the signals below.

RULES:
- Start with: <b>🤖 Kairon Daily Briefing</b> (date in YYYY-MM-DD)
- Use bold for section headers: {topic_headers}
- For each section, write 2–3 tight sentences covering the key signals — no bullet lists
- End with a one-sentence "The Takeaway" that ties the day's themes together
- Total output: 300–500 words max
- CRITICAL: Use <b>text</b> for bold. Do NOT use asterisks, underscores, or any markdown syntax. Plain text only except for <b> tags.
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
    Synthesize a Telegram-ready daily briefing for one user with enhanced error handling.

    Args:
        user_id:  Telegram chat_id of the recipient.
        topics:   Topics the user is subscribed to (controls section order).
        articles: Analyzed and relevance-filtered articles from the analyzer.
        feedback_summary: Optional user preferences from past feedback.

    Returns:
        A Briefing object with telegram_text ready for delivery.

    Raises:
        RuntimeError: If NIM_API_KEY is not set or all API call retries fail.
    """
    if not NIM_API_KEY:
        raise RuntimeError(
            "NIM_API_KEY is not set. Add it to your .env file. "
            "Get a free key at https://build.nvidia.com/"
        )

    if not articles:
        # Graceful empty briefing — no API call needed
        now = datetime.now(timezone.utc)
        empty_text = "<b>🤖 Kairon Daily Briefing</b>\n\nNo significant signals found today. Check back tomorrow!"
        return Briefing(
            briefing_id=_briefing_id(user_id, now),
            user_id=user_id,
            generated_at=now,
            topics_covered=topics,
            article_count=0,
            telegram_text=_to_html(empty_text),
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
        timeout=NIM_TIMEOUT,
    )

    # Enhanced retry logic with exponential backoff
    last_error = None
    for attempt in range(1, NIM_MAX_RETRIES + 1):
        try:
            # Apply rate limiting before each NIM call
            logger.info("NIM API attempt %d/%d for user %d", attempt, NIM_MAX_RETRIES, user_id)

            # Wait for rate limiter (async function needs to be called from async context)
            # For synchronous function, we'll use time.sleep instead
            wait_needed = _nim_rate_limiter._min_interval - (time.time() - _nim_rate_limiter._last_request_time)
            if wait_needed > 0:
                logger.info("NIM rate limiter: waiting %.1fs before request", wait_needed)
                time.sleep(wait_needed)

            response = client.chat.completions.create(
                model=NIM_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=NIM_MAX_TOKENS,
                temperature=0.65,   # slightly higher than analyzer for narrative variety
                timeout=NIM_TIMEOUT,
            )
            raw_text = response.choices[0].message.content.strip()
            # Convert to HTML-safe text while preserving <b> tags
            telegram_text = _to_html(raw_text)

            # Update last request time on success
            _nim_rate_limiter._last_request_time = time.time()

            logger.info("✅ NIM API success on attempt %d for user %d", attempt, user_id)
            break

        except RateLimitError as exc:
            last_error = exc
            wait_time = min(NIM_INITIAL_RETRY_DELAY * (2 ** (attempt - 1)), NIM_MAX_RETRY_DELAY)
            logger.warning(
                "NIM rate limit hit for user %d (attempt %d/%d), retrying in %.1fs",
                user_id, attempt, NIM_MAX_RETRIES, wait_time
            )
            if attempt < NIM_MAX_RETRIES:
                time.sleep(wait_time)

        except APIConnectionError as exc:
            last_error = exc
            wait_time = min(NIM_INITIAL_RETRY_DELAY * (2 ** (attempt - 1)), NIM_MAX_RETRY_DELAY)
            logger.warning(
                "NIM connection failed for user %d (attempt %d/%d), retrying in %.1fs: %s",
                user_id, attempt, NIM_MAX_RETRIES, wait_time, exc
            )
            if attempt < NIM_MAX_RETRIES:
                time.sleep(wait_time)

        except APIStatusError as exc:
            last_error = exc
            # For 5xx errors, retry. For 4xx errors (except 429), don't retry
            if exc.status_code >= 500 or exc.status_code == 429:
                wait_time = min(NIM_INITIAL_RETRY_DELAY * (2 ** (attempt - 1)), NIM_MAX_RETRY_DELAY)
                logger.warning(
                    "NIM API error %d for user %d (attempt %d/%d), retrying in %.1fs",
                    exc.status_code, user_id, attempt, NIM_MAX_RETRIES, wait_time
                )
                if attempt < NIM_MAX_RETRIES:
                    time.sleep(wait_time)
            else:
                logger.error("NIM API client error %d for user %d (no retry): %s", exc.status_code, user_id, exc)
                raise RuntimeError(f"NIM API client error {exc.status_code}: {exc.message}") from exc

        except Exception as exc:
            last_error = exc
            logger.error("NIM synthesis failed for user %d (attempt %d/%d): %s", user_id, attempt, NIM_MAX_RETRIES, exc)
            if attempt == NIM_MAX_RETRIES:
                raise RuntimeError(f"Synthesis failed after {NIM_MAX_RETRIES} attempts: {exc}") from exc
            time.sleep(NIM_INITIAL_RETRY_DELAY * (2 ** (attempt - 1)))
    else:
        # This runs if the loop completes without breaking (all retries failed)
        error_msg = f"NIM API failed after {NIM_MAX_RETRIES} attempts"
        if isinstance(last_error, RateLimitError):
            error_msg = "NIM API rate limit exceeded - please try again later"
        elif isinstance(last_error, APIConnectionError):
            error_msg = "Failed to connect to NVIDIA NIM API - please check your connection"
        elif isinstance(last_error, APIStatusError):
            error_msg = f"NIM API returned status {last_error.status_code}"
        else:
            error_msg = f"Synthesis failed: {last_error}"

        logger.error("NIM synthesis ultimately failed for user %d: %s", user_id, error_msg)
        raise RuntimeError(error_msg) from last_error

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
