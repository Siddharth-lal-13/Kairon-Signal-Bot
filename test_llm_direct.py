"""
Direct LLM Test Script for Kairon Analyzer

Tests the LLM analysis functionality without Docker by directly running
the analyzer agent with a sample article. This allows for rapid iteration
and debugging of LLM output parsing issues.
"""

import asyncio
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv
from models.schemas import RawArticle, Topic, SignalType
from agents.analyzer import analyze_articles

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Override OLLAMA_BASE_URL for local testing (not Docker)
import os
os.environ["OLLAMA_BASE_URL"] = "http://localhost:11434"

def create_test_article(article_id: str, title: str, source: str) -> RawArticle:
    """Create a test RawArticle for LLM analysis."""
    return RawArticle(
        article_id=article_id,
        title=title,
        url=f"https://example.com/articles/{article_id}",
        source_name=source,
        api_source="test",  # Required field
        published_at=datetime.now(timezone.utc),
        topics_matched=[Topic.AI],
        description="Test article for LLM analysis",
        content="This is a test article to verify LLM JSON output parsing capabilities. The article discusses artificial intelligence developments and should be analyzed for signal type, relevance, and key entities."
    )

async def main():
    """Main test function."""
    logger.info("Starting direct LLM test...")

    # Create test articles
    test_articles = [
        create_test_article(
            "test_001",
            "OpenAI announces GPT-5 with improved reasoning capabilities",
            "TechCrunch"
        ),
        create_test_article(
            "test_002",
            "Anthropic raises $2B funding round for Claude development",
            "VentureBeat"
        ),
        create_test_article(
            "test_003",
            "New study shows AI automation impacting 40% of jobs",
            "MIT Technology Review"
        ),
    ]

    logger.info(f"Created {len(test_articles)} test articles for LLM analysis")

    # Test LLM analysis
    try:
        logger.info("Running LLM analysis on test articles...")
        analyzed = await analyze_articles(test_articles)

        logger.info(f"✅ LLM analysis completed successfully!")
        logger.info(f"📊 Results: {len(analyzed)} articles analyzed out of {len(test_articles)} input articles")

        if analyzed:
            logger.info("\n🔍 Analysis Results:")
            for i, article in enumerate(analyzed, 1):
                logger.info(f"\n--- Article {i} ---")
                logger.info(f"Title: {article.title}")
                logger.info(f"Signal Type: {article.signal_type.value}")
                logger.info(f"Relevance Score: {article.relevance_score:.2f}")
                logger.info(f"Summary: {article.one_line_summary}")
                logger.info(f"Why It Matters: {article.why_it_matters}")
                logger.info(f"Key Entities: {', '.join(article.key_entities)}")
        else:
            logger.warning("⚠️  No articles passed the relevance threshold!")
            logger.warning("This may indicate issues with LLM output or relevance filtering")

        # Test success criteria
        if len(analyzed) >= len(test_articles) * 0.5:  # At least 50% success rate
            logger.info("\n✅ SUCCESS: LLM analysis is working correctly!")
            logger.info("The LLM is producing valid JSON output and parsing is successful.")
            return True
        else:
            logger.warning("\n⚠️  PARTIAL SUCCESS: LLM analysis completed but with low success rate")
            logger.warning(f"Expected at least {len(test_articles) * 0.5} articles, got {len(analyzed)}")
            return False

    except Exception as e:
        logger.error(f"❌ LLM analysis failed with error: {e}", exc_info=True)
        logger.error("This indicates a critical issue with LLM output parsing or configuration")
        return False

if __name__ == "__main__":
    print("=" * 70)
    print("KAIRON LLM DIRECT TEST")
    print("=" * 70)
    print("This test directly runs the LLM analyzer without Docker")
    print("It will test JSON output parsing and article analysis")
    print("=" * 70)
    print()

    success = asyncio.run(main())

    print()
    print("=" * 70)
    if success:
        print("✅ LLM TEST PASSED - Ready to notify user")
    else:
        print("❌ LLM TEST FAILED - Issues detected")
    print("=" * 70)
