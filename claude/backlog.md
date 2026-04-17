# Kairon Upgrade Backlog

## Blockers & Issues

### Upgrade 1: crawl4ai scraper agent
- **Status**: ✅ **COMPLETED**
- **Resolved Issues**:
  - crawl4ai installation: Successfully installed with all dependencies
  - Windows character encoding: Fixed by adding `sys.stdout` codec wrapper
  - HTML parsing: Fixed by using `result.cleaned_html` instead of `result.html`
  - Pydantic validation: Fixed by using `datetime.now(timezone.utc)` instead of `None` for published_at
  - Topic matching too strict: Fixed by falling back to `Topic.TECH` instead of skipping articles
  - Windows ResourceWarning noise: Fixed with `warnings.filterwarnings("ignore", category=ResourceWarning)`
- **Test Results**:
  - Hacker News: ✅ 20 articles successfully scraped
  - Ars Technica: ✅ 15 articles successfully scraped
  - Total after deduplication: ✅ 31 articles
  - Article quality: ✅ Properly formatted with metadata (points, comments, URLs)
  - Performance: ✅ ~2 seconds per scraper, total ~4 seconds
- **Dependencies**: crawl4ai>=0.4 ✅ installed

### Upgrade 2: LangGraph supervisor pattern
- **Status**: ✅ **COMPLETED**
- **Implementation**:
  - ✅ Created agents/pipeline.py with LangGraph supervisor pattern
  - ✅ Implemented 3 nodes (fetch, analyze, synthesize) with state management
  - ✅ Added supervisor/router logic with conditional edges and retry mechanisms
  - ✅ Integrated multi-agent routing replacing sequential pipeline
  - ✅ Updated api/webhook.py to use unified pipeline
  - ✅ Created agents/__init__.py with proper exports
  - **Test Status**: Basic implementation complete, full integration testing needed
- **Dependencies**: langgraph>=0.2 ✅ installed

### Upgrade 3: MemPalace integration hooks (Stage 1)

### Upgrade 3: MemPalace integration hooks (Stage 1)
- **Status**: Not started
- **Potential Issues**:
  - Understanding MemPalace wing/room structure
  - Ensuring Stage 1 hooks don't accidentally call MemPalace
  - Proper error handling for missing mempalace_wing
- **Dependencies**: None (Stage 1 is stub implementation)

### Upgrade 4: Documentation updates
- **Status**: Not started
- **Potential Issues**:
  - Keeping documentation consistent with code changes
  - Architecture diagram updates
  - Ensuring all references are updated
- **Dependencies**: Completion of previous upgrades

## Resolved Issues

### Environment Setup (Initial Setup)
- **Status**: ✅ Resolved
- **Issue**: ModuleNotFoundError for langchain_core
- **Resolution**: Added load_dotenv() to analyzer.py and synthesizer.py
- **Lessons**: Always ensure .env loading at module level

### Testing Speed Optimization (Initial Setup)
- **Status**: ✅ Resolved
- **Issue**: Pipeline taking too long due to large API requests
- **Resolution**: Reduced API request sizes (NewsAPI: 20→5, GNews: 10→3)
- **Lessons**: Optimize request sizes for development/testing

### Ollama Model Issues (Initial Setup)
- **Status**: ⚠️ Partially Resolved
- **Issue**: qwen3.5:4b producing invalid JSON output
- **Resolution**: Added graceful error handling, but model still has JSON issues
- **Workaround**: Accept that some articles will fail analysis
- **Future**: Consider switching to llama3.2 for better JSON output

### GNews Rate Limiting (Initial Setup)
- **Status**: ✅ Resolved
- **Issue**: 429 Too Many Requests errors from GNews
- **Resolution**: Reduced request frequency and size, added error handling
- **Lessons**: Free tier APIs have strict rate limits

---

## ✅ ALL UPGRADES COMPLETED ✅

### Upgrade 1 - Web Scraping Agent (COMPLETED) ✅
- ✅ Created agents/scraper.py with crawl4ai integration
- ✅ Hacker News scraper: 20 articles with engagement metrics
- ✅ Ars Technica scraper: 15 articles with front page headlines
- ✅ Fixed Windows encoding, HTML parsing, datetime validation
- ✅ Performance: ~4 seconds total scraping time
- ✅ Requirements updated with crawl4ai>=0.4

### Upgrade 2 - LangGraph Supervisor Pattern (COMPLETED) ✅
- ✅ Created agents/pipeline.py with supervisor routing
- ✅ Implemented 3 nodes (fetch, analyze, synthesize)
- ✅ Added retry logic and conditional edge functions
- ✅ Updated api/webhook.py to use unified pipeline
- ✅ Created agents/__init__.py with proper exports
- ✅ Requirements updated with langgraph>=0.2

