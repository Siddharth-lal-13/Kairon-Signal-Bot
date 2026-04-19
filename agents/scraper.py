"""
Kairon — Scraper Agent

Scrapes two sources using crawl4ai:
1. Hacker News (news.ycombinator.com) — front page stories
2. Ars Technica (arstechnica.com) — front page headlines

Design decisions:
  - crawl4ai AsyncWebCrawler in lightweight fetch mode (no browser)
  - HN: extract title, URL, points score, comment count as relevance signals
  - Ars Technica: extract title, URL, and summary/description
  - Keyword-based topic matching (same logic as fetcher)
  - Graceful failure handling — one source failing doesn't kill the other
  - Returns list[RawArticle] using existing schema with api_source="hn_scrape"
    and api_source="ars_scrape"
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Optional

from bs4 import BeautifulSoup
from crawl4ai import AsyncWebCrawler
from dotenv import load_dotenv

from models.schemas import RawArticle, Topic

# Fix Windows console encoding issue
if sys.platform == "win32":
    import codecs
    sys.stdout = codecs.getwriter("utf-8")(sys.stdout.detach())
    sys.stderr = codecs.getwriter("utf-8")(sys.stderr.detach())

load_dotenv()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Topic keywords for matching (same as fetcher)
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

# Crawler configuration — lightweight mode, no browser
CRAWLER_TIMEOUT = 10  # seconds per crawl

# Article limits for testing (conservative to stay within daily quotas)
HACKER_NEWS_MAX_STORIES = int(os.getenv("HACKER_NEWS_MAX_STORIES", "2"))   # Conservative: 2 stories from HN for testing
ARS_TECHNICA_MAX_ARTICLES = int(os.getenv("ARS_TECHNICA_MAX_ARTICLES", "2"))  # Conservative: 2 articles from Ars for testing

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _match_topic(text: str, selected_topics: list[Topic] | None = None) -> list[Topic]:
    """
    Return list of topics that match given text (case-insensitive).

    Args:
        text: Text to match topics against
        selected_topics: If provided, only match against these specific topics
                        (None means match against all possible topics)
    """
    text_lower = text.lower()
    matched: list[Topic] = []

    # If selected_topics is provided, only match against those
    topics_to_check = selected_topics if selected_topics else TOPIC_KEYWORDS.keys()

    for topic in topics_to_check:
        keywords = TOPIC_KEYWORDS.get(topic, [])
        for keyword in keywords:
            if keyword.lower() in text_lower:
                if topic not in matched:
                    matched.append(topic)
                break  # match one keyword per topic

    return matched


async def _fetch_hacker_news(selected_topics: list[Topic] | None = None) -> list[RawArticle]:
    """
    Scrape Hacker News front page using crawl4ai.

    Extracts:
    - Story title and actual article URL (not comments link)
    - Points score and comment count as relevance signals
    """
    articles: list[RawArticle] = []

    try:
        crawler = AsyncWebCrawler(verbose=False)
        result = await crawler.arun(
            url="https://news.ycombinator.com/",
            bypass_cache=True,
            process_iframes=False,
            remove_overlay_elements=True,
            simulate_user=False,  # lightweight mode
            timeout=CRAWLER_TIMEOUT,
        )

        html_content = result.cleaned_html
        if not html_content:
            logger.warning("Hacker News returned empty HTML")
            return articles

        # Debug: log first 1000 chars of HTML
        logger.info("HN HTML sample (first 1000 chars): %s", html_content[:1000])

        # Parse HN articles using BeautifulSoup
        soup = BeautifulSoup(html_content, 'html.parser')

        # Find all story rows
        story_rows = soup.find_all('tr', class_='athing')

        for row in story_rows[:HACKER_NEWS_MAX_STORIES]:  # Limit to configured number of stories
            try:
                # Find title line
                title_line = row.find('span', class_='titleline')
                if not title_line:
                    continue

                link = title_line.find('a')
                if not link:
                    continue

                title = link.get_text().strip()
                url = link.get('href', '')

                # Skip HN internal links (comments, etc.)
                if not url or url.startswith('https://news.ycombinator.com'):
                    continue

                # Find subline for points and comments
                subline = row.find_next_sibling('tr')
                if subline:
                    subline = subline.find('td', class_='subtext')
                    if subline:
                        # Extract points
                        points_span = subline.find('span', id=lambda x: x and x.startswith('score_'))
                        points = int(points_span.get_text().replace('points', '').strip()) if points_span else 0

                        # Extract comments count
                        comments = 0
                        for a_tag in subline.find_all('a'):
                            href = a_tag.get('href', '')
                            if 'item' in href or 'user' in href:
                                continue
                            comment_text = a_tag.get_text().strip()
                            if 'comment' in comment_text.lower():
                                try:
                                    comments = int(comment_text.split()[0])
                                except (ValueError, IndexError):
                                    pass

                # Match topics based on title (only selected topics)
                topics_matched = _match_topic(title, selected_topics)
                if not topics_matched:
                    # Only fallback to TECH if user selected TECH or no specific topics provided
                    if not selected_topics or Topic.TECH in selected_topics:
                        topics_matched = [Topic.TECH]  # fallback, analyzer will score relevance
                    else:
                        # User selected specific topics but none matched - skip this article
                        continue

                # Create RawArticle with relevance metadata in description
                relevance_info = f"Points: {points}, Comments: {comments}"

                articles.append(
                    RawArticle(
                        article_id=f"hn_{abs(hash(url)) % 100000:05d}",  # Stable short ID
                        title=title,
                        description=relevance_info,  # Store points/comments here
                        content=None,
                        url=url,
                        source_name="Hacker News",
                        api_source="hn_scrape",
                        published_at=datetime.now(timezone.utc),  # Use current time as HN doesn't provide timestamps
                        topics_matched=topics_matched,
                    )
                )

            except Exception as exc:
                logger.debug("Failed to parse HN story: %s", exc)
                continue

        logger.info("Hacker News scraper returned %d articles", len(articles))
        return articles

    except Exception as exc:
        logger.error("Hacker News scraping failed: %s", exc)
        return articles


async def _fetch_ars_technica(selected_topics: list[Topic] | None = None) -> list[RawArticle]:
    """
    Scrape Ars Technica front page using crawl4ai.

    Extracts:
    - Article title, URL, and summary/description
    """
    articles: list[RawArticle] = []

    try:
        crawler = AsyncWebCrawler(verbose=False)
        result = await crawler.arun(
            url="https://arstechnica.com/",
            bypass_cache=True,
            process_iframes=False,
            remove_overlay_elements=True,
            simulate_user=False,  # lightweight mode
            timeout=CRAWLER_TIMEOUT,
        )

        html_content = result.html
        if not html_content:
            logger.warning("Ars Technica returned empty HTML")
            return articles

        # Parse Ars Technica articles using BeautifulSoup
        soup = BeautifulSoup(html_content, 'html.parser')

        # Find article links - look for h2 or h3 tags with links
        articles_found = []

        # Try different selectors for Ars Technica front page
        for header in soup.find_all(['h2', 'h3']):
            link = header.find('a')
            if link and link.get('href'):
                url = link.get('href')
                title = link.get_text().strip()

                # Skip non-article links
                if not url or not url.startswith('https://arstechnica.com'):
                    continue

                articles_found.append((url, title))

        # Limit to configured number of articles
        for url, title in articles_found[:ARS_TECHNICA_MAX_ARTICLES]:
            try:
                # Match topics based on title (only selected topics)
                topics_matched = _match_topic(title, selected_topics)
                if not topics_matched:
                    # Only fallback to TECH if user selected TECH or no specific topics provided
                    if not selected_topics or Topic.TECH in selected_topics:
                        topics_matched = [Topic.TECH]  # fallback, analyzer will score relevance
                    else:
                        # User selected specific topics but none matched - skip this article
                        continue

                articles.append(
                    RawArticle(
                        article_id=f"ars_{abs(hash(url)) % 100000:05d}",  # Stable short ID
                        title=title,
                        description=None,  # Ars doesn't always show summaries on front page
                        content=None,
                        url=url,
                        source_name="Ars Technica",
                        api_source="ars_scrape",
                        published_at=datetime.now(timezone.utc),  # Use current time as Ars front page doesn't have dates
                        topics_matched=topics_matched,
                    )
                )

            except Exception as exc:
                logger.debug("Failed to parse Ars Technica article: %s", exc)
                continue

        logger.info("Ars Technica scraper returned %d articles", len(articles))
        return articles

    except Exception as exc:
        logger.error("Ars Technica scraping failed: %s", exc)
        return articles


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


async def scrape_articles(topics: list[Topic]) -> list[RawArticle]:
    """
    Scrape articles from HN and Ars Technica concurrently.

    Args:
        topics: List of Topic enum values to match against (unused internally,
                scrapers match based on their own keyword logic).

    Returns:
        Deduplicated list of RawArticle from both scrapers.
    """
    if not topics:
        logger.warning("scrape_articles called with empty topic list")
        return []

    # Run both scrapers concurrently, passing selected topics
    tasks = [_fetch_hacker_news(topics), _fetch_ars_technica(topics)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Combine results, skipping exceptions (already logged)
    raw: list[RawArticle] = []
    for result in results:
        if isinstance(result, Exception):
            logger.error("Scraper task raised an exception: %s", result)
            continue
        # Debug: Check if result is valid before extending
        if result is None:
            logger.warning("Scraper returned None, skipping")
            continue
        if not isinstance(result, list):
            logger.error("Scraper returned non-iterable: %s (type: %s)", result, type(result))
            continue
        raw.extend(result)

    # Deduplicate by URL (keep first occurrence)
    seen_urls = set()
    deduped: list[RawArticle] = []

    for article in raw:
        if article.url not in seen_urls:
            seen_urls.add(article.url)
            deduped.append(article)

    logger.info(
        "Scraper: %d raw articles -> %d after deduplication",
        len(raw),
        len(deduped),
    )
    return deduped


# ---------------------------------------------------------------------------
# CLI smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import logging as _logging
    import warnings

    # Ignore Windows cleanup noise from crawl4ai subprocess
    warnings.filterwarnings("ignore", category=ResourceWarning)

    _logging.basicConfig(level=logging.DEBUG)

    async def _smoke():
        articles = await scrape_articles([Topic.AI, Topic.TECH])
        print(f"\nScraped {len(articles)} articles\n")
        for a in articles[:3]:
            print(json.dumps(a.model_dump(mode="json"), indent=2, default=str))
            print("---")

    asyncio.run(_smoke())