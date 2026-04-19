"""
Kairon — Analyzer Agent (OpenRouter Version)

Takes a list of RawArticle objects and runs each through a LangChain LCEL
chain backed by OpenRouter free models. Returns a list of AnalyzedArticle
objects with LLM-extracted signal metadata.

Design decisions:
  - LCEL chain: prompt | llm | output_parser  (no agents, no tools — fast)
  - JSON mode via PydanticOutputParser for structured extraction
  - OpenRouter with multiple free model fallbacks for reliability
  - Articles are processed in a semaphore-guarded async loop
  - Multiple free models with automatic fallback on busy/network errors
  - Robust retry logic with exponential backoff for network issues
  - **SEQUENTIAL PROCESSING** to respect OpenRouter free model rate limits (8-16 req/min)
  - Rate limit-aware retry logic that respects X-RateLimit-Reset headers
  - Relevance scoring is done by the LLM; articles below RELEVANCE_THRESHOLD
    are dropped before returning so the synthesizer doesn't have to filter.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from typing import Optional, List
from datetime import datetime, timezone

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from models.schemas import AnalyzedArticle, RawArticle, SignalType, Topic

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL: str = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
MAX_CONCURRENT_LLM: int = int(os.getenv("MAX_CONCURRENT_LLM", "1"))  # SAFE: Sequential processing for rate limits
RELEVANCE_THRESHOLD: float = float(os.getenv("RELEVANCE_THRESHOLD", "0.2"))

# Powerful OpenRouter free models with fallback priority
FREE_MODELS: List[dict] = [
    {
        "name": "meta-llama/llama-3.3-70b-instruct:free",
        "description": "Llama 3.3 70B - State-of-the-art reasoning",
        "priority": 1
    },
    {
        "name": "openai/gpt-oss-120b:free",
        "description": "GPT-OSS 120B - Massive context and reasoning",
        "priority": 2
    },
    {
        "name": "qwen/qwen3-next-80b-a3b-instruct:free",
        "description": "Qwen3 Next 80B - Excellent for technical content",
        "priority": 3
    },
    {
        "name": "nousresearch/hermes-3-llama-3.1-405b:free",
        "description": "Hermes 3 405B - Massive model, deep analysis",
        "priority": 4
    },
    {
        "name": "google/gemma-4-26b-a4b-it:free",
        "description": "Gemma 4 26B - Fast and capable",
        "priority": 5
    },
    {
        "name": "nvidia/nemotron-3-super-120b-a12b:free",
        "description": "Nemotron Super 120B - Enterprise-grade reasoning",
        "priority": 6
    },
    {
        "name": "z-ai/glm-4.5-air:free",
        "description": "GLM 4.5 Air - Balanced performance",
        "priority": 7
    },
    {
        "name": "google/gemma-4-31b-it:free",
        "description": "Gemma 4 31B - Reliable fallback",
        "priority": 8
    }
]

# Content character limit increased for powerful OpenRouter models
# With 70B+ parameter models, we can process much more context
CONTENT_CHAR_LIMIT = 3000  # Increased from 800 for full article analysis


# ---------------------------------------------------------------------------
# OpenRouter Rate Limiter (respects free model limits: 8-16 req/min)
# ---------------------------------------------------------------------------


class OpenRouterRateLimiter:
    """
    Global rate limiter for OpenRouter free models to avoid 429 errors.

    OpenRouter free models have strict rate limits (typically 8-16 requests per minute).
    This limiter ensures we stay within limits by:
    1. Tracking request timing globally
    2. Adding substantial delays between requests (AI models need time to think)
    3. Respecting rate limit reset times from API responses
    4. Accounting for actual model processing time (30-60 seconds per request)
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        self._last_request_time = 0.0
        self._min_request_interval = 60.0  # Conservative: 1 request per 60 seconds (1 req/min)
        self._global_rate_limit_reset = 0  # Timestamp when global limit resets
        self._requests_in_current_window = 0

    async def wait_if_needed(self, reset_time: Optional[int] = None) -> None:
        """
        Wait if necessary to respect rate limits.

        Args:
            reset_time: Optional Unix timestamp when rate limit resets (from X-RateLimit-Reset header)
        """
        async with self._lock:
            now = time.time()

            # Check if we have a specific reset time from a 429 response
            if reset_time:
                wait_time = reset_time - now
                if wait_time > 0:
                    logger.warning(f"Rate limited, waiting {wait_time:.1f}s until {datetime.fromtimestamp(reset_time, tz=timezone.utc)}")
                    await asyncio.sleep(wait_time)
                    self._requests_in_current_window = 0
                    return

            # Apply minimum interval between requests (AI models need 30-60 seconds to think)
            elapsed = now - self._last_request_time
            if elapsed < self._min_request_interval:
                wait_time = self._min_request_interval - elapsed
                logger.info(f"Rate limiter: waiting {wait_time:.1f}s before next request (AI models need time to think)")
                await asyncio.sleep(wait_time)

            self._last_request_time = time.time()
            self._requests_in_current_window += 1


