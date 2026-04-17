"""
Quick test for analyzer fix with just one article to verify the thinking field handling.
"""

import asyncio
import os
import sys
from datetime import datetime, timezone

# Override Ollama URL for local testing
os.environ["OLLAMA_BASE_URL"] = "http://localhost:11434"

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models.schemas import RawArticle, Topic
from agents.analyzer import analyze_articles

async def main():
    """Quick test with one article."""
    print("=" * 70)
    print("QUICK ANALYZER TEST - Single Article")
    print("=" * 70)
    print()

    # Create one realistic test article
    test_article = RawArticle(
        article_id="quick_test_001",
        title="OpenAI announces GPT-5 with improved reasoning capabilities",
        url="https://techcrunch.com/2024/04/17/openai-gpt5-reasoning",
        source_name="TechCrunch",
        api_source="test",
        published_at=datetime.now(timezone.utc),
        topics_matched=[Topic.AI],
        description="OpenAI releases GPT-5 with enhanced reasoning",
        content="OpenAI has officially announced the release of GPT-5, featuring significantly improved reasoning capabilities compared to previous versions. The new model demonstrates enhanced performance in complex problem-solving tasks and shows better understanding of nuanced instructions."
    )

    print(f"Testing with article: {test_article.title}")
    print()

    try:
        print("Running LLM analysis...")
        analyzed = await analyze_articles([test_article])

        if analyzed:
            article = analyzed[0]
            print("=" * 70)
            print("SUCCESS! Article analyzed successfully:")
            print("=" * 70)
            print(f"Title: {article.title}")
            print(f"Signal Type: {article.signal_type.value}")
            print(f"Relevance Score: {article.relevance_score:.2f}")
            print(f"Summary: {article.one_line_summary}")
            print(f"Why It Matters: {article.why_it_matters}")
            print(f"Key Entities: {', '.join(article.key_entities)}")
            print("=" * 70)
            print("\n✅ ANALYZER FIX WORKING CORRECTLY!")
            return True
        else:
            print("❌ No articles were successfully analyzed")
            print("This indicates the fix may need adjustment")
            return False

    except Exception as e:
        print(f"❌ Error during analysis: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = asyncio.run(main())
    print(f"\nFinal result: {'SUCCESS' if success else 'FAILED'}")
    sys.exit(0 if success else 1)
