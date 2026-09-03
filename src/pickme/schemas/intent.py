"""Intent schema for the NL router."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class Intent(BaseModel):
    """Structured intent produced by the fast-path or LLM router."""

    action: Literal["summarize", "evaluate", "qa", "forget", "help"] = "qa"
    count: int | None = None
    target: str | None = None
