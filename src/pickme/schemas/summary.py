"""Summary result schema."""

from __future__ import annotations

from pydantic import BaseModel


class SummaryResult(BaseModel):
    """Result of a summarize pipeline run."""

    chat_id: int
    message_count: int
    truncated: bool
    html: str
