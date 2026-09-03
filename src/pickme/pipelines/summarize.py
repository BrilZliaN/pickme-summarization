"""Summarize pipeline — single-shot + map-reduce with hard token cap."""

from __future__ import annotations

import logging

from pickme.db import queries
from pickme.llm.client import LLMError, extract_json  # type: ignore
from pickme.llm.prompts import SUMMARIZE_REDUCE_SYSTEM, SUMMARIZE_SYSTEM, ROLLING_MERGE_SYSTEM
from pickme.pipelines.render import build_aliases, dealias, estimate_tokens, render_transcript, sanitize_html
from pickme.schemas.summary import SummaryResult

logger = logging.getLogger("pickme.pipelines")

# Placeholder for LSP / circular-avoidance; real PipelineError lives in pipelines.__init__
class PipelineError(Exception):  # type: ignore[no-redef]
    """Placeholder — actual class is pickme.pipelines.PipelineError."""

    pass


def _chunk_rows_by_tokens(rows: list[dict], aliases: dict[int, str], chunk_budget: int) -> list[list[dict]]:
    """Split rows into chunks where each chunk transcript estimate <= chunk_budget."""
    if not rows:
        return []
    chunks: list[list[dict]] = []
    current: list[dict] = []
    for row in rows:
        # Tentatively add row to current chunk and estimate
        tentative = current + [row]
        transcript = render_transcript(tentative, aliases, with_time=True)
        est = estimate_tokens(transcript)
        if est > chunk_budget and current:
            # Current chunk without this row is within budget; start new chunk
            chunks.append(current)
            current = [row]
            # If single row alone exceeds budget, still keep it as one chunk
            single_transcript = render_transcript(current, aliases, with_time=True)
            if estimate_tokens(single_transcript) > chunk_budget:
                # Keep as is — cannot split a single message further
                pass
        else:
            current = tentative
            # If even current alone would exceed and it's a single row, keep it
            if estimate_tokens(render_transcript(current, aliases, with_time=True)) > chunk_budget and len(current) == 1:
                # Will be flushed on next iteration or at end
                pass
    if current:
        chunks.append(current)
    return chunks


