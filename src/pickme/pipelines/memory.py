"""Memory merge pipeline — batched profile updates and reaper."""

from __future__ import annotations

import logging
import time

from pickme.db import queries
from pickme.llm.client import LLMError, extract_json  # type: ignore
from pickme.llm.prompts import MEMORY_MERGE_SYSTEM
from pickme.pipelines.render import build_aliases, render_transcript
from pickme.schemas.profile import UserProfile

logger = logging.getLogger("pickme.pipelines")

# Placeholder to satisfy LSP before pipelines.__init__ exists
class PipelineError(Exception):  # type: ignore[no-redef]
    pass


async def _fetch_display_names(conn, user_ids: list[int]) -> dict[int, str]:
    """Fetch display_name map for given user_ids."""
    if not user_ids:
        return {}
    placeholders = ",".join("?" for _ in user_ids)
    try:
        cursor = await conn.execute(
            f"SELECT user_id, display_name FROM users WHERE user_id IN ({placeholders})",
            tuple(user_ids),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        result: dict[int, str] = {}
        for r in rows:
            uid = int(r["user_id"])  # type: ignore[index]
            name = r.get("display_name")  # type: ignore[union-attr]
            if name:
                result[uid] = str(name)
        return result
    except Exception:
        logger.debug("fetch display names failed", exc_info=True)
        return {}


async def run_memory_merge(db, llm, settings, chat_id: int, user_id: int) -> None:
    """Merge new messages for a user into their profile.

    Never raises — logs and cleans up job on failure to avoid poison loop.
    """
    conn = db.conn
    try:
        profile = await queries.get_profile(conn, chat_id, user_id)
    except Exception:
        logger.warning("run_memory_merge get_profile failed for %s/%s", chat_id, user_id, exc_info=True)
        return

    watermark = int(profile["since_message_id"]) if profile is not None else 0  # type: ignore[index]

    try:
        rows = await queries.get_user_messages_since(conn, chat_id, user_id, watermark)
    except Exception:
        logger.warning("get_user_messages_since failed for %s/%s", chat_id, user_id, exc_info=True)
        return

    if not rows:
        try:
            await queries.delete_memory_job(conn, chat_id, user_id)
        except Exception:
            logger.debug("delete_memory_job failed (empty rows)", exc_info=True)
        return

    aliases = build_aliases(rows)
    transcript = render_transcript(rows, aliases, with_time=True)
    old_json = profile["profile_json"] if profile is not None else "{}"  # type: ignore[index]
    # Ensure old_json is string
    if not old_json:
        old_json = "{}"

    user_content = f"OLD profile:\n{old_json}\n\nNEW messages:\n{transcript}"

    def _parse_profile(text: str) -> UserProfile | None:
        data = extract_json(text)
        if data is None or not isinstance(data, dict):
            return None
        try:
            return UserProfile.model_validate(data)
        except Exception:
            logger.debug("UserProfile validation failed for data: %s", data, exc_info=True)
            return None

    # First attempt
    try:
        res = await llm.chat(
            [
                {"role": "system", "content": MEMORY_MERGE_SYSTEM},
                {"role": "user", "content": user_content},
            ],
            feature="memory",
            json_mode=True,
        )
    except Exception as e:
        logger.warning("memory merge LLM call failed for %s/%s: %s", chat_id, user_id, e, exc_info=True)
        # Keep old profile, delete job to avoid poison loop
        try:
            await queries.delete_memory_job(conn, chat_id, user_id)
        except Exception:
            logger.debug("delete_memory_job failed after LLM error", exc_info=True)
        return

    profile_obj = _parse_profile(res.text or "")
    if profile_obj is None:
        # Retry once with extra instruction
        try:
            res2 = await llm.chat(
                [
                    {"role": "system", "content": MEMORY_MERGE_SYSTEM},
                    {"role": "user", "content": user_content},
                    {"role": "user", "content": "Respond with valid JSON only."},
                ],
                feature="memory",
                json_mode=True,
            )
            profile_obj = _parse_profile(res2.text or "")
        except Exception as e:
            logger.warning("memory merge retry failed for %s/%s: %s", chat_id, user_id, e, exc_info=True)
            profile_obj = None

    if profile_obj is None:
        logger.warning("memory merge malformed after retry for %s/%s — keeping old profile", chat_id, user_id)
        try:
            await queries.delete_memory_job(conn, chat_id, user_id)
        except Exception:
            logger.debug("delete_memory_job failed after malformed", exc_info=True)
        return

    # Compute activity_score and msg_count
    try:
        old_score = float(profile["activity_score"]) if profile is not None else 0.0  # type: ignore[index]
    except Exception:
        old_score = 0.0
    batch_size = int(getattr(settings, "memory_batch_size", 50))
    if batch_size <= 0:
        batch_size = 50
    # Formula from spec: min(5.0, (old_score or 0)*0.5 + 2.5*(len(rows)/batch_size)) rounded 2 decimals
    new_score = min(5.0, old_score * 0.5 + 2.5 * (len(rows) / batch_size))
    new_score = round(new_score, 2)
    try:
        old_msg_count = int(profile["msg_count"]) if profile is not None else 0  # type: ignore[index]
    except Exception:
        old_msg_count = 0
    new_msg_count = old_msg_count + len(rows)
    try:
        last_id = int(rows[-1].get("id", watermark))  # type: ignore[arg-type]
    except Exception:
        last_id = watermark
    now_ms = int(time.time() * 1000)

    try:
        await queries.upsert_profile(
            conn,
            chat_id,
            user_id,
            new_score,
            new_msg_count,
            last_id,
            profile_obj.model_dump_json(),
            now_ms,
        )
        await queries.delete_memory_job(conn, chat_id, user_id)
    except Exception:
        logger.warning("upsert_profile/delete_memory_job failed for %s/%s", chat_id, user_id, exc_info=True)


async def drain_pending(db, llm, settings) -> int:
    """Process all pending memory jobs sequentially.

    Returns count processed.
    """
    conn = db.conn
    try:
        jobs = await queries.pending_memory_jobs(conn)
    except Exception:
        logger.warning("pending_memory_jobs fetch failed", exc_info=True)
        return 0

    count = 0
    for job in jobs:
        try:
            await run_memory_merge(db, llm, settings, int(job["chat_id"]), int(job["user_id"]))  # type: ignore[index]
            count += 1
        except Exception:
            logger.warning("drain_pending job failed for %s", job, exc_info=True)
            count += 1
    return count


async def reaper_tick(db, settings, factor: float = 0.5) -> int:
    """Decay inactive profiles not updated in 24h.

    Returns rowcount.
    """
    conn = db.conn
    now_ms = int(time.time() * 1000)
    try:
        rowcount = await queries.decay_inactive_profiles(conn, now_ms, quiet_hours=24, factor=float(factor))
        return int(rowcount)
    except Exception:
        logger.warning("reaper_tick failed", exc_info=True)
        return 0
