"""
Simple direct test to verify Ollama connectivity and LLM response.

This bypasses all the complex pipeline code and directly tests:
1. Ollama API connectivity
2. Basic LLM response
3. JSON output parsing
"""

import requests
import json
from dotenv import load_dotenv
import os

# Load environment variables
load_dotenv()

# Override for local testing
os.environ["OLLAMA_BASE_URL"] = "http://localhost:11434"
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL")

print(f"Testing Ollama connection at: {OLLAMA_BASE_URL}")
print(f"Using model: {OLLAMA_MODEL}")
print("-" * 70)

# Test 1: Check Ollama is running
print("\nTest 1: Checking Ollama status...")
try:
    response = requests.get(f"{OLLAMA_BASE_URL}/api/tags")
    if response.status_code == 200:
        models = response.json()
        print(f"SUCCESS Ollama is running! Found {len(models['models'])} model(s)")
        for model in models['models']:
            print(f"   - {model['name']}")
    else:
        print(f"ERROR Ollama returned status {response.status_code}")
        exit(1)
except Exception as e:
    print(f"ERROR Failed to connect to Ollama: {e}")
    exit(1)

# Test 2: Test basic LLM call
print("\nTest Test 2: Testing basic LLM call...")
test_prompt = "You are a helpful assistant. Respond with 'Hello' in JSON format like: {\"response\": \"Hello\"}"

try:
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": test_prompt,
        "stream": False,
        "format": "json"
    }

    response = requests.post(f"{OLLAMA_BASE_URL}/api/generate", json=payload)
    if response.status_code == 200:
        result = response.json()
        print(f"SUCCESS LLM responded successfully!")
        print(f"Full response keys: {result.keys()}")
        print(f"Response text: '{result['response']}'")
        print(f"Response length: {len(result['response'])}")

        # Check if thinking field has content
        if 'thinking' in result and result['thinking']:
            print(f"Thinking field present: {result['thinking'][:200]}...")

        # Test if response contains valid JSON
        try:
            json_data = json.loads(result['response'])
            print(f"SUCCESS Response is valid JSON: {json_data}")
        except json.JSONDecodeError:
            print(f"WARNING  Response is NOT valid JSON (contains extra text)")
            print(f"Raw response: {result['response'][:500]}")
    else:
        print(f"ERROR LLM call failed with status {response.status_code}")
        print(f"Error: {response.text}")
        exit(1)

except Exception as e:
    print(f"ERROR LLM call failed: {e}")
    exit(1)

# Test 3: Test article analysis with realistic prompt
print("\nTest Test 3: Testing article analysis prompt...")

system_prompt = """You are a strict data extraction API. You will receive a news article and extract structured intelligence from it.

CRITICAL REQUIREMENTS - READ CAREFULLY:
- Return ONLY valid JSON - no markdown, no explanations, no thinking process
- Start your response immediately with a curly brace opening and end with a curly brace closing
- Do NOT include any text before or after the JSON
- Do NOT include "Thinking Process" or any other non-JSON content
- Do NOT use markdown formatting or code blocks
- Output must be parseable as raw JSON with no extra characters

Follow the schema exactly: signal_type, one_line_summary, why_it_matters, key_entities, relevance_score.

Output JSON format:
{
  "signal_type": "product_launch",
  "one_line_summary": "One sentence (≤25 words) summarising what happened",
  "why_it_matters": "One sentence (≤40 words) explaining the signal's significance",
  "key_entities": ["entity1", "entity2", "entity3", "entity4", "entity5"],
  "relevance_score": 0.9
}

Remember: ONLY valid JSON, nothing else."""

article_content = """Article title: OpenAI announces GPT-5 with improved reasoning capabilities
Source: TechCrunch
Published: 2026-04-17T01:00:00Z
Topics matched: ai

Content:
OpenAI has officially announced the release of GPT-5, featuring significantly improved reasoning capabilities compared to previous versions. The new model demonstrates enhanced performance in complex problem-solving tasks and shows better understanding of nuanced instructions. Industry experts believe this advancement could revolutionize how AI assistants handle multi-step reasoning and decision-making processes."""

try:
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": f"{system_prompt}\n\n{article_content}\n\nExtract the signal. Respond only with the JSON object.",
        "stream": False,
        "format": "json",
        "temperature": 0.1,
        "top_p": 0.9,
        "max_tokens": 512
    }

    response = requests.post(f"{OLLAMA_BASE_URL}/api/generate", json=payload)
    if response.status_code == 200:
        result = response.json()
        print(f"Full response keys: {result.keys()}")
        print(f"Done: {result.get('done', False)}")

        if 'response' not in result:
            print(f"ERROR No 'response' key in result!")
            print(f"Full result: {result}")
            exit(1)

        llm_response = result['response'].strip()

        print(f"SUCCESS LLM responded to article analysis!")
        print(f"Response length: {len(llm_response)} characters")
        print(f"Raw response: '{llm_response}'")

        # Check if thinking field has content
        if 'thinking' in result and result['thinking']:
            print(f"\nTHINKING FIELD CONTENT:")
            print("=" * 70)
            print(result['thinking'])
            print("=" * 70)

            # Try to parse JSON from thinking field
            try:
                thinking_json = json.loads(result['thinking'])
                print(f"\nFIX APPLIED: Successfully parsed JSON from thinking field!")
                print(json.dumps(thinking_json, indent=2))
            except json.JSONDecodeError:
                print(f"WARNING: Thinking field is not valid JSON")

        print(f"\nOUTPUT Raw LLM Response:")
        print("=" * 70)
        print(llm_response)
        print("=" * 70)

        # Test JSON parsing
        try:
            parsed_json = json.loads(llm_response)
            print(f"\nSUCCESS SUCCESS! Valid JSON parsed successfully:")
            print(json.dumps(parsed_json, indent=2))

            # Check if required fields are present
            required_fields = ["signal_type", "one_line_summary", "why_it_matters", "key_entities", "relevance_score"]
            missing_fields = [field for field in required_fields if field not in parsed_json]

            if missing_fields:
                print(f"\nWARNING  Missing fields: {missing_fields}")
            else:
                print(f"\nSUCCESS All required fields present!")

        except json.JSONDecodeError as e:
            print(f"\nERROR JSON parsing failed: {e}")
            print(f"Response starts with: {llm_response[:100]}")
            print(f"Response ends with: {llm_response[-100:]}")

            # Try to extract JSON if there's extra text
            import re
            json_match = re.search(r'\{.*\}', llm_response, re.DOTALL)
            if json_match:
                print(f"\nFIX Found potential JSON in response: {json_match.group()}")
                try:
                    extracted = json.loads(json_match.group())
                    print(f"SUCCESS Extracted JSON successfully!")
                    print(json.dumps(extracted, indent=2))
                except json.JSONDecodeError:
                    print(f"ERROR Extracted content is still not valid JSON")
    else:
        print(f"ERROR Article analysis failed with status {response.status_code}")
        print(f"Error: {response.text}")
        exit(1)

except Exception as e:
    print(f"ERROR Article analysis failed: {e}")
    import traceback
    traceback.print_exc()
    exit(1)

print("\n" + "=" * 70)
print("SUCCESS OLLAMA TEST COMPLETED")
print("=" * 70)
print("If you see valid JSON output above, the LLM is working correctly!")
print("If you see JSON parsing errors, the prompt needs adjustment.")