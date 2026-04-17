"""
Kairon — Pydantic v2 data models.

All inter-module data flows through these schemas so every agent
(fetcher → analyzer → synthesizer → delivery) is type-safe and
the n8n webhook layer has a clear contract.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, HttpUrl, field_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Topic(str, Enum):
    """Supported signal topics.  Extend here when adding new categories."""

    AI = "ai"
    AUTOMATION = "automation"
    STARTUPS = "startups"
    TECH = "tech"


class SignalType(str, Enum):
    """Classification of what kind of signal an article represents."""

    PRODUCT_LAUNCH = "product_launch"
    FUNDING = "funding"
    ACQUISITION = "acquisition"
    RESEARCH = "research"
    REGULATION = "regulation"
    TREND = "trend"
    OPINION = "opinion"
    OTHER = "other"


class DeliveryStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Raw article — output of the fetcher agent
# ---------------------------------------------------------------------------


class RawArticle(BaseModel):
    """A single article as returned by a news API before any LLM processing."""

    article_id: str = Field(
        description="Stable dedup key: sha256(source + url)[:16]"
    )
    title: str
    description: Optional[str] = None
    content: Optional[str] = None          # may be truncated by free-tier APIs
    url: str
    source_name: str                       # e.g. "TechCrunch", "gnews"
    api_source: str                        # which API returned this: "newsapi" | "gnews"
    published_at: datetime
    topics_matched: list[Topic] = Field(
        default_factory=list,
        description="Topics whose keywords matched this article during fetch",
    )

    @field_validator("title")
    @classmethod
    def title_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Article title cannot be empty")
        return v.strip()


# ---------------------------------------------------------------------------
# Analyzed article — output of the analyzer agent (LangChain / Ollama)
# ---------------------------------------------------------------------------


class AnalyzedArticle(BaseModel):
    """RawArticle enriched by local LLM analysis."""

    article_id: str
    title: str
    url: str
    source_name: str
    published_at: datetime
    topics: list[Topic]

    # LLM-extracted fields
    signal_type: SignalType
    one_line_summary: str = Field(
        description="≤ 25-word plain-English summary of what happened"
    )
    why_it_matters: str = Field(
        description="≤ 40-word explanation of the signal's significance"
    )
    key_entities: list[str] = Field(
        default_factory=list,
        description="Companies, people, or technologies central to the story",
        max_length=5,
    )
    relevance_score: float = Field(
        ge=0.0,
        le=1.0,
        description="How relevant this article is to the matched topics (0–1)",
    )


# ---------------------------------------------------------------------------
# Briefing — output of the synthesizer agent (NVIDIA NIM / Llama-3.1-70B)
# ---------------------------------------------------------------------------


class Briefing(BaseModel):
    """The final synthesized daily briefing ready for Telegram delivery."""

    briefing_id: str = Field(
        description="Unique ID: sha256(user_id + date)[:12]"
    )
    user_id: int = Field(description="Telegram chat_id of the recipient")
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    topics_covered: list[Topic]
    article_count: int

    # Telegram-formatted Markdown text (MarkdownV2-safe)
    telegram_text: str = Field(
        description="Full briefing text formatted for Telegram MarkdownV2"
    )

    # Stage 2 hook — MemPalace wing key for this user's memory
    # Populated in Stage 2 once mempalace is integrated.
    mempalace_wing: Optional[str] = Field(
        default=None,
        description="Stage 2: MemPalace wing key for per-user memory storage",
    )


# ---------------------------------------------------------------------------
# User preferences — persisted to storage/preferences.json (Stage 1)
# ---------------------------------------------------------------------------


class UserPreferences(BaseModel):
    """Per-user topic preferences.  Flat-file backed in Stage 1."""

    user_id: int = Field(description="Telegram chat_id")
    username: Optional[str] = None
    topics: list[Topic] = Field(
        default_factory=lambda: [Topic.AI, Topic.TECH],
        description="Topics the user wants in their briefing",
    )
    delivery_hour_utc: int = Field(
        default=7,
        ge=0,
        le=23,
        description="Hour (UTC) at which the daily briefing is delivered",
    )
    active: bool = True
    registered_at: datetime = Field(default_factory=datetime.utcnow)

    # Stage 2 hook
    mempalace_wing: Optional[str] = Field(
        default=None,
        description="Stage 2: MemPalace wing for this user's memory palace",
    )


# ---------------------------------------------------------------------------
# Delivery log entry — persisted to storage/delivery_log.json (Stage 1)
# ---------------------------------------------------------------------------


class DeliveryRecord(BaseModel):
    """Audit trail entry written after each briefing attempt."""

    record_id: str
    user_id: int
    briefing_id: str
    delivered_at: datetime = Field(default_factory=datetime.utcnow)
    status: DeliveryStatus
    article_count: int
    topics_covered: list[Topic]
    error_message: Optional[str] = None    # populated on failure


# ---------------------------------------------------------------------------
# n8n webhook payloads
# ---------------------------------------------------------------------------


class TriggerPayload(BaseModel):
    """
    Payload sent by n8n to POST /trigger when the 12-hour cron fires.
    Can optionally target a single user for testing.
    """

    run_id: str = Field(description="UUID generated by n8n for this run")
    triggered_at: datetime
    target_user_id: Optional[int] = Field(
        default=None,
        description="If set, only deliver to this user (debug/test mode)",
    )


class TriggerResponse(BaseModel):
    """FastAPI response to the n8n trigger."""

    run_id: str
    accepted: bool
    queued_users: int
    message: str


# ---------------------------------------------------------------------------
# Bot command models (two-way Telegram interaction)
# ---------------------------------------------------------------------------


class TopicUpdateRequest(BaseModel):
    """Parsed from /topics Telegram command."""

    user_id: int
    new_topics: list[Topic]


class StatusResponse(BaseModel):
    """Returned by /status command."""

    user_id: int
    topics: list[Topic]
    delivery_hour_utc: int
    active: bool
    last_briefing_at: Optional[datetime] = None