### Upgrade 3 - MemPalace Integration Hooks (COMPLETED) ✅
- ✅ Added init_user_wing() to storage/store.py
- ✅ Added store_briefing_memory() to storage/store.py
- ✅ Updated bot/telegram_bot.py cmd_start to initialize wings
- ✅ Updated pipeline.py synthesize_node to call store_briefing_memory
- ✅ MemPalace dependency in requirements.txt (commented for Stage 2)

### Upgrade 4 - Documentation Updates (COMPLETED) ✅
- ✅ Updated README.md architecture to LangGraph Pipeline
- ✅ Added "Data Sources" section listing all 4 sources
- ✅ Updated tech stack table with LangGraph and Web Scraping rows
- ✅ Updated Stage 2 roadmap with MemPalace integration note
- ✅ Updated project structure to reflect new modules
- ✅ Updated model references to correct versions

### Summary
All four targeted upgrades have been successfully implemented:
1. ✅ **Data Source Expansion**: From 2 APIs to 4 sources (NewsAPI, GNews, HN, Ars)
2. ✅ **Intelligent Multi-Agent System**: LangGraph supervisor pattern with retry logic
3. ✅ **Persistent Memory Foundation**: MemPalace hooks ready for Stage 2
4. ✅ **Comprehensive Documentation**: All README sections updated to reflect new capabilities

### Remaining Work
- Integration testing of LangGraph pipeline (full end-to-end)
- Performance optimization and load testing
- Stage 2 MemPalace activation
- Production deployment and monitoring setup

---

## Known Limitations (Stage 1)

### Data Quality
- **JSON Parsing**: qwen3.5:4b sometimes produces invalid JSON
- **Article Quality**: Free-tier APIs may provide truncated content
- **Deduplication**: URL-based dedup may miss semantic duplicates

### Performance
- **Sequential Processing**: Current pipeline doesn't parallelize optimally
- **LLM Latency**: Local Ollama inference is slower than cloud APIs
- **API Rate Limits**: Free tiers constrain data volume

### Architecture
- **No Retry Logic**: Single failure points can kill entire pipeline
- **No Coverage Monitoring**: Can't tell if results are too sparse
- **No Personalization**: All users get identical briefings for same topics

---

## Future Enhancements (Beyond Current Scope)

### Advanced Features
- Real-time breaking news alerts
- Multi-language support
- Custom topic creation by users
- Source credibility scoring
- Sentiment analysis integration

### Performance Optimizations
- Caching layer for API responses
- Batch processing improvements
- Incremental updates (only new articles)
- LLM output streaming

### Infrastructure
- PostgreSQL migration tools
- Monitoring and alerting
- A/B testing framework
- User analytics dashboard

---

## Dependencies & Installation Notes

### New Dependencies Required
- `crawl4ai>=0.4` (Upgrade 1)
- `langgraph>=0.2` (Upgrade 2)
- `mempalace>=3.1,<4.0` (Upgrade 3 - Stage 2 only)

### Installation Commands
```bash
# For crawl4ai
pip install -U crawl4ai
crawl4ai-setup
crawl4ai-doctor

# For playwright (if needed)
python -m playwright install --with-deps chromium

# For langgraph
pip install langgraph

# For mempalace (Stage 2)
pip install mempalace
mempalace init ~/projects/kairon
```

---

## Testing Strategy

### Unit Tests (Future)
- Individual scraper tests
- Agent node tests
- State machine tests
- Storage function tests

### Integration Tests (Future)
- End-to-end pipeline tests
- Error recovery tests
- Multi-user concurrency tests
- Rate limiting tests

### Manual Tests (Current)
- Smoke-tests in each module (__main__)
- Manual trigger testing via /trigger endpoint
- Telegram command testing
- Docker compose deployment testing

---

## Deployment Considerations

### Production Readiness
- **Current**: Development/testing configuration
- **Production Needed**:
  - Proper error monitoring
  - Rate limiting and backoff strategies
  - Logging infrastructure
  - Backup and recovery procedures

### Scaling Considerations
- **Current**: Single-user or small-group usage
- **Production Needed**:
  - Horizontal scaling capabilities
  - Load balancing
  - Database connection pooling
  - Caching strategies

---

## Last Updated
- **Date**: 2026-04-13
- **Status**: Ready to begin Upgrade 1 implementation
- **Next Action**: Create agents/scraper.py with crawl4ai integration