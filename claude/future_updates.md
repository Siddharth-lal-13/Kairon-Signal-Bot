# Kairon Future Updates & Ideas

## Upgrade 5: Feedback Loop with Inline Buttons

### Overview
Users provide feedback on briefing articles via inline keyboard buttons (👍 More like this, 👎 Less like this, 🔍 Deep dive). Feedback is stored and injected into synthesis prompt on future briefings, personalizing coverage over time.

### Implementation Status
- [ ] Part A: New storage file and functions
- [ ] Part B: Telegram bot — inline buttons and callback handler
- [ ] Part C: Personalized synthesis
- [ ] Part D: Wire it into the pipeline
- [ ] Part E: Update .gitignore and README

### Key Features
- **Inline Feedback**: Each briefing section includes interactive buttons for user engagement
- **Persistent Memory**: User feedback stored and analyzed for future personalization
- **Dynamic Synthesis**: Briefing generation adapts based on user preferences
- **Deep Dive Capability**: Users can request detailed analysis on specific topics
- **Non-Breaking**: Backward compatible with existing delivery mechanism

### Technical Details
- Storage: `storage/feedback_log.json` (replaced by MemPalace in Stage 2)
- Buttons: InlineKeyboardMarkup with 3 voting options per article section
- Callbacks: `vote:{type}:{article_id}:{topic}` pattern
- Personalization: Weighted coverage based on user feedback history
- Caching: In-memory `RECENT_ARTICLES` dict for callback article lookup

### Next Steps After Implementation
- Full testing with user account (ID: 1212792251)
- Integration testing with n8n workflow
- Performance optimization for high-user scenarios
- Migration to MemPalace in Stage 2