# Global rate limiter instance
_openrouter_rate_limiter = OpenRouterRateLimiter()


def _parse_rate_limit_reset(reset_time_str: str) -> Optional[int]:
    """
    Parse X-RateLimit-Reset header to get Unix timestamp.

    Args:
        reset_time_str: Reset time string from API response

    Returns:
        Unix timestamp or None if parsing fails
    """
    try:
        # OpenRouter returns timestamp in milliseconds
        return int(reset_time_str) // 1000  # Convert to seconds
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# LLM extraction schema (internal — not exposed outside this module)
# ---------------------------------------------------------------------------


class _ArticleExtraction(BaseModel):
    """Structured output expected from the LLM for each article."""

    signal_type: SignalType = Field(
        description=(
            "Category: product_launch, funding, acquisition, research, "
            "regulation, trend, opinion, or other"
        )
    )
    one_line_summary: str = Field(
        description="One sentence (≤25 words) summarising what happened"
    )
    why_it_matters: str = Field(
        description="One sentence (≤40 words) explaining the signal's significance"
    )
    key_entities: list[str] = Field(
        description="Up to 5 company, person, or technology names central to the story",
        max_length=5,
    )
    relevance_score: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Float 0–1: how relevant this article is to AI/automation/startup/tech signals. "
            "0.0 = completely off-topic, 1.0 = highly relevant breaking signal"
        ),
    )


# ---------------------------------------------------------------------------
# Build the LCEL chain (built once, reused across all articles)
# ---------------------------------------------------------------------------

_parser = PydanticOutputParser(pydantic_object=_ArticleExtraction)

_SYSTEM = """\
You are an expert intelligence analyst specializing in AI, automation, startups, and technology signals. You will receive a news article and extract structured intelligence from it.

CRITICAL REQUIREMENTS - READ CAREFULLY:
- Return ONLY valid JSON - no markdown, no explanations, no thinking process
- Start your response immediately with a curly brace opening and end with a curly brace closing
- Do NOT include any text before or after the JSON
- Do NOT include "Thinking Process" or any other non-JSON content
- Do NOT use markdown formatting or code blocks
- Output must be parseable as raw JSON with no extra characters

ANALYSIS INSTRUCTIONS - COMPREHENSIVE ARTICLE ANALYSIS:
- Read the FULL article content provided, not just the title
- Look for deep insights, not just surface-level mentions
- Identify the actual impact and significance of the news
- Consider both immediate effects and long-term implications
- Extract specific companies, people, technologies, and metrics mentioned
- Determine the primary signal type (what kind of news is this?)

SIGNAL TYPE CLASSIFICATION:
- product_launch: New product, feature, service, or tool release
- funding: Investment rounds, funding announcements, financial backing
- acquisition: M&A activity, company buyouts, mergers
- research: Academic research, scientific breakthroughs, papers published
- regulation: Policy changes, regulations, compliance, legal frameworks
- trend: Emerging patterns, market shifts, behavioral changes
- opinion: Commentary, analysis, predictions, thought leadership
- other: Anything that doesn't fit the above categories

RELEVANCE SCORING GUIDELINES - BE GENEROUS:
- 0.8-1.0: Directly related to AI/automation/startups/tech with significant impact and actionable insights
- 0.5-0.7: Strongly related with clear connections to the target topics
- 0.3-0.4: Tangentially related but contains relevant information or context
- 0.0-0.2: Off-topic or very weak relevance
- IMPORTANT: Prefer higher scores to avoid filtering good content
- If the article mentions any AI, automation, startup, or tech concepts, score at least 0.3
- Consider the source credibility and information quality
- Factor in the recency and potential impact of the information

ONE-LINE SUMMARY GUIDELINES:
- Maximum 25 words
- Capture the essence of what happened
- Include the main subject and key action
- Be specific and factual
- Example: "OpenAI launches GPT-5 with enhanced reasoning capabilities"

WHY IT MATTERS GUIDELINES:
- Maximum 40 words
- Explain the significance and impact
- Consider implications for the industry/users
- Connect to broader trends or patterns
- Example: "Sets new benchmark for AI reasoning and accelerates enterprise adoption"

KEY ENTITIES GUIDELINES:
- Extract up to 5 most important entities
- Include company names, key people, technologies, or products
- Prioritize entities that are central to the story
- Use the exact names as mentioned in the article

Follow the schema exactly: signal_type, one_line_summary, why_it_matters, key_entities, relevance_score.

{format_instructions}
"""

