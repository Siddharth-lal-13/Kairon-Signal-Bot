"""
Kairon — Fetcher Agent

Pulls articles from two free-tier news APIs (NewsAPI + GNews) in parallel,
deduplicates by URL, and returns a list of RawArticle objects.

Free-tier constraints:
  NewsAPI  — 100 requests/day, results capped at 100 articles per query
  GNews    — 100 requests/day, 10 articles per request on free tier

Design decisions:
  - Both APIs are queried for every active topic so we maximise volume.
  - Dedup is URL-based (after normalisation) and article_id uses a short
    content hash so collisions across APIs are caught even with different URLs.
  - HTTP calls are made concurrently via asyncio + httpx.
  - Any single-API failure is logged and skipped; the other API's results
    are still returned, so one bad key never kills an entire run.
  - Supports user-specific topic fetching from storage/preferences.json
  - Increased article limits for comprehensive coverage with powerful LLMs
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
import trafilatura

from models.schemas import RawArticle, Topic

load_dotenv()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

NEWSAPI_KEY: str = os.getenv("NEWSAPI_KEY", "")
GNEWS_KEY: str = os.getenv("GNEWS_KEY", "")

# Rate limiting for GNews to prevent 429 errors
import time
import random

class RateLimitedGNews:
    """Serializes GNews requests with a minimum 62-second gap (free tier: 1 req/min)."""
    def __init__(self):
        self._lock = asyncio.Lock()
        self._last_request_time: float = 0.0
        self._min_interval: float = 62.0  # 1 req/min + 2s buffer

    async def wait_if_needed(self):
        async with self._lock:
            now = asyncio.get_event_loop().time()
            elapsed = now - self._last_request_time
            if elapsed < self._min_interval:
                wait = self._min_interval - elapsed
                logger.info("GNews rate limiter: waiting %.1fs before next request", wait)
                await asyncio.sleep(wait)
            self._last_request_time = asyncio.get_event_loop().time()

gnews_rate_limiter = RateLimitedGNews()

NEWSAPI_BASE = "https://newsapi.org/v2/everything"
GNEWS_BASE = "https://gnews.io/api/v4/search"

# Keywords used when querying each topic.
# Multiple terms per topic increase recall without burning extra API calls
# (both APIs accept OR queries / comma-separated keywords).
TOPIC_KEYWORDS: dict[Topic, list[str]] = {
    Topic.AI: [
        "artificial intelligence",
        "machine learning",
        "LLM",
        "generative AI",
        "OpenAI",
        "Anthropic",
    ],
    Topic.AUTOMATION: [
        "automation",
        "RPA",
        "n8n",
        "workflow automation",
        "agentic AI",
    ],
    Topic.STARTUPS: [
        "startup funding",
        "Series A",
        "venture capital",
        "tech startup",
    ],
    Topic.TECH: [
        "technology",
        "software engineering",
        "developer tools",
        "cloud computing",
    ],
}

REQUEST_TIMEOUT = 15  # seconds per API call

# Storage directory for user preferences
STORAGE_DIR = Path(os.getenv("STORAGE_DIR", "storage"))
PREFERENCES_FILE = STORAGE_DIR / "preferences.json"

# Article limits - conservative for testing with daily API quotas
NEWSAPI_PAGE_SIZE = int(os.getenv("NEWSAPI_PAGE_SIZE", "3"))   # Conservative: 3 articles per topic for testing
GNEWS_MAX_RESULTS = int(os.getenv("GNEWS_MAX_RESULTS", "2"))   # Conservative: 2 articles per topic for testing


# ---------------------------------------------------------------------------
# User preference helpers
# ---------------------------------------------------------------------------


def _load_user_preferences() -> dict:
    """
    Load user preferences from storage/preferences.json.

    Returns:
        Dictionary mapping user_id to UserPreferences data.
    """
    try:
        if PREFERENCES_FILE.exists():
            with open(PREFERENCES_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                logger.info(f"Loaded preferences for {len(data)} users from {PREFERENCES_FILE}")
                return data
        else:
            logger.warning(f"Preferences file not found at {PREFERENCES_FILE}, using empty preferences")
            return {}
    except Exception as exc:
        logger.error(f"Failed to load user preferences: {exc}")
        return {}


def _get_user_topics(user_id: Optional[int] = None) -> list[Topic]:
    """
    Get topics for a specific user, or return default topics if user not found.

    Args:
        user_id: Telegram user_id to fetch topics for. If None, returns default topics.

    Returns:
        List of Topic enum values for the specified user.
    """
    if user_id is None:
        logger.info("No user_id provided, using default topics: [AI, TECH]")
        return [Topic.AI, Topic.TECH]

    preferences = _load_user_preferences()
    user_key = str(user_id)

    if user_key in preferences:
        user_data = preferences[user_key]
        topics_str = user_data.get("topics", ["ai", "tech"])

        # Convert string topics to Topic enum
        topics = []
        for topic_str in topics_str:
            try:
                topic = Topic(topic_str)
                topics.append(topic)
            except ValueError:
                logger.warning(f"Invalid topic '{topic_str}' for user {user_id}, skipping")

        if topics:
            logger.info(f"Found {len(topics)} topics for user {user_id}: {[t.value for t in topics]}")
            return topics
        else:
            logger.warning(f"No valid topics found for user {user_id}, using defaults")
            return [Topic.AI, Topic.TECH]
    else:
        logger.warning(f"User {user_id} not found in preferences, using default topics: [AI, TECH]")
        return [Topic.AI, Topic.TECH]


def _get_date_range(days_back: int = 7) -> tuple[str, str]:
    """
    Get date range for news filtering.

    Args:
        days_back: Number of days to look back from today.

    Returns:
        Tuple of (from_date, to_date) in ISO format.
    """
    to_date = datetime.now(timezone.utc)
    from_date = to_date - timedelta(days=days_back)

    # Format for NewsAPI (YYYY-MM-DD)
    from_str = from_date.strftime("%Y-%m-%d")
    to_str = to_date.strftime("%Y-%m-%d")

    logger.info(f"Date range: {from_str} to {to_str} (last {days_back} days)")
    return from_str, to_str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _article_id(source: str, url: str) -> str:
    """Stable 16-char dedup key from source name + URL."""
    raw = f"{source}:{url}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _normalise_url(url: str) -> str:
    """Strip query params and fragments so near-duplicate URLs collapse."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")


