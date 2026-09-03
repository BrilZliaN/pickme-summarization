"""Pipelines package — public API for ingestion, summarization, memory, evaluation, and Q&A."""

from __future__ import annotations


class PipelineError(Exception):
    """User-friendly, Telegram-ready error.

    ``str(exc)`` is safe to send directly to the user.
    """

    def __str__(self) -> str:  # type: ignore[override]
        msg = super().__str__()
        if msg:
            return msg
        return "Something went wrong — please try again."


# Re-export public names (import after PipelineError definition to avoid circular issues
# — submodules defer PipelineError import, so importing them here is safe).
from pickme.pipelines.render import (  # noqa: E402
    build_aliases,
    dealias,
    estimate_tokens,
    render_transcript,
    sanitize_html,
)
from pickme.pipelines.ingest import ingest_message  # noqa: E402
from pickme.pipelines.summarize import maybe_update_rolling, run_summarize  # noqa: E402
from pickme.pipelines.memory import drain_pending, reaper_tick, run_memory_merge  # noqa: E402
from pickme.pipelines.evaluate import render_card, run_evaluate  # noqa: E402
from pickme.pipelines.qa import run_qa  # noqa: E402

__all__ = [
    "PipelineError",
    "build_aliases",
    "render_transcript",
    "dealias",
    "estimate_tokens",
    "sanitize_html",
    "ingest_message",
    "run_summarize",
    "maybe_update_rolling",
    "run_memory_merge",
    "drain_pending",
    "reaper_tick",
    "run_evaluate",
    "render_card",
    "run_qa",
]
