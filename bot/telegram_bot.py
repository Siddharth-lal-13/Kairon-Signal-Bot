"""
Kairon — Telegram Bot

Two-way interaction via python-telegram-bot v21 (async).

Commands:
  /start   — register and show welcome message
  /topics  — list current topic subscriptions
  /set <topic1> <topic2> ... — update topic subscriptions
  /status  — show current preferences
  /help    — command reference

The bot also accepts push delivery: the FastAPI webhook calls
`deliver_briefing(chat_id, text)` to push a briefing to a specific user.

Design decisions:
  - Preferences are read/written via the flat-file storage layer (storage/).
  - The bot runs in webhook mode when BOT_WEBHOOK_URL is set (production),
    and falls back to polling mode (development / local testing).
  - All command handlers are async and follow PTB v21 patterns.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

from dotenv import load_dotenv
from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from models.schemas import Topic, UserPreferences, AnalyzedArticle
from storage.store import (
    init_user_wing,
    load_preferences,
    save_preferences,
)

load_dotenv()
logger = logging.getLogger(__name__)

BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
BOT_WEBHOOK_URL: str = os.getenv("BOT_WEBHOOK_URL", "")   # e.g. https://yourdomain.com/bot
WEBHOOK_SECRET: str = os.getenv("BOT_WEBHOOK_SECRET", "")

# Module-level cache for article lookup in callbacks (Stage 1)
# Stage 2: replace with mempalace lookup
RECENT_ARTICLES: dict[str, AnalyzedArticle] = {}
MAX_RECENT_ARTICLES = 200


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_TOPICS = {t.value: t for t in Topic}

HELP_TEXT = (
    "*Kairon — AI Signal Briefing Bot*\n\n"
    "Commands:\n"
    "/start — Register and get started\n"
    "/topics — Show your current topic subscriptions\n"
    "/set ai automation startups tech — Update topics \\(space\\-separated\\)\n"
    "/status — Your full preference summary\n"
    "/help — This message\n\n"
    "Available topics: `ai` `automation` `startups` `tech`"
)


def _format_prefs(prefs: UserPreferences) -> str:
    topics_str = " ".join(f"`{t.value}`" for t in prefs.topics)
    return (
        f"*Your Kairon Preferences*\n\n"
        f"Topics: {topics_str}\n"
        f"Delivery: {prefs.delivery_hour_utc:02d}:00 UTC\n"
        f"Active: {'✅' if prefs.active else '❌'}"
    )


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat_id = update.effective_chat.id

    prefs = load_preferences(chat_id)
    if prefs is None:
        # Initialize user's MemPalace wing for Stage 1
        wing_key = init_user_wing(chat_id)

        prefs = UserPreferences(
            user_id=chat_id,
            username=user.username if user else None,
            mempalace_wing=wing_key,  # Set wing key for Stage 2
        )
        save_preferences(prefs)
        welcome = (
            f"*Welcome to Kairon\\!* 🤖\n\n"
            f"You're subscribed to: `ai` `tech`\n\n"
            f"Use /set to change topics, /help for all commands\\.\n"
            f"Your first briefing arrives at 07:00 UTC\\."
        )
    else:
        welcome = (
            f"*Welcome back\\!* 🤖\n\n"
            f"Your preferences are saved\\. Use /status to see them\\."
        )

    await update.message.reply_text(welcome, parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_topics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    prefs = load_preferences(chat_id)

    if prefs is None:
        await update.message.reply_text(
            "You're not registered yet\\. Send /start to get started\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    topics_str = " ".join(f"`{t.value}`" for t in prefs.topics)
    await update.message.reply_text(
        f"*Your topics:* {topics_str}\n\nUse /set to change them\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    args = context.args  # list of words after /set

    if not args:
        await update.message.reply_text(
            "Usage: `/set ai automation startups tech`\n"
            "Available: `ai` `automation` `startups` `tech`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    new_topics: list[Topic] = []
    invalid: list[str] = []
    for word in args:
        word = word.lower().strip()
        if word in VALID_TOPICS:
            t = VALID_TOPICS[word]
            if t not in new_topics:
                new_topics.append(t)
        else:
            invalid.append(word)

    if invalid:
        bad = ", ".join(f"`{w}`" for w in invalid)
        await update.message.reply_text(
            f"Unknown topic\\(s\\): {bad}\n"
            f"Available: `ai` `automation` `startups` `tech`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    if not new_topics:
        await update.message.reply_text(
            "Please specify at least one valid topic\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    prefs = load_preferences(chat_id) or UserPreferences(user_id=chat_id)
    prefs.topics = new_topics
    save_preferences(prefs)

    topics_str = " ".join(f"`{t.value}`" for t in new_topics)
    await update.message.reply_text(
        f"✅ Topics updated to: {topics_str}",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    prefs = load_preferences(chat_id)

    if prefs is None:
        await update.message.reply_text(
            "You're not registered yet\\. Send /start to get started\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    await update.message.reply_text(
        _format_prefs(prefs),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN_V2)


# ---------------------------------------------------------------------------
# Feedback loop functions (Upgrade 5)
# ---------------------------------------------------------------------------


def build_feedback_keyboard(article_id: str, topic: str) -> InlineKeyboardMarkup:
    """
    Build inline keyboard with feedback buttons for a single article.

    Args:
        article_id: Unique identifier for the article.
        topic: Primary topic of the article.

    Returns:
        InlineKeyboardMarkup with 3 buttons in one row.
    """
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👍 More like this", callback_data=f"vote:upvote:{article_id}:{topic}"),
                InlineKeyboardButton(text="👎 Less like this", callback_data=f"vote:downvote:{article_id}:{topic}"),
                InlineKeyboardButton(text="🔍 Deep dive", callback_data=f"vote:deepdive:{article_id}:{topic}"),
            ]
        ]
    )
    return keyboard


def format_briefing_with_buttons(briefing_text: str, analyzed_articles: list[AnalyzedArticle]) -> list[dict]:
    """
    Split briefing text by topic sections and attach inline keyboards.

    Args:
        briefing_text: Full briefing text from synthesizer.
        analyzed_articles: List of analyzed articles with article_ids and signal_types.

    Returns:
        List of message dicts, each with "text" and optional "keyboard".
    """
    if not analyzed_articles:
        # Fallback: return full briefing as single message without keyboard
        return [{"text": briefing_text, "keyboard": None}]

    # Update RECENT_ARTICLES cache (Stage 1)
    global RECENT_ARTICLES
    for article in analyzed_articles:
        RECENT_ARTICLES[article.article_id] = article
    # Evict oldest if over limit
    if len(RECENT_ARTICLES) > MAX_RECENT_ARTICLES:
        oldest_ids = list(RECENT_ARTICLES.keys())[:len(RECENT_ARTICLES) - MAX_RECENT_ARTICLES]
        for oid in oldest_ids:
            del RECENT_ARTICLES[oid]

    # Split briefing by topic sections
    topic_sections = {
        Topic.AI: r"\*AI\*",
        Topic.AUTOMATION: r"\*AUTOMATION\*",
        Topic.STARTUPS: r"\*STARTUPS\*",
        Topic.TECH: r"\*TECH\*",
    }

    messages = []
    for topic, pattern in topic_sections.items():
        import re
        # Split briefing by topic section headers
        sections = re.split(pattern, briefing_text)
        if len(sections) > 1:
            # Extract text for this topic (first non-empty section after header)
            section_text = sections[1].strip()
            if section_text:
                # Find first article in this topic from analyzed_articles
                topic_articles = [a for a in analyzed_articles if any(t in [topic.value] for t in a.topics)]
                if topic_articles:
                    first_article = topic_articles[0]
                    # Attach keyboard to this section's first article
                    keyboard = build_feedback_keyboard(first_article.article_id, topic.value)
                    messages.append({"text": sections[0] + section_text, "keyboard": keyboard})
                else:
                    # No articles for this topic, no keyboard
                    messages.append({"text": sections[0] + section_text, "keyboard": None})

    # Handle the final "Takeaway" section (no keyboard)
    remaining_text = sections[-1].strip() if sections else ""
    if remaining_text:
        messages.append({"text": remaining_text, "keyboard": None})

    # If splitting failed, fall back to full briefing
    if not messages:
        messages = [{"text": briefing_text, "keyboard": None}]

    return messages


async def handle_feedback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle inline button callback queries for user feedback.

    Parses callback_data format: "vote:{vote_type}:{article_id}:{topic}"
    """
    query = update.callback_query
    if not query:
        return

    callback_data = query.data
    logger.debug("Received callback: %s", callback_data)

    try:
        # Parse callback data: "vote:{vote_type}:{article_id}:{topic}"
        parts = callback_data.split(":")
        if len(parts) != 4 or parts[0] != "vote":
            await query.answer("Invalid callback format")
            return

        vote_type, article_id, topic = parts[1], parts[2], parts[3]

        # Validate vote type
        if vote_type not in ["upvote", "downvote", "deepdive"]:
            await query.answer("Invalid vote type")
            return

        # Look up article from cache to get entities and signal_type
        article = RECENT_ARTICLES.get(article_id)
        if not article:
            # Fallback: use empty entities and "other" for signal_type
            entities = []
            signal_type = "other"
        else:
            entities = article.key_entities
            signal_type = article.signal_type.value

        # Store feedback
        from storage.store import append_feedback_record
        await append_feedback_record(
            user_id=query.from_user.id,
            article_id=article_id,
            signal_type=signal_type,
            entities=entities,
            topic=topic,
            vote=vote_type,
        )

        # Answer callback based on vote type
        if vote_type == "upvote":
            await query.answer("Got it — more like this 👍")
        elif vote_type == "downvote":
            await query.answer("Noted — less of this 👎")
        elif vote_type == "deepdive":
            await query.answer("On it — fetching more... 🔍")
            # For deepdive: trigger a follow-up message and pipeline run
            await query.message.reply_text(f"🔍 Deep dive on this topic coming shortly...")
            # Run pipeline for this specific topic as background task
            try:
                from agents.pipeline import run_pipeline
                import asyncio

                # Convert topic string to Topic enum
                from models.schemas import Topic
                topic_enum = Topic(topic)

                # Run pipeline as background task
                asyncio.create_task(run_pipeline(query.from_user.id, [topic_enum]))

            except Exception as exc:
                logger.error("Failed to trigger deep dive: %s", exc)
                await query.message.reply_text(f"⚠️ Failed to trigger deep dive. Please try again later.")

    except Exception as exc:
        logger.error("Error handling feedback callback: %s", exc)
        await query.answer("Sorry, something went wrong. Please try again.")


