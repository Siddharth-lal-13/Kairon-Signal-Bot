"""
Kairon — Storage Layer (Stage 2: MemPalace + flat JSON files)

Stage 2 Architecture:
  storage/preferences.json  — dict[str(user_id), UserPreferences]
  storage/delivery_log.json — list[DeliveryRecord]
  MemPalace              — Persistent memory for briefings and user history

Thread-safe writes use a simple file lock (filelock library).
MemPalace provides long-term memory storage for personalization and trend analysis.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from filelock import FileLock

# MemPalace integration (Stage 2)
try:
    from mempalace import MemPalace
    MEMPALACE_AVAILABLE = True
except ImportError:
    MEMPALACE_AVAILABLE = False
    # Silently use fallback mode in Stage 1 - no warning needed

from models.schemas import DeliveryRecord, UserPreferences, Briefing

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MemPalace Connection (Stage 2)
# ---------------------------------------------------------------------------

_mempalace: Optional[MemPalace] = None
_mempalace_initialized = False


def _get_mempalace() -> Optional[MemPalace]:
    """Get or initialize MemPalace connection (lazy initialization)."""
    global _mempalace, _mempalace_initialized

    if not MEMPALACE_AVAILABLE:
        return None

    if not _mempalace_initialized:
        try:
            # Initialize MemPalace with default settings
            _mempalace = MemPalace()
            _mempalace_initialized = True
            logger.info("MemPalace initialized successfully")
        except Exception as exc:
            logger.error("Failed to initialize MemPalace: %s", exc)
            _mempalace = None
            _mempalace_initialized = True

    return _mempalace


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_STORAGE_DIR = Path(os.getenv("STORAGE_DIR", "storage"))
_PREFS_FILE = _STORAGE_DIR / "preferences.json"
_LOG_FILE = _STORAGE_DIR / "delivery_log.json"
_FEEDBACK_FILE = _STORAGE_DIR / "feedback_log.json"

_PREFS_LOCK = FileLock(str(_PREFS_FILE) + ".lock")
_LOG_LOCK = FileLock(str(_LOG_FILE) + ".lock")
_FEEDBACK_LOCK = FileLock(str(_FEEDBACK_FILE) + ".lock")


def _ensure_storage_dir() -> None:
    _STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    if not _PREFS_FILE.exists():
        _PREFS_FILE.write_text("{}", encoding="utf-8")
    if not _LOG_FILE.exists():
        _LOG_FILE.write_text("[]", encoding="utf-8")


_ensure_storage_dir()


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------


def load_preferences(user_id: int) -> Optional[UserPreferences]:
    """Return UserPreferences for a given user_id, or None if not registered."""
    with _PREFS_LOCK:
        # Handle UTF-8 BOM by using utf-8-sig encoding
        content = _PREFS_FILE.read_text(encoding="utf-8-sig")
        data = json.loads(content)

    record = data.get(str(user_id))
    if record is None:
        return None

    try:
        return UserPreferences.model_validate(record)
    except Exception as exc:
        logger.error("Corrupt preferences for user %d: %s", user_id, exc)
        return None


def save_preferences(prefs: UserPreferences) -> None:
    """Persist (create or update) preferences for one user."""
    with _PREFS_LOCK:
        # Handle UTF-8 BOM by using utf-8-sig encoding for reading
        content = _PREFS_FILE.read_text(encoding="utf-8-sig")
        data = json.loads(content)
        data[str(prefs.user_id)] = prefs.model_dump(mode="json")
        # Write without BOM to prevent future issues
        _PREFS_FILE.write_text(
            json.dumps(data, indent=2, default=str), encoding="utf-8"
        )
    logger.debug("Saved preferences for user %d", prefs.user_id)


def load_all_preferences() -> list[UserPreferences]:
    """Return all active user preferences."""
    with _PREFS_LOCK:
        # Handle UTF-8 BOM by using utf-8-sig encoding
        content = _PREFS_FILE.read_text(encoding="utf-8-sig")
        data = json.loads(content)

    prefs_list: list[UserPreferences] = []
    for uid, record in data.items():
        try:
            p = UserPreferences.model_validate(record)
            if p.active:
                prefs_list.append(p)
        except Exception as exc:
            logger.error("Skipping corrupt preferences for user %s: %s", uid, exc)

    return prefs_list


# ---------------------------------------------------------------------------
# Delivery log
# ---------------------------------------------------------------------------


def append_delivery_record(record: DeliveryRecord) -> None:
    """Append one DeliveryRecord to the delivery log."""
    with _LOG_LOCK:
        # Handle UTF-8 BOM by using utf-8-sig encoding
        content = _LOG_FILE.read_text(encoding="utf-8-sig")
        log = json.loads(content)
        log.append(record.model_dump(mode="json"))
        # Write without BOM to prevent future issues
        _LOG_FILE.write_text(
            json.dumps(log, indent=2, default=str), encoding="utf-8"
        )
    logger.debug("Logged delivery %s for user %d", record.record_id, record.user_id)


def get_last_delivery(user_id: int) -> Optional[DeliveryRecord]:
    """Return the most recent delivery record for a user, or None."""
    with _LOG_LOCK:
        # Handle UTF-8 BOM by using utf-8-sig encoding
        content = _LOG_FILE.read_text(encoding="utf-8-sig")
        log = json.loads(content)

    user_records = [r for r in log if r.get("user_id") == user_id]
    if not user_records:
        return None

    latest = max(user_records, key=lambda r: r.get("delivered_at", ""))
    try:
        return DeliveryRecord.model_validate(latest)
    except Exception as exc:
        logger.error("Corrupt delivery record for user %d: %s", user_id, exc)
        return None


# ---------------------------------------------------------------------------
# MemPalace Integration Hooks (Stage 1)
# ---------------------------------------------------------------------------


def init_user_wing(user_id: int) -> str:
    """
    Initialize user's MemPalace wing for Stage 2.

    Args:
        user_id: Telegram chat_id of the user.

    Returns:
        Wing key string "wing_user_{user_id}".
    """
    wing_key = f"wing_user_{user_id}"

    if not MEMPALACE_AVAILABLE:
        logger.warning("MemPalace not available, using fallback wing key: %s", wing_key)
        return wing_key

    mempalace = _get_mempalace()
    if not mempalace:
        logger.warning("MemPalace initialization failed, using fallback wing key: %s", wing_key)
        return wing_key

    try:
        # Create a new wing for the user in MemPalace
        mempalace.create_wing(wing_key, description=f"User {user_id} briefing history")
        logger.info("MemPalace wing %s initialized for user %d", wing_key, user_id)
    except Exception as exc:
        logger.error("Failed to create MemPalace wing %s: %s", wing_key, exc)
        # Return wing key anyway as fallback

    return wing_key


def store_briefing_memory(briefing: Briefing) -> None:
    """
    Store briefing in MemPalace for Stage 2 (full implementation).

    Creates a memory drawer containing the full briefing data including
    topics, article count, and the actual text content for future
    personalization and trend analysis.

    Args:
        briefing: Briefing object to store.
    """
    wing_key = briefing.mempalace_wing or "not_set"

    if not MEMPALACE_AVAILABLE:
        logger.warning("MemPalace not available, skipping storage for briefing %s", briefing.briefing_id)
        return

    mempalace = _get_mempalace()
    if not mempalace:
        logger.warning("MemPalace initialization failed, skipping storage for briefing %s", briefing.briefing_id)
        return

    try:
        # Create a memory drawer with briefing data
        drawer_key = f"briefing_{briefing.briefing_id}"

        memory_data = {
            "briefing_id": briefing.briefing_id,
            "user_id": briefing.user_id,
            "generated_at": briefing.generated_at.isoformat(),
            "topics_covered": [topic.value for topic in briefing.topics_covered],
            "article_count": briefing.article_count,
            "telegram_text": briefing.telegram_text,
            "stored_at": datetime.utcnow().isoformat(),
        }

        mempalace.add_drawer(wing_key, drawer_key, content=memory_data)
        logger.info(
            "Stored briefing %s in MemPalace wing %s (drawer: %s)",
            briefing.briefing_id,
            wing_key,
            drawer_key,
        )
    except Exception as exc:
        logger.error("Failed to store briefing %s in MemPalace: %s", briefing.briefing_id, exc)


# ---------------------------------------------------------------------------
# Feedback Log (Stage 2: User feedback for personalization)
# ---------------------------------------------------------------------------


def append_feedback_record(user_id: int, article_id: str, signal_type: str,
                          entities: list[str], topic: str, vote: str) -> None:
    """
    Append one feedback record to the feedback log.

    Args:
        user_id: Telegram chat_id of the user.
        article_id: Unique identifier for the article being rated.
        signal_type: SignalType of the article (product_launch, funding, etc.).
        entities: List of key entities mentioned in the article.
        topic: Primary topic of the article.
        vote: One of "upvote", "downvote", "deepdive".
    """
    feedback_record = {
        "user_id": user_id,
        "article_id": article_id,
        "signal_type": signal_type,
        "entities": entities,
        "topic": topic,
        "vote": vote,
        "recorded_at": datetime.utcnow().isoformat(),
    }

    with _FEEDBACK_LOCK:
        # Handle UTF-8 BOM by using utf-8-sig encoding
        content = _FEEDBACK_FILE.read_text(encoding="utf-8-sig")
        log = json.loads(content)
        log.append(feedback_record)
        # Write without BOM to prevent future issues
        _FEEDBACK_FILE.write_text(
            json.dumps(log, indent=2, default=str), encoding="utf-8"
        )
    logger.debug("Logged feedback %s for user %d on article %s", vote, user_id, article_id)

    # Stage 2: replace with mempalace_add_drawer to # hall_preferences in user's wing
    return None


def get_user_feedback_summary(user_id: int) -> dict:
    """
    Get aggregated feedback summary for a user.

    Returns:
        Dict with:
        - "upvoted_entities": list[str] (all entities from upvotes, deduplicated, max 10 recent)
        - "downvoted_topics": list[str] (signal_types from downvotes)
        - "upvoted_topics": list[str] (signal_types from upvotes)
    """
    with _FEEDBACK_LOCK:
        # Handle UTF-8 BOM by using utf-8-sig encoding
        content = _FEEDBACK_FILE.read_text(encoding="utf-8-sig")
        feedback_log = json.loads(content)

    # Filter feedback for this user
    user_feedback = [f for f in feedback_log if f.get("user_id") == user_id]

    if not user_feedback:
        return {
            "upvoted_entities": [],
            "downvoted_topics": [],
            "upvoted_topics": [],
        }

    # Sort by recency, keep most recent
    user_feedback.sort(key=lambda x: x.get("recorded_at", ""), reverse=True)

    # Extract upvoted entities (deduplicated, max 10 recent)
    upvoted_entities_set = set()
    upvoted_entities = []
    for feedback in user_feedback:
        if feedback.get("vote") == "upvote":
            entities = feedback.get("entities", [])
            for entity in entities:
                if entity not in upvoted_entities_set:
                    upvoted_entities_set.add(entity)
                    upvoted_entities.append(entity)
                if len(upvoted_entities) >= 10:
                    break

    # Extract downvoted topics
    downvoted_topics_set = set()
    for feedback in user_feedback:
        if feedback.get("vote") == "downvote":
            topic = feedback.get("topic", "")
            if topic:
                downvoted_topics_set.add(topic)

    # Extract upvoted topics
    upvoted_topics_set = set()
    for feedback in user_feedback:
        if feedback.get("vote") == "upvote":
            signal_type = feedback.get("signal_type", "")
            if signal_type:
                upvoted_topics_set.add(signal_type)

    return {
        "upvoted_entities": upvoted_entities,
        "downvoted_topics": list(downvoted_topics_set),
        "upvoted_topics": list(upvoted_topics_set),
    }

    # Stage 2: replace with mempalace_search("upvoted entities", wing=user_wing)
    return None