def _parse_dt(value: Optional[str]) -> datetime:
    """Parse ISO-8601 datetime string; fall back to now on failure."""
    if not value:
        return datetime.now(timezone.utc)
    try:
        # Both APIs return UTC strings; Python 3.11+ handles 'Z' natively.
        # For 3.10 compatibility we replace 'Z' manually.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Could not parse datetime %r, using utcnow", value)
        return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Trafilatura DOM Cleaner
# ---------------------------------------------------------------------------


def _clean_content(content: str | None) -> str | None:
    """
    Clean article content using trafilatura to extract only clean text.

    Removes HTML tags, scripts, ads, and other noise while preserving
    the main article content. Returns None if content is None or empty.
    """
    if not content:
        return None

    try:
        # Use trafilatura to extract clean text
        clean_text = trafilatura.extract(content)
        if clean_text:
            return clean_text.strip()
        return None
    except Exception as exc:
        logger.debug("Failed to clean content with trafilatura: %s", exc)
        return content  # Return original if cleaning fails


# ---------------------------------------------------------------------------
# NewsAPI fetcher
# ---------------------------------------------------------------------------


async def _fetch_newsapi(
    client: httpx.AsyncClient,
    topic: Topic,
    keywords: list[str],
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> list[RawArticle]:
    """Fetch up to 30 recent articles from NewsAPI for one topic with date filtering."""
    logger.info("NEWSAPI START: Fetching for topic %s with keywords %s", topic.value, keywords[:4])

    if not NEWSAPI_KEY:
        logger.warning("NEWSAPI SKIP: Key not set — skipping NewsAPI for topic %s", topic)
        return []

    query = " OR ".join(f'"{kw}"' for kw in keywords[:4])  # keep query readable
    params = {
        "q": query,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": NEWSAPI_PAGE_SIZE,  # Using 30 for balanced coverage
        "apiKey": NEWSAPI_KEY,
    }

    # Add date range for recent, topic-specific news
    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date

    logger.info(f"NEWSAPI CONFIG: pageSize={params['pageSize']}, date range: {from_date} to {to_date}")

    try:
        logger.info("NEWSAPI CALL: Making request to %s with query '%s'", NEWSAPI_BASE, query)
        resp = await client.get(NEWSAPI_BASE, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        logger.info("NEWSAPI SUCCESS: Received response with %d articles", len(data.get("articles", [])))
    except httpx.HTTPError as exc:
        logger.error("NEWSAPI ERROR: Request failed for topic %s: %s", topic, exc)
        return []

    articles: list[RawArticle] = []
    for item in data.get("articles", []):
        url = item.get("url", "")
        if not url or url == "https://removed.com":
            continue
        articles.append(
            RawArticle(
                article_id=_article_id("newsapi", url),
                title=item.get("title") or "Untitled",
                description=item.get("description"),
                content=_clean_content(item.get("content")),  # MAP-REDUCE: Clean content with trafilatura
                url=url,
                source_name=item.get("source", {}).get("name", "Unknown"),
                api_source="newsapi",
                published_at=_parse_dt(item.get("publishedAt")),
                topics_matched=[topic],
            )
        )

    logger.info("NewsAPI returned %d articles for topic %s", len(articles), topic)
    return articles


# ---------------------------------------------------------------------------
# GNews fetcher
# ---------------------------------------------------------------------------


async def _fetch_gnews(
    client: httpx.AsyncClient,
    topic: Topic,
    keywords: list[str],
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> list[RawArticle]:
    """Fetch up to 10 recent articles from GNews for one topic with date filtering."""
    logger.info("GNEWS START: Fetching for topic %s with keywords %s", topic.value, keywords[:3])

    if not GNEWS_KEY:
        logger.warning("GNEWS SKIP: Key not set — skipping GNews for topic %s", topic)
        return []

    # Apply rate limiting
    logger.info("GNEWS RATE LIMIT: Checking rate limiter before request...")
    await gnews_rate_limiter.wait_if_needed()
    logger.info("GNEWS RATE LIMIT: Rate limiter check passed")

    # GNews free tier: max 10 results, only supports simple keyword queries.
    query = " OR ".join(keywords[:3])
    params = {
        "q": query,
        "lang": "en",
        "max": GNEWS_MAX_RESULTS,  # Using 10 (max allowed on free tier)
        "sortby": "publishedAt",
        "apikey": GNEWS_KEY,
    }

    # Add date range if GNews supports it
    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date

    logger.info(f"GNEWS CONFIG: max={params['max']}, date range: {from_date} to {to_date}")

    try:
        logger.info("GNEWS CALL: Making request to %s with query '%s'", GNEWS_BASE, query)
        resp = await client.get(GNEWS_BASE, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        logger.info("GNEWS SUCCESS: Received response with %d articles", len(data.get("articles", [])))
    except httpx.HTTPError as exc:
        logger.error("GNEWS ERROR: Request failed for topic %s: %s", topic, exc)
        return []

    articles: list[RawArticle] = []
    for item in data.get("articles", []):
        url = item.get("url", "")
        if not url:
            continue
        articles.append(
            RawArticle(
                article_id=_article_id("gnews", url),
                title=item.get("title") or "Untitled",
                description=item.get("description"),
                content=_clean_content(item.get("content")),  # MAP-REDUCE: Clean content with trafilatura
                url=url,
                source_name=item.get("source", {}).get("name", "Unknown"),
                api_source="gnews",
                published_at=_parse_dt(item.get("publishedAt")),
                topics_matched=[topic],
            )
        )

    logger.info("GNews returned %d articles for topic %s", len(articles), topic)
    return articles


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


async def fetch_articles(topics: Optional[list[Topic]] = None, user_id: Optional[int] = None) -> list[RawArticle]:
    """
    Fetch and deduplicate articles for all requested topics.

    Args:
        topics: List of Topic enum values to fetch signals for. If None, uses user-specific topics.
        user_id: Telegram user_id to fetch personalized topics for. If provided, overrides topics parameter.

    Returns:
        Deduplicated list of RawArticle, sorted newest-first.
    """
    # Determine which topics to use
    if user_id is not None:
        # Use user-specific topics from preferences
        topics = _get_user_topics(user_id)
        logger.info(f"FETCHER: Using user-specific topics for user {user_id}")
    elif topics is None:
        # Use default topics if neither user_id nor topics provided
        topics = [Topic.AI, Topic.TECH]
        logger.info("FETCHER: No topics or user_id provided, using default topics [AI, TECH]")

    logger.info("=" * 60)
    logger.info("FETCHER START: Fetching articles for %d topics", len(topics))
    logger.info("FETCHER TOPICS: %s", [t.value for t in topics])
    logger.info("FETCHER USER: %s", "user-specific" if user_id else "default")
    logger.info("FETCHER API STATUS: NewsAPI key available=%s, GNews key available=%s",
                bool(NEWSAPI_KEY), bool(GNEWS_KEY))
    logger.info("=" * 60)

    if not topics:
        logger.warning("FETCHER: Called with empty topic list")
        return []

    # Get date range for recent news (last 7 days)
    from_date, to_date = _get_date_range(days_back=7)

    async with httpx.AsyncClient() as client:
        tasks = []
        for topic in topics:
            keywords = TOPIC_KEYWORDS.get(topic, [topic.value])
            logger.info("FETCHER: Adding NewsAPI task for topic %s", topic.value)
            tasks.append(_fetch_newsapi(client, topic, keywords, from_date, to_date))
            # GNews now has built-in rate limiting, so no manual delay needed
            logger.info("FETCHER: Adding GNews task for topic %s", topic.value)
            tasks.append(_fetch_gnews(client, topic, keywords, from_date, to_date))

        logger.info("FETCHER: Starting %d concurrent API calls with asyncio.gather...", len(tasks))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("FETCHER: All API calls completed")

    # Flatten, skip exceptions (already logged inside each fetcher)
    raw: list[RawArticle] = []
    successful_sources = 0
    total_sources = 0

    logger.info("FETCHER: Processing results from %d API sources...", len(results))
    for result in results:
        total_sources += 1
        if isinstance(result, Exception):
            logger.error("Fetcher task raised an exception: %s", result)
            continue
        if result is None:
            logger.warning("Fetcher returned None - skipping")
            continue
        raw.extend(result)
        successful_sources += 1

    # Graceful degradation: if API sources fail, log but continue with scraped data
    if successful_sources < total_sources:
        logger.warning(
            f"Only {successful_sources}/{total_sources} sources succeeded. "
            f"Pipeline will continue with available data."
        )

    # Deduplicate by normalised URL — keep first occurrence (newest-first sort
    # happens after dedup so the first occurrence is from whichever API was faster,
    # not necessarily newer; we re-sort below).
    seen_urls: set[str] = set()
    seen_ids: set[str] = set()
    deduped: list[RawArticle] = []

    for article in raw:
        norm_url = _normalise_url(article.url)
        if norm_url in seen_urls or article.article_id in seen_ids:
            # Merge topics_matched onto the already-kept article instead of dropping
            for kept in deduped:
                if _normalise_url(kept.url) == norm_url:
                    for t in article.topics_matched:
                        if t not in kept.topics_matched:
                            kept.topics_matched.append(t)
                    break
            continue
        seen_urls.add(norm_url)
        seen_ids.add(article.article_id)
        deduped.append(article)

    logger.info("FETCHER: Deduplicating %d raw articles...", len(raw))

    deduped.sort(key=lambda a: a.published_at, reverse=True)

    logger.info(
        "FETCHER RESULT: %d raw articles → %d after deduplication (topics: %s, sources: %d/%d)",
        len(raw),
        len(deduped),
        [t.value for t in topics],
        successful_sources,
        total_sources,
    )
    return deduped


# ---------------------------------------------------------------------------
# CLI smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO)

    async def _smoke():
        articles = await fetch_articles([Topic.AI, Topic.TECH])
        print(f"\nFetched {len(articles)} articles\n")
        for a in articles[:3]:
            print(json.dumps(a.model_dump(mode="json"), indent=2, default=str))
            print("---")

    asyncio.run(_smoke())
