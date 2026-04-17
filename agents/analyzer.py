"""
Kairon — Analyzer Agent

Takes a list of RawArticle objects and runs each through a LangChain LCEL
chain backed by qwen3:4b via Ollama (local inference).  Returns a list of
AnalyzedArticle objects with LLM-extracted signal metadata.

Design decisions:
  - LCEL chain: prompt | llm | output_parser  (no agents, no tools — fast)
  - JSON mode via PydanticOutputParser for structured extraction
  - Ollama is called via langchain_ollama.ChatOllama (local, free, 4GB VRAM safe)
  - Articles are processed in a semaphore-guarded async loop to avoid OOM on
    the RTX 3050 4GB.  Concurrency is configurable via MAX_CONCURRENT_LLM env.
  - Articles with very thin content (title + description < 80 chars) are
    given a best-effort summary without penalising the whole run.
  - Relevance scoring is done by the LLM; articles below RELEVANCE_THRESHOLD
    are dropped before returning so the synthesizer doesn't have to filter.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_ollama import ChatOllama
from pydantic import BaseModel, Field

from models.schemas import AnalyzedArticle, RawArticle, SignalType, Topic

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL")
MAX_CONCURRENT_LLM: int = int(os.getenv("MAX_CONCURRENT_LLM", "2"))
RELEVANCE_THRESHOLD: float = float(os.getenv("RELEVANCE_THRESHOLD", "0.4"))

# How many characters of article content to send to the LLM.
# Free-tier APIs often truncate at 200 chars; we cap at 800 to stay lean.
CONTENT_CHAR_LIMIT = 800


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
You are a strict data extraction API. You will receive a news article and extract structured intelligence from it.

CRITICAL REQUIREMENTS - READ CAREFULLY:
- Return ONLY valid JSON - no markdown, no explanations, no thinking process
- Start your response immediately with a curly brace opening and end with a curly brace closing
- Do NOT include any text before or after the JSON
- Do NOT include "Thinking Process" or any other non-JSON content
- Do NOT use markdown formatting or code blocks
- Output must be parseable as raw JSON with no extra characters

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

_llm = ChatOllama(
    model=OLLAMA_MODEL,
    base_url=OLLAMA_BASE_URL,
    temperature=0.1,        # low temperature for consistent structured output
    format="json",          # Ollama JSON mode — reduces hallucinated markdown
    top_p=0.9,            # Slightly reduce randomness
    max_tokens=512,         # Limit output length to reduce chance of extra content
)

# Custom parser to handle qwen3.5:4b thinking field behavior
from langchain_core.runnables import RunnableLambda

def _parse_ollama_response(message) -> _ArticleExtraction:
    """
    Parse Ollama response, checking both response and thinking fields.

    qwen3.5:4b puts structured output in the 'thinking' field instead of 'response'.
    """
    import json

    # Extract content from AIMessage object
    if hasattr(message, 'content'):
        text = message.content
    else:
        text = str(message)

    # DEBUG: Log the full message structure to understand Docker environment
    logger.debug(f"Received message type: {type(message)}")
    logger.debug(f"Message content: '{text}'")
    if hasattr(message, 'response_metadata'):
        logger.debug(f"Response metadata: {message.response_metadata}")
    if hasattr(message, 'additional_kwargs'):
        logger.debug(f"Additional kwargs: {message.additional_kwargs}")
    if hasattr(message, 'usage_metadata'):
        logger.debug(f"Usage metadata: {message.usage_metadata}")

    # First, try to extract JSON from thinking field if present
    try:
        parsed = json.loads(text)
        if 'thinking' in parsed and parsed['thinking']:
            thinking_content = parsed['thinking']
            # Extract JSON if thinking contains structured data
            if isinstance(thinking_content, dict):
                # Direct JSON object in thinking field
                logger.debug(f"Found direct dict in thinking field")
                return _ArticleExtraction(**thinking_content)
            elif isinstance(thinking_content, str):
                # String in thinking field - try to parse as JSON
                logger.debug(f"Found string in thinking field, attempting JSON parse")
                thinking_json = json.loads(thinking_content)
                return _ArticleExtraction(**thinking_json)
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.debug(f"Failed to parse thinking field: {e}")

    # Try to check if the content itself is valid JSON
    try:
        if text and text.strip():
            json_data = json.loads(text)
            if isinstance(json_data, dict) and 'signal_type' in json_data:
                logger.debug(f"Found valid JSON directly in content")
                return _ArticleExtraction(**json_data)
    except (json.JSONDecodeError, ValueError) as e:
        logger.debug(f"Content is not valid JSON: {e}")

    # Try to extract JSON from response_metadata (Docker environment specific)
    if hasattr(message, 'response_metadata') and message.response_metadata:
        try:
            metadata_text = str(message.response_metadata)
            if metadata_text and metadata_text.strip():
                # Try to find JSON in metadata
                import re
                json_match = re.search(r'\{.*\}', metadata_text, re.DOTALL)
                if json_match:
                    json_data = json.loads(json_match.group())
                    if 'signal_type' in json_data:
                        logger.debug(f"Found JSON in response_metadata")
                        return _ArticleExtraction(**json_data)
        except Exception as e:
            logger.debug(f"Failed to parse response_metadata: {e}")

    # If all else fails, try standard parsing
    try:
        return _parser.parse(text)
    except Exception as e:
        logger.debug(f"Standard parsing failed: {e}")
        raise

# Create a custom chain that handles qwen3.5:4b's thinking field
_llm_chain = _prompt | _llm
_custom_parser_chain = RunnableLambda(_parse_ollama_response)
_chain = _llm_chain | _custom_parser_chain


# ---------------------------------------------------------------------------
# Per-article analysis
# ---------------------------------------------------------------------------


async def _analyze_one(
    article: RawArticle,
    semaphore: asyncio.Semaphore,
) -> Optional[AnalyzedArticle]:
    """Run the LCEL chain on a single article.  Returns None on failure."""
    content_text = " ".join(
        filter(
            None,
            [article.description, article.content],
        )
    )[:CONTENT_CHAR_LIMIT] or article.title

    async with semaphore:
        max_retries = 3
        for attempt in range(max_retries):
            try:
                extraction: _ArticleExtraction = await _chain.ainvoke(
                    {
                        "title": article.title,
                        "source": article.source_name,
                        "published_at": article.published_at.isoformat(),
                        "topics": ", ".join(t.value for t in article.topics_matched),
                        "content": content_text,
                    }
                )
                break  # Success, exit retry loop
            except Exception as exc:
                if attempt == max_retries - 1:
                    logger.warning(
                        "LLM analysis failed for article %s (%s) after %d retries: %s - Skipping article",
                        article.article_id,
                        article.title[:60],
                        max_retries,
                        str(exc),
                    )
                    return None
                # Exponential backoff
                await asyncio.sleep(2 ** attempt)

    if extraction.relevance_score < RELEVANCE_THRESHOLD:
        logger.debug(
            "Article %s dropped (relevance %.2f < threshold %.2f)",
            article.article_id,
            extraction.relevance_score,
            RELEVANCE_THRESHOLD,
        )
        return None

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
    Run LangChain/Ollama analysis on a batch of raw articles.

    Args:
        articles: Raw articles from the fetcher.

    Returns:
        Analyzed articles with signal metadata, filtered by relevance threshold,
        sorted by relevance_score descending.
    """
    if not articles:
        return []

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_LLM)
    tasks = [_analyze_one(article, semaphore) for article in articles]
    results = await asyncio.gather(*tasks)

    analyzed = [r for r in results if r is not None]
    analyzed.sort(key=lambda a: a.relevance_score, reverse=True)

    logger.info(
        "Analyzer: %d articles in → %d after relevance filtering",
        len(articles),
        len(analyzed),
    )
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