_HUMAN = """\
Article title: {title}
Source: {source}
Published: {published_at}
Topics matched: {topics}

Content:
{content}

Extract the signal. Respond only with the JSON object.
"""

_prompt = ChatPromptTemplate.from_messages(
    [
        ("system", _SYSTEM),
        ("human", _HUMAN),
    ]
).partial(format_instructions=_parser.get_format_instructions())

# Create LLM instances for each free model (cached for reuse)
_llm_cache: dict[str, ChatOpenAI] = {}

def _get_llm(model_name: str) -> ChatOpenAI:
    """Get or create LLM instance for a specific model."""
    if model_name not in _llm_cache:
        _llm_cache[model_name] = ChatOpenAI(
            model=model_name,
            openai_api_key=OPENROUTER_API_KEY,
            openai_api_base=OPENROUTER_BASE_URL,
            temperature=0.1,
            timeout=120,  # 2 minutes timeout - AI models need time to think
            max_retries=0,  # We handle retries ourselves
            request_timeout=120,  # Allow 2 minutes for model processing
        )
        logger.info(f"Created LLM instance for model: {model_name} with 120s timeout")
    return _llm_cache[model_name]

def _parse_openrouter_response(message) -> _ArticleExtraction:
    """
    Parse OpenRouter response, handling various response formats.
    """
    import json
    import re

    # Extract content from AIMessage object
    if hasattr(message, 'content'):
        text = message.content
    else:
        text = str(message)

    logger.debug(f"OpenRouter response type: {type(message)}")
    logger.debug(f"OpenRouter response content: '{text[:200]}...'")

    # Try direct JSON parsing first
    try:
        if text and text.strip():
            # Clean common issues
            text = text.strip()
            # Remove markdown code blocks if present
            text = re.sub(r'```json\s*', '', text)
            text = re.sub(r'```\s*', '', text)

            json_data = json.loads(text)
            if isinstance(json_data, dict) and 'signal_type' in json_data:
                logger.debug("Successfully parsed JSON directly")
                return _ArticleExtraction(**json_data)
    except (json.JSONDecodeError, ValueError) as e:
        logger.debug(f"Direct JSON parsing failed: {e}")

    # Try to extract JSON from text using regex
    try:
        json_match = re.search(r'\{[^{}]*"signal_type"[^{}]*\}', text, re.DOTALL)
        if json_match:
            json_data = json.loads(json_match.group())
            if 'signal_type' in json_data:
                logger.debug("Extracted JSON using regex")
                return _ArticleExtraction(**json_data)
    except Exception as e:
        logger.debug(f"Regex JSON extraction failed: {e}")

    # Fallback to standard parser
    try:
        return _parser.parse(text)
    except Exception as e:
        logger.error(f"All parsing methods failed: {e}")
        raise ValueError(f"Failed to parse LLM response: {text[:200]}...")


# ---------------------------------------------------------------------------
# Per-article analysis with model fallback
# ---------------------------------------------------------------------------


