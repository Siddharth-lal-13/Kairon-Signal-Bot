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
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv

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
    """Rate limiter for GNews API to prevent rate limiting errors."""
    def __init__(self):
        self.requests_per_minute = random.randint(65, 70)  # Random between 65-70 requests/minute
        self.last_request_time = 0

    async def wait_if_needed(self):
        """Wait if necessary to respect rate limit."""
        time_since_last = time.time() - self.last_request_time
        min_interval = 60 / self.requests_per_minute

        if time_since_last < min_interval:
            wait_time = min_interval - time_since_last
            await asyncio.sleep(wait_time)

        self.last_request_time = time.time()

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
# NewsAPI fetcher
# ---------------------------------------------------------------------------


async def _fetch_newsapi(
    client: httpx.AsyncClient,
    topic: Topic,
    keywords: list[str],
) -> list[RawArticle]:
    """Fetch up to 20 recent articles from NewsAPI for one topic."""
    if not NEWSAPI_KEY:
        logger.warning("NEWSAPI_KEY not set — skipping NewsAPI for topic %s", topic)
        return []

    query = " OR ".join(f'"{kw}"' for kw in keywords[:4])  # keep query readable
    params = {
        "q": query,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": 5,  # Reduced for faster testing
        "apiKey": NEWSAPI_KEY,
    }

    try:
        resp = await client.get(NEWSAPI_BASE, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        logger.error("NewsAPI request failed for topic %s: %s", topic, exc)
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
                content=item.get("content"),
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
) -> list[RawArticle]:
    """Fetch up to 10 recent articles from GNews for one topic."""
    if not GNEWS_KEY:
        logger.warning("GNEWS_KEY not set — skipping GNews for topic %s", topic)
        return []

    # Apply rate limiting
    await gnews_rate_limiter.wait_if_needed()

    # GNews free tier: max 10 results, only supports simple keyword queries.
    query = " OR ".join(keywords[:3])
    params = {
        "q": query,
        "lang": "en",
        "max": 2,  # Reduced from 3 to 2 to avoid rate limiting
        "sortby": "publishedAt",
        "apikey": GNEWS_KEY,
    }

    try:
        resp = await client.get(GNEWS_BASE, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        logger.error("GNews request failed for topic %s: %s", topic, exc)
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
                content=item.get("content"),
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


async def fetch_articles(topics: list[Topic]) -> list[RawArticle]:
    """
    Fetch and deduplicate articles for all requested topics.

    Args:
        topics: List of Topic enum values to fetch signals for.

    Returns:
        Deduplicated list of RawArticle, sorted newest-first.
    """
    if not topics:
        logger.warning("fetch_articles called with empty topic list")
        return []

    async with httpx.AsyncClient() as client:
        tasks = []
        for topic in topics:
            keywords = TOPIC_KEYWORDS.get(topic, [topic.value])
            tasks.append(_fetch_newsapi(client, topic, keywords))
            # GNews now has built-in rate limiting, so no manual delay needed
            tasks.append(_fetch_gnews(client, topic, keywords))

        results = await asyncio.gather(*tasks, return_exceptions=True)

    # Flatten, skip exceptions (already logged inside each fetcher)
    raw: list[RawArticle] = []
    successful_sources = 0
    total_sources = 0

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

    deduped.sort(key=lambda a: a.published_at, reverse=True)

    logger.info(
        "Fetcher: %d raw articles → %d after deduplication (topics: %s, sources: %d/%d)",
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