async def deliver_briefing_with_feedback(chat_id: int, briefing_text: str,
                                        analyzed_articles: list[AnalyzedArticle]) -> None:
    """
    Deliver briefing with inline buttons for feedback (Upgrade 5).

    This replaces deliver_briefing for interactive briefings while
    keeping the original function for backward compatibility.

    Args:
        chat_id: Telegram chat_id of the recipient.
        briefing_text: Full briefing text from synthesizer.
        analyzed_articles: List of analyzed articles for button generation.
    """
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    bot = Bot(token=BOT_TOKEN)

    try:
        # Format briefing into sections with buttons
        messages = format_briefing_with_buttons(briefing_text, analyzed_articles)

        # Send each message section with its keyboard
        for msg in messages:
            if msg["keyboard"]:
                await bot.send_message(
                    chat_id=chat_id,
                    text=msg["text"],
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=msg["keyboard"],
                )
            else:
                # Last section (The Takeaway) or fallback
                await bot.send_message(
                    chat_id=chat_id,
                    text=msg["text"],
                    parse_mode=ParseMode.MARKDOWN_V2,
                )

        logger.info("Delivered interactive briefing to chat_id %d", chat_id)

    except Exception as exc:
        logger.error("Failed to deliver briefing to chat_id %d: %s", chat_id, exc)
        raise


# ---------------------------------------------------------------------------
# Push delivery (called externally by the pipeline)
# ---------------------------------------------------------------------------


async def deliver_briefing(chat_id: int, text: str) -> None:
    """Push a pre-formatted briefing text to a Telegram chat."""
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    bot = Bot(token=BOT_TOKEN)
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        logger.info("Delivered briefing to chat_id %d", chat_id)
    except Exception as exc:
        logger.error("Failed to deliver briefing to chat_id %d: %s", chat_id, exc)
        raise


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def build_application() -> Application:
    """Build and return a configured PTB Application instance."""
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in .env")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("topics", cmd_topics))
    app.add_handler(CommandHandler("set", cmd_set))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("help", cmd_help))

    # Register feedback callback handler (Upgrade 5)
    app.add_handler(CallbackQueryHandler(handle_feedback_callback, pattern="^vote:"))

    return app


# ---------------------------------------------------------------------------
# Entrypoint — polling mode for development
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import logging as _logging

    _logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("Starting Kairon bot in polling mode (development)...")
    app = build_application()
    app.run_polling(drop_pending_updates=True)