async def _analyze_with_retry(
    article: RawArticle,
    content_text: str,
    max_total_retries: int = 2  # Minimal retries, prefer model switching
) -> Optional[_ArticleExtraction]:
    """
    Analyze article with multiple model fallbacks and smart retry logic.

    Strategy:
    1. Use global rate limiter to respect OpenRouter limits (1 req/min to be safe)
    2. Try primary model with minimal retries (AI models take 30-60s to think)
    3. If rate limited, extract reset time and wait appropriately
    4. If all retries fail, try next model in priority list with substantial delay
    5. Continue until success or all models exhausted
    """
    sorted_models = sorted(FREE_MODELS, key=lambda x: x['priority'])

    for model_info in sorted_models:
        model_name = model_info['name']
        model_desc = model_info['description']
        llm = _get_llm(model_name)

        # Create chain for this model
        chain = _prompt | llm
        parse_chain = chain | (lambda msg: _parse_openrouter_response(msg))

        logger.info(f"Trying model: {model_name} ({model_desc})")

        for attempt in range(max_total_retries):
            try:
                # Apply global rate limiting before each request (AI models need time to think)
                await _openrouter_rate_limiter.wait_if_needed()

                logger.info(f"Attempt {attempt + 1}/{max_total_retries} with model {model_name}")
                logger.info(f"Starting AI processing (this may take 30-60 seconds)...")

                extraction: _ArticleExtraction = await parse_chain.ainvoke(
                    {
                        "title": article.title,
                        "source": article.source_name,
                        "published_at": article.published_at.isoformat(),
                        "topics": ", ".join(t.value for t in article.topics_matched),
                        "content": content_text,
                    }
                )

                logger.info(f"✅ Success with {model_name} (attempt {attempt + 1})")
                logger.info(f"Relevance score: {extraction.relevance_score:.2f}")
                return extraction

            except Exception as exc:
                error_msg = str(exc).lower()
                is_429 = '429' in error_msg or 'rate limit' in error_msg
                is_busy = any(keyword in error_msg for keyword in ['busy', 'overloaded'])
                is_network = any(keyword in error_msg for keyword in ['timeout', 'network', 'connection', 'dns'])

                logger.warning(f"Attempt {attempt + 1} failed with {model_name}: {exc}")

                # Extract rate limit reset time from 429 errors
                reset_time = None
                if is_429:
                    # Try to extract X-RateLimit-Reset from exception
                    if hasattr(exc, 'response') and hasattr(exc.response, 'headers'):
                        reset_header = exc.response.headers.get('X-RateLimit-Reset')
                        if reset_header:
                            reset_time = _parse_rate_limit_reset(reset_header)
                            if reset_time:
                                logger.warning(f"Rate limit detected, resets at {datetime.fromtimestamp(reset_time, tz=timezone.utc)}")

                if is_429 or is_busy:
                    # For rate limits and busy models, try next model immediately
                    # but also update global rate limiter if we got a reset time
                    if reset_time:
                        await _openrouter_rate_limiter.wait_if_needed(reset_time)
                    logger.info(f"Model {model_name} is rate-limited/busy, will try next model")
                    break  # Try next model instead of retrying the same one

                if is_network:
                    # Network errors deserve retry with backoff
                    backoff_time = min(2 ** attempt, 30)  # Max 30 seconds
                    logger.info(f"Network error, waiting {backoff_time}s before retry...")
                    await asyncio.sleep(backoff_time)
                    continue

                # Other errors - minimal retry with short backoff
                if attempt < max_total_retries - 1:
                    backoff_time = min(2 ** attempt, 5)  # Max 5 seconds for non-rate-limit errors
                    logger.info(f"Retrying in {backoff_time}s...")
                    await asyncio.sleep(backoff_time)

        # All retries failed for this model, try next one with substantial delay
        logger.warning(f"All retries exhausted for {model_name}, waiting 30s before next model...")
        await asyncio.sleep(30.0)  # Wait 30 seconds between model switches

    # All models failed
    logger.error(f"All {len(sorted_models)} models failed for article {article.article_id}")
    return None