async def _fetch_display_names(conn, aliases: dict[int, str]) -> dict[int, str]:
    """Fetch real display names for the user_ids covered by *aliases*.

    Used only for the final dealias substitution — prompts stay aliased
    (privacy rule §4).
    """
    user_ids = [uid for uid in aliases if uid is not None]
    if not user_ids:
        return {}
    try:
        placeholders = ",".join("?" for _ in user_ids)
        cursor = await conn.execute(
            f"SELECT user_id, display_name FROM users WHERE user_id IN ({placeholders})",
            tuple(user_ids),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        names: dict[int, str] = {}
        for r in rows:
            try:
                uid = int(r["user_id"])  # type: ignore[index]
                disp = r.get("display_name")  # type: ignore[union-attr]
                if disp:
                    names[uid] = str(disp)
            except Exception:
                continue
        return names
    except Exception:
        logger.debug("fetch names for summarize dealias failed", exc_info=True)
        return {}


async def _call_summarize_chunk(llm, transcript: str) -> str:
    """Call LLM to summarize a single transcript chunk (raw, unsanitized)."""
    # Deferred import to avoid circular init
    from pickme.pipelines import PipelineError as _PipelineError

    try:
        res = await llm.chat(
            [
                {"role": "system", "content": SUMMARIZE_SYSTEM},
                {"role": "user", "content": transcript},
            ],
            feature="summarize",
        )
    except LLMError as e:
        raise _PipelineError(str(e) or "LLM unavailable for summarization.") from e
    except Exception as e:  # pragma: no cover
        raise _PipelineError(f"Summarization failed: {e}") from e
    return res.text or ""


async def run_summarize(db, llm, settings, chat_id: int, count: int) -> SummaryResult:
    """Summarize the last *count* messages for a chat.

    Handles 150k hard cap, single-shot vs map-reduce, and returns a
    :class:`SummaryResult`.
    """
    cap = int(getattr(settings, "summarize_cap", 500))
    count = max(1, min(int(count), cap))

    conn = db.conn
    rows = await queries.get_last_messages(conn, chat_id, count)

    if not rows:
        return SummaryResult(chat_id=chat_id, message_count=0, truncated=False, html=sanitize_html("Пока нечего резюмировать — сообщений нет."))

    aliases = build_aliases(rows)
    transcript = render_transcript(rows, aliases, with_time=True)

    # Hard cap ~150k tokens — drop oldest rows and set truncated=True
    truncated = False
    total_est = estimate_tokens(transcript)
    hard_cap = 150_000
    if total_est > hard_cap:
        truncated = True
        # Drop oldest until within cap
        while rows and estimate_tokens(render_transcript(rows, aliases, with_time=True)) > hard_cap:
            rows = rows[1:]
            # Recompute aliases for remaining rows to keep numbering tight
            aliases = build_aliases(rows)
        transcript = render_transcript(rows, aliases, with_time=True)
        total_est = estimate_tokens(transcript)
        if not rows:
            return SummaryResult(chat_id=chat_id, message_count=0, truncated=True, html=sanitize_html("Сообщения слишком объёмные для резюме."))

    # Real display names for the final substitution only — prompts stay aliased (§4).
    names = await _fetch_display_names(conn, aliases)

    # Single-shot path
    if total_est <= 60_000:
        raw = await _call_summarize_chunk(llm, transcript)
        if truncated:
            raw += "\n\nⓘ Показаны самые свежие сообщения (слишком большой объём для полного охвата)."
        return SummaryResult(
            chat_id=chat_id,
            message_count=len(rows),
            truncated=truncated,
            html=sanitize_html(dealias(raw, aliases, names)),
        )

    # Map-reduce path: split into ~40k token chunks
    chunks = _chunk_rows_by_tokens(rows, aliases, 40_000)
    if not chunks:
        chunks = [rows]

    chunk_summaries: list[str] = []
    for chunk in chunks:
        chunk_transcript = render_transcript(chunk, aliases, with_time=True)
        chunk_summary = await _call_summarize_chunk(llm, chunk_transcript)
        chunk_summaries.append(chunk_summary)

    # Reduce phase
    combined = "\n\n---\n\n".join(chunk_summaries)
    try:
        res = await llm.chat(
            [
                {"role": "system", "content": SUMMARIZE_REDUCE_SYSTEM},
                {"role": "user", "content": combined},
            ],
            feature="summarize",
        )
    except LLMError as e:
        from pickme.pipelines import PipelineError as _PE2

        raise _PE2(str(e) or "LLM unavailable for summarization.") from e
    except Exception as e:  # pragma: no cover
        from pickme.pipelines import PipelineError as _PE3

        raise _PE3(f"Summarization reduce failed: {e}") from e

    raw_final = res.text or combined
    if truncated:
        raw_final += "\n\nⓘ Показаны самые свежие сообщения (слишком большой объём для полного охвата)."
    return SummaryResult(
        chat_id=chat_id,
        message_count=len(rows),
        truncated=truncated,
        html=sanitize_html(dealias(raw_final, aliases, names)),
    )


async def maybe_update_rolling(
    db, llm, settings, chat_id: int, min_new: int = 25, window_budget_tokens: int = 40000
) -> bool:
    """Maybe update the rolling summary if enough new messages exist.

    Returns ``True`` on successful update, ``False`` otherwise (never raises).
    """
    conn = db.conn
    try:
        chat = await queries.get_chat(conn, chat_id)
    except Exception:
        logger.debug("get_chat failed for rolling update", exc_info=True)
        return False

    if chat is None:
        return False

    watermark = chat.get("rolling_summary_at")
    try:
        watermark_int = int(watermark) if watermark is not None else 0  # type: ignore[arg-type]
    except Exception:
        watermark_int = 0

    try:
        new_count = await queries.count_messages_since(conn, chat_id, watermark_int)
    except Exception:
        logger.debug("count_messages_since failed for rolling", exc_info=True)
        return False

    if new_count < int(min_new):
        return False

    try:
        window_rows = await queries.get_messages_since(conn, chat_id, watermark_int)
    except Exception:
        logger.debug("get_messages_since failed for rolling", exc_info=True)
        return False

    if not window_rows:
        return False

    aliases = build_aliases(window_rows)
    transcript = render_transcript(window_rows, aliases, with_time=True)
    window_est = estimate_tokens(transcript)

    old_summary = chat.get("rolling_summary") or ""

    try:
        # If window exceeds budget, map-reduce the window first
        segment_summaries: list[str]
        if window_est > window_budget_tokens:
            chunks = _chunk_rows_by_tokens(window_rows, aliases, 40_000)
            segment_summaries = []
            for chunk in chunks:
                chunk_transcript = render_transcript(chunk, aliases, with_time=True)
                # Use summarize system for window chunk compression
                try:
                    res = await llm.chat(
                        [
                            {"role": "system", "content": SUMMARIZE_SYSTEM},
                            {"role": "user", "content": chunk_transcript},
                        ],
                        feature="rolling",
                    )
                    segment_summaries.append(res.text or "")
                except Exception as e:
                    logger.warning("rolling window chunk summarize failed: %s", e, exc_info=True)
                    return False
            new_segment = "\n\n---\n\n".join(segment_summaries)
        else:
            new_segment = transcript

        # Final merge prompt
        user_content = f"OLD summary:\n{old_summary}\n\nNEW segment:\n{new_segment}"
        res = await llm.chat(
            [
                {"role": "system", "content": ROLLING_MERGE_SYSTEM},
                {"role": "user", "content": user_content},
            ],
            feature="rolling",
        )
        new_summary_raw = res.text or ""
        # Keep compact: limit to ~2k tokens ~8000 chars
        new_summary = new_summary_raw.strip()
        # Truncate if over ~8000 chars to respect 2k tokens
        if len(new_summary) // 4 > 2000:
            # Rough char limit
            new_summary = new_summary[:8000]

        new_summary_html = sanitize_html(new_summary)
        # Use last message id as new watermark (frozen spec)
        try:
            last_id = int(window_rows[-1].get("id", watermark_int))  # type: ignore[arg-type]
        except Exception:
            last_id = watermark_int

        await queries.set_rolling_summary(conn, chat_id, new_summary_html, last_id)
        return True
    except LLMError:
        logger.warning("maybe_update_rolling LLMError for chat %s", chat_id, exc_info=True)
        return False
    except Exception:
        logger.warning("maybe_update_rolling failed for chat %s", chat_id, exc_info=True)
        return False
