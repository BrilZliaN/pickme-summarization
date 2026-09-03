"""User profile and evaluation schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class UserProfile(BaseModel):
    """Merged per-user memory snapshot stored as JSON in ``user_profiles``."""

    topics: list[str] = Field(default_factory=list)
    stance: str | None = None
    activity_level: int = Field(default=1, ge=1, le=5)
    notable_facts: list[str] = Field(default_factory=list)
    narrative: str = ""


class Evaluation(BaseModel):
    """Structured assessment produced by the evaluate pipeline."""

    tone: str = Field(default="neutral")  # supportive|neutral|confrontational
    constructiveness: int = Field(default=3, ge=1, le=5)
    participation: int = Field(default=3, ge=1, le=5)
    dominant_topics: list[str] = Field(default_factory=list)
    notable_contributions: list[str] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)


class EvalTarget(BaseModel):
    """Target selector for ``/evaluate``."""

    kind: Literal["user", "me", "everyone"] = "me"
    user_id: int | None = None
    username: str | None = None