async def _analyze_one(
    article: RawArticle,
    semaphore: asyncio.Semaphore,
) -> Optional[AnalyzedArticle]:
    """Run the LCEL chain on a single article with model fallbacks."""

    # Prepare content
    raw_content = " ".join(filter(None, [article.description, article.content]))

    if len(raw_content) > CONTENT_CHAR_LIMIT:
        content_text = raw_content[:CONTENT_CHAR_LIMIT]
        logger.debug(
            "Article content %d chars truncated to %d chars for analysis",
            len(raw_content),
            len(content_text)
        )
    else:
        content_text = raw_content or article.title

    # STREAMING DEBUG: Log article analysis start
    logger.info("=" * 60)
    logger.info("LLM ANALYSIS START: Article ID: %s", article.article_id)
    logger.info("LLM ANALYSIS START: Title: %s", article.title[:50])
    logger.info("LLM ANALYSIS START: Content length: %d chars", len(content_text))
    logger.info("LLM ANALYSIS START: Topics: %s", [t.value for t in article.topics_matched])
    logger.info("=" * 60)

    async with semaphore:
        logger.info(f"SEMAPHORE ACQUIRED: Processing article {article.article_id}")

        extraction = await _analyze_with_retry(article, content_text)

        if extraction is None:
            logger.warning(f"Failed to analyze article {article.article_id} after all model fallbacks")
            return None

    # Apply relevance filtering
    if extraction.relevance_score < RELEVANCE_THRESHOLD:
        logger.warning(
            "Article %s DROPPED - Relevance %.2f < threshold %.2f | Title: %s",
            article.article_id,
            extraction.relevance_score,
            RELEVANCE_THRESHOLD,
            article.title[:50],
        )
        return None
    else:
        logger.info(
            "Article %s PASSED - Relevance %.2f >= threshold %.2f | Title: %s",
            article.article_id,
            extraction.relevance_score,
            RELEVANCE_THRESHOLD,
            article.title[:50],
        )

    return AnalyzedArticle(
        article_id=article.article_id,
        title=article.title,
        url=article.url,
        source_name=article.source_name,
        published_at=article.published_at,
        topics=article.topics_matched,
        signal_type=extraction.signal_type,
        one_line_summary=extraction.one_line_summary,
        why_it_matters=extraction.why_it_matters,
        key_entities=extraction.key_entities,
        relevance_score=extraction.relevance_score,
    )


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


async def analyze_articles(articles: list[RawArticle]) -> list[AnalyzedArticle]:
    """
    Run OpenRouter analysis on a batch of raw articles.

    Args:
        articles: Raw articles from the fetcher.

    Returns:
        Analyzed articles with signal metadata, filtered by relevance threshold,
        sorted by relevance_score descending.
    """
    if not OPENROUTER_API_KEY or OPENROUTER_API_KEY == "your_openrouter_api_key_here":
        logger.error("OPENROUTER_API_KEY not set or is placeholder. Please set a valid API key in .env")
        return []

    logger.info("=" * 60)
    logger.info("ANALYZER START: Processing %d articles", len(articles))
    logger.info("ANALYZER CONFIG: MAX_CONCURRENT_LLM=%d, RELEVANCE_THRESHOLD=%.2f", MAX_CONCURRENT_LLM, RELEVANCE_THRESHOLD)
    logger.info("ANALYZER OPENROUTER: Using %d free models with fallback", len(FREE_MODELS))
    logger.info("ANALYZER MODELS: %s", [m['name'] for m in FREE_MODELS])
    logger.info("=" * 60)

    if not articles:
        logger.warning("ANALYZER: No articles to analyze")
        return []

    logger.info("ANALYZER: Creating semaphore with limit %d for concurrent LLM calls", MAX_CONCURRENT_LLM)
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_LLM)

    logger.info("ANALYZER: Creating %d analysis tasks...", len(articles))
    tasks = [_analyze_one(article, semaphore) for article in articles]

    logger.info("ANALYZER: Starting concurrent analysis with asyncio.gather...")
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=3600  # 60 minutes total for all articles (1 req/min × 20 articles × 2 min processing time)
        )
    except asyncio.TimeoutError:
        logger.error("ANALYZER: Timeout waiting for all analysis tasks to complete")
        results = []

    logger.info("ANALYZER: All analysis tasks completed")

    analyzed = [r for r in results if r is not None and not isinstance(r, Exception)]
    analyzed.sort(key=lambda a: a.relevance_score, reverse=True)

    logger.info(
        "ANALYZER: %d articles in → %d after relevance filtering",
        len(articles),
        len(analyzed),
    )

    # Log success rate by model if possible
    success_rate = len(analyzed) / len(articles) * 100 if articles else 0
    logger.info("ANALYZER: Success rate: %.1f%%", success_rate)

    return analyzed


# ---------------------------------------------------------------------------
# CLI smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import logging as _logging

    from agents.fetcher import fetch_articles

    _logging.basicConfig(level=logging.INFO)

    async def _smoke():
        raw = await fetch_articles([Topic.AI])
        print(f"Fetched {len(raw)} raw articles, analyzing top 2...")
        analyzed = await analyze_articles(raw[:2])
        for a in analyzed:
            print(json.dumps(a.model_dump(mode="json"), indent=2, default=str))
            print("---")

    asyncio.run(_smoke())
