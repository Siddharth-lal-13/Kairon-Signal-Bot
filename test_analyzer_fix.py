"""
Test the fixed analyzer with qwen3.5:4b thinking field support.

This test verifies that the analyzer can now successfully parse JSON output
from qwen3.5:4b model which puts its output in the 'thinking' field.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from dotenv import load_dotenv

# Override Ollama URL for local testing
os.environ["OLLAMA_BASE_URL"] = "http://localhost:11434"

from models.schemas import RawArticle, Topic
from agents.analyzer import analyze_articles

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

def create_test_article(article_id: str, title: str, source: str, description: str, content: str) -> RawArticle:
    """Create a test RawArticle for LLM analysis."""
    return RawArticle(
        article_id=article_id,
        title=title,
        url=f"https://example.com/articles/{article_id}",
        source_name=source,
        api_source="test",
        published_at=datetime.now(timezone.utc),
        topics_matched=[Topic.AI],
        description=description,
        content=content
    )

async def main():
    """Main test function."""
    print("=" * 70)
    print("FIXED ANALYZER TEST - qwen3.5:4b thinking field support")
    print("=" * 70)
    print()

    logger.info("Starting fixed analyzer test...")

    # Create realistic test articles
    test_articles = [
        create_test_article(
            "test_001",
            "OpenAI announces GPT-5 with improved reasoning capabilities",
            "TechCrunch",
            "OpenAI releases GPT-5 with enhanced reasoning",
            "OpenAI has officially announced the release of GPT-5, featuring significantly improved reasoning capabilities compared to previous versions. The new model demonstrates enhanced performance in complex problem-solving tasks and shows better understanding of nuanced instructions. Industry experts believe this advancement could revolutionize how AI assistants handle multi-step reasoning and decision-making processes."
        ),
        create_test_article(
            "test_002",
            "Anthropic raises $2B funding round for Claude development",
            "VentureBeat",
            "Anthropic secures major funding for AI research",
            "Anthropic, the AI company behind Claude, has successfully raised $2 billion in a Series C funding round led by major tech investors. The funding will be used to accelerate the development of more advanced AI systems and expand the company's research capabilities. This valuation makes Anthropic one of the most valuable AI startups globally."
        ),
        create_test_article(
            "test_003",
            "New study shows AI automation impacting 40% of jobs",
            "MIT Technology Review",
            "Research reveals AI automation workforce impact",
            "A comprehensive study from MIT researchers has found that AI automation could impact approximately 40% of current jobs over the next decade. The research highlights that while many jobs will be transformed rather than eliminated, significant workforce retraining and policy changes will be required. The study emphasizes the need for proactive adaptation strategies."
        ),
    ]

    logger.info(f"Created {len(test_articles)} realistic test articles for LLM analysis")

    # Test LLM analysis with fixed parser
    try:
        logger.info("Running LLM analysis on test articles with fixed parser...")
        analyzed = await analyze_articles(test_articles)

        logger.info(f"Analyzer completed successfully!")
        logger.info(f"Results: {len(analyzed)} articles analyzed out of {len(test_articles)} input articles")

        if analyzed:
            print("\n" + "=" * 70)
            print("SUCCESSFUL ANALYSIS RESULTS:")
            print("=" * 70)
            for i, article in enumerate(analyzed, 1):
                print(f"\n--- Article {i} ---")
                print(f"Title: {article.title}")
                print(f"Signal Type: {article.signal_type.value}")
                print(f"Relevance Score: {article.relevance_score:.2f}")
                print(f"Summary: {article.one_line_summary}")
                print(f"Why It Matters: {article.why_it_matters}")
                print(f"Key Entities: {', '.join(article.key_entities)}")
                print("-" * 70)

            # Test success criteria
            success_rate = len(analyzed) / len(test_articles)
            if success_rate >= 0.8:  # 80% success rate
                print("\n" + "=" * 70)
                print("✅ TEST PASSED!")
                print("=" * 70)
                print(f"Success rate: {success_rate*100:.1f}%")
                print("The LLM analyzer is working correctly with qwen3.5:4b!")
                print("JSON output is being properly parsed from the thinking field.")
                return True
            else:
                print("\n" + "=" * 70)
                print("⚠️  PARTIAL SUCCESS")
                print("=" * 70)
                print(f"Success rate: {success_rate*100:.1f}% (target: 80%)")
                print("Some articles failed analysis. Check logs for details.")
                return False
        else:
            print("\n" + "=" * 70)
            print("❌ TEST FAILED")
            print("=" * 70)
            print("No articles were successfully analyzed!")
            print("This indicates issues with LLM output parsing or connection.")
            return False

    except Exception as e:
        logger.error(f"LLM analysis failed with error: {e}", exc_info=True)
        print("\n" + "=" * 70)
        print("❌ TEST FAILED WITH ERROR")
        print("=" * 70)
        print(f"Error: {e}")
        print("Check the logs above for detailed error information.")
        return False

if __name__ == "__main__":
    success = asyncio.run(main())

    print("\n" + "=" * 70)
    print("TEST SUMMARY")
    print("=" * 70)
    if success:
        print("✅ The analyzer fix is working correctly!")
        print("Ready to notify user and mark issue as SOLVED.")
    else:
        print("❌ Further investigation needed.")
    print("=" * 70)
