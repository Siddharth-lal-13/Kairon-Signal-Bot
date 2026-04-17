"""
Kairon — Pipeline Agent (LangGraph Supervisor Pattern)

Replaces simple LCEL chain with intelligent multi-agent routing
using LangGraph state machine.

Architecture: supervisor pattern with three worker nodes.

State schema:
- user_id: int
- topics: list[Topic]
- raw_articles: list[RawArticle]
- analyzed_articles: list[AnalyzedArticle]
- briefing: Optional[Briefing]
- coverage_score: float  (0-1, set by analyzer)
- retry_count: int
- error: Optional[str]

Nodes:
1. fetch_node — calls fetch_articles() AND scrape_articles() concurrently,
   merges results into state.raw_articles. Uses asyncio.gather.

2. analyze_node — calls analyze_articles() on raw_articles, sets
   state.analyzed_articles and calculates state.coverage_score as
   (len(analyzed) / max(len(raw), 1)). This is the agent's
   independent decision point.

3. synthesize_node — calls synthesize_briefing(), sets state.briefing.
   If article_count == 0, sets error instead of crashing.

Supervisor/router logic (a conditional edge function):
- After fetch_node: if len(raw_articles) < 5 AND retry_count < 2,
  increment retry_count and loop back to fetch_node. Otherwise
  proceed to analyze_node.
- After analyze_node: if coverage_score < 0.3 AND retry_count < 2,
  increment retry_count, loop back to fetch_node for more data.
  Otherwise proceed to synthesize_node.
- After synthesize_node: END.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Annotated, Literal

from dotenv import load_dotenv
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from agents.analyzer import analyze_articles
from agents.fetcher import fetch_articles
from agents.scraper import scrape_articles
from storage.store import store_briefing_memory, get_user_feedback_summary
from agents.synthesizer import synthesize_briefing
from models.schemas import AnalyzedArticle, Briefing, RawArticle, Topic

load_dotenv()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State Schema
# ---------------------------------------------------------------------------


class PipelineState(TypedDict):
    """State for the LangGraph pipeline."""

    user_id: int
    topics: list[Topic]
    raw_articles: list[RawArticle]
    analyzed_articles: list[AnalyzedArticle]
    briefing: Briefing | None
    coverage_score: float
    retry_count: int
    error: str | None

# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


async def fetch_node(state: PipelineState) -> PipelineState:
    """
    Fetch articles from both APIs (NewsAPI + GNews) and scrapers
    (Hacker News + Ars Technica) concurrently, merge results.

    Uses asyncio.gather for concurrent execution of all 4 sources.
    """
    logger.info(
        "Fetch node: fetching articles for user %d (topics: %s), retry %d",
        state["user_id"],
        [t.value for t in state["topics"]],
        state["retry_count"],
    )

    try:
        # Run fetchers and scrapers concurrently (re-enabled for debugging)
        tasks = [
            fetch_articles(state["topics"]),
            scrape_articles(state["topics"]),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Combine results, handling failures gracefully
        all_articles: list[RawArticle] = []
        for result in results:
            if isinstance(result, Exception):
                logger.error("Article source failed in fetch_node: %s", result)
                continue
            # Debug: Check if result is valid before extending
            if result is None:
                logger.warning("Source returned None, skipping")
                continue
            if not isinstance(result, list):
                logger.error("Source returned non-iterable: %s (type: %s)", result, type(result))
                continue
            all_articles.extend(result)

        # Deduplicate by URL (keep first occurrence)
        seen_urls = set()
        deduped: list[RawArticle] = []

        for article in all_articles:
            if article.url not in seen_urls:
                seen_urls.add(article.url)
                deduped.append(article)

        # Update state
        state["raw_articles"] = deduped
        logger.info(
            "Fetch node: %d articles collected (retry %d)",
            len(deduped),
            state["retry_count"],
        )

        return state

    except Exception as exc:
        logger.error("Fetch node failed: %s", exc)
        state["error"] = str(exc)
        return state


async def analyze_node(state: PipelineState) -> PipelineState:
    """
    Analyze raw articles using LangChain LCEL chain with Ollama.

    Calculates coverage_score as (len(analyzed) / max(len(raw), 1))
    which represents the analyzer's independent decision point about data quality.
    """
    logger.info(
        "Analyze node: processing %d articles for user %d (retry %d)",
        len(state["raw_articles"]),
        state["user_id"],
        state["retry_count"],
    )

    try:
        # Analyze articles using existing analyzer agent
        analyzed = await analyze_articles(state["raw_articles"])

        # Calculate coverage score (analyzer's independent decision point)
        raw_count = len(state["raw_articles"])
        coverage = len(analyzed) / max(raw_count, 1) if raw_count > 0 else 0.0

        # Update state
        state["analyzed_articles"] = analyzed
        state["coverage_score"] = coverage

        logger.info(
            "Analyze node: %d/%d articles passed relevance (coverage: %.2f, retry %d)",
            len(analyzed),
            raw_count,
            coverage,
            state["retry_count"],
        )

        return state

    except Exception as exc:
        logger.error("Analyze node failed: %s", exc)
        state["error"] = str(exc)
        return state


async def synthesize_node(state: PipelineState) -> PipelineState:
    """
    Synthesize briefing using NVIDIA NIM API.

    Handles the case where article_count == 0 by setting error
    instead of crashing.
    """
    logger.info(
        "Synthesize node: creating briefing for user %d with %d articles",
        state["user_id"],
        len(state["analyzed_articles"]),
    )

    try:
        # Handle empty articles case
        if not state["analyzed_articles"]:
            logger.warning("Synthesize node: no articles to synthesize for user %d", state["user_id"])
            state["error"] = "No articles passed analysis threshold"
            return state

        # Get user feedback summary for personalization
        feedback = get_user_feedback_summary(state["user_id"])

        # Synthesize briefing using existing synthesizer agent
        briefing = synthesize_briefing(
            user_id=state["user_id"],
            topics=state["topics"],
            articles=state["analyzed_articles"],
            feedback_summary=feedback,
        )

        # Store briefing memory (Stage 1 stub)
        store_briefing_memory(briefing)

        # Update state
        state["briefing"] = briefing

        logger.info(
            "Synthesize node: briefing created for user %d (id: %s, %d chars)",
            state["user_id"],
            briefing.briefing_id,
            len(briefing.telegram_text),
        )

        return state

    except Exception as exc:
        logger.error("Synthesize node failed: %s", exc)
        state["error"] = str(exc)
        return state


# ---------------------------------------------------------------------------
# Conditional Edge Functions (Supervisor/Router Logic)
# ---------------------------------------------------------------------------


def should_retry_fetch(state: PipelineState) -> Literal["fetch", "analyze"]:
    """
    Supervisor decision after fetch_node:

    If len(raw_articles) < 5 AND retry_count < 2,
    increment retry_count and loop back to fetch_node.
    Otherwise proceed to analyze_node.
    """
    if state["error"]:
        # If there's an error, proceed to next node (don't retry)
        return "analyze"

    if len(state["raw_articles"]) < 5 and state["retry_count"] < 2:
        logger.info(
            "Supervisor: low article count (%d), retrying fetch (attempt %d)",
            len(state["raw_articles"]),
            state["retry_count"] + 1,
        )
        state["retry_count"] += 1
        return "fetch"

    return "analyze"


def should_retry_analyze(state: PipelineState) -> Literal["fetch", "synthesize"]:
    """
    Supervisor decision after analyze_node:

    If coverage_score < 0.3 AND retry_count < 2,
    increment retry_count and loop back to fetch_node for more data.
    Otherwise proceed to synthesize_node.
    """
    if state["error"]:
        # If there's an error, proceed to end
        return END

    if state["coverage_score"] < 0.3 and state["retry_count"] < 2:
        logger.info(
            "Supervisor: low coverage score (%.2f), retrying fetch (attempt %d)",
            state["coverage_score"],
            state["retry_count"] + 1,
        )
        state["retry_count"] += 1
        return "fetch"

    return "synthesize"


# ---------------------------------------------------------------------------
# Graph Construction
# ---------------------------------------------------------------------------


def build_pipeline() -> StateGraph:
    """
    Build the LangGraph pipeline with supervisor routing logic.

    Returns a compiled StateGraph ready for execution.
    """
    graph = StateGraph(PipelineState)

    # Add nodes
    graph.add_node("fetch", fetch_node)
    graph.add_node("analyze", analyze_node)
    graph.add_node("synthesize", synthesize_node)

    # Add conditional edges (supervisor routing)
    graph.set_entry_point("fetch")
    graph.add_conditional_edges("fetch", should_retry_fetch)
    graph.add_conditional_edges("analyze", should_retry_analyze)
    graph.add_edge("synthesize", END)

    return graph.compile()


# ---------------------------------------------------------------------------
# Public Interface
# ---------------------------------------------------------------------------


async def run_pipeline(user_id: int, topics: list[Topic]) -> tuple[Briefing, list[AnalyzedArticle]]:
    """
    Run the full LangGraph pipeline for one user.

    This replaces the sequential agent calls in webhook.py with
    intelligent multi-agent routing that can retry and self-heal.

    Args:
        user_id: Telegram chat_id of the recipient.
        topics: Topics the user is subscribed to.

    Returns:
        A tuple of (Briefing, analyzed_articles) where Briefing has
        telegram_text ready for delivery and analyzed_articles contains
        the analyzed articles used to generate the briefing.

    Raises:
        RuntimeError: If pipeline fails and no briefing can be generated.
    """
    # Initialize state
    initial_state: PipelineState = {
        "user_id": user_id,
        "topics": topics,
        "raw_articles": [],
        "analyzed_articles": [],
        "briefing": None,
        "coverage_score": 0.0,
        "retry_count": 0,
        "error": None,
    }

    # Build and run graph
    graph = build_pipeline()

    logger.info(
        "Starting LangGraph pipeline for user %d (topics: %s)",
        user_id,
        [t.value for t in topics],
    )

    try:
        # Run the graph
        final_state = await graph.ainvoke(initial_state)

        # Check for errors
        if final_state["error"]:
            raise RuntimeError(f"Pipeline failed: {final_state['error']}")

        # Check for successful briefing
        if not final_state["briefing"]:
            raise RuntimeError("Pipeline completed but no briefing was generated")

        briefing = final_state["briefing"]

        logger.info(
            "Pipeline completed successfully for user %d: briefing %s",
            user_id,
            briefing.briefing_id,
        )

        return (briefing, final_state["analyzed_articles"])

    except Exception as exc:
        logger.error("Pipeline execution failed for user %d: %s", user_id, exc)
        raise RuntimeError(f"Pipeline execution failed: {exc}") from exc


# ---------------------------------------------------------------------------
# CLI smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import asyncio
    import json
    import logging as _logging

    from agents.analyzer import analyze_articles
    from agents.fetcher import fetch_articles
    from agents.scraper import scrape_articles
    from agents.synthesizer import synthesize_briefing

    _logging.basicConfig(level=logging.INFO)

    async def _smoke():
        """Test the pipeline with a sample user and topics."""
        # Test with single topic for faster execution
        test_user_id = 999999
        test_topics = [Topic.AI]

        print(f"Starting pipeline smoke test for user {test_user_id} with topics: {[t.value for t in test_topics]}")

        try:
            # Fetch and scrape articles
            print("Fetching articles...")
            api_articles = await fetch_articles(test_topics)
            scraped_articles = await scrape_articles(test_topics)

            # Merge results
            all_articles = api_articles + scraped_articles
            # Slice to max 3 for smoke test only
            all_articles = all_articles[:3]
            print(f"Using {len(all_articles)} articles for smoke test (sliced from total fetched)")

            # Analyze articles
            print("Analyzing articles...")
            analyzed = await analyze_articles(all_articles)

            if not analyzed:
                print("❌ No articles passed analysis threshold")
                return

            print(f"Analyzed {len(analyzed)} articles")

            # Synthesize briefing
            print("Synthesizing briefing...")
            briefing = synthesize_briefing(
                user_id=test_user_id,
                topics=test_topics,
                articles=analyzed,
            )

            print(f"\n✅ Smoke test successful!")
            print(f"Briefing ID: {briefing.briefing_id}")
            print(f"Topics covered: {[t.value for t in briefing.topics_covered]}")
            print(f"Article count: {briefing.article_count}")
            print(f"Text length: {len(briefing.telegram_text)} chars")
            print("\n--- Briefing Preview (first 500 chars) ---")
            print(briefing.telegram_text[:500] + "...")

        except Exception as exc:
            print(f"\n❌ Smoke test failed: {exc}")
            import traceback
            traceback.print_exc()

    asyncio.run(_smoke())