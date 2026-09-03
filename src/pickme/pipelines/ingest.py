"""Ingest pipeline — store message and check memory trigger."""

from __future__ import annotations

import logging
import time

from pickme.db import queries

logger = logging.getLogger("pickme.pipelines")


async def ingest_message(
    db,
    settings,
    *,
    chat_id: int,
    chat_title: str | None,
    user_id: int | None,
    username: str | None,
    display_name: str | None,
    text: str | None,
    reply_to_message_id: int | None,
    media_type: str | None,
    media_meta: str | None,
    created_at: int,
    is_bot: bool,
    is_edited: bool,
) -> tuple[int, bool]:
    """Ingest a single Telegram message.

    Returns ``(message_id, memory_due)``. ``memory_due`` indicates a memory
    merge should be queued.
    """
    conn = db.conn

    # Upsert chat
    await queries.upsert_chat(conn, chat_id, chat_title, created_at)

    # Upsert user if present
    if user_id is not None:
        await queries.upsert_user(conn, user_id, username, display_name, created_at)

    # Insert message
    message_id = await queries.insert_message(
        conn,
        chat_id,
        user_id,
        text,
        reply_to_message_id,
        media_type,
        media_meta,
        created_at,
        is_bot,
        is_edited,
    )

    # Increment chat counter
    await queries.increment_chat_message_count(conn, chat_id)

    # Memory trigger is per (chat, user) — only for real users
    memory_due = False
    if user_id is not None:
        profile = await queries.get_profile(conn, chat_id, user_id)
        watermark = int(profile["since_message_id"]) if profile is not None else 0  # type: ignore[index]
        try:
            count = await queries.count_user_messages_since(conn, chat_id, user_id, watermark)
        except Exception:
            logger.debug("count_user_messages_since failed", exc_info=True)
            count = 0

        due_by_count = count >= int(getattr(settings, "memory_batch_size", 50))
        due_by_ttl = False
        if profile is not None:
            try:
                last_updated = int(profile.get("last_updated_at", 0))  # type: ignore[union-attr]
                ttl_ms = int(getattr(settings, "memory_ttl_hours", 6)) * 3600 * 1000
                now_ms = int(created_at)
                if now_ms - last_updated >= ttl_ms:
                    due_by_ttl = True
            except Exception:
                logger.debug("TTL check failed", exc_info=True)

        if due_by_count or due_by_ttl:
            watermark_for_job = watermark
            try:
                await queries.upsert_memory_job(conn, chat_id, user_id, watermark_for_job, created_at)
                memory_due = True
            except Exception:
                logger.debug("upsert_memory_job failed", exc_info=True)

    return message_id, memory_due


async def remember_own_message(db, chat_id: int, text: str) -> None:
    """Store one of the bot's own sent replies so future prompts include it.

    Stored with ``user_id`` NULL + ``is_bot=1``; :func:`render_transcript`
    labels such rows ``bot`` — telling the LLM these are its own earlier
    messages. Fire-and-forget: never raises.
    """
    if not text:
        return
    try:
        now_ms = int(time.time() * 1000)
        conn = db.conn
        await queries.insert_message(
            conn,
            chat_id,
            None,
            text[:3900],
            None,
            None,
            None,
            now_ms,
            True,
            False,
        )
        await queries.increment_chat_message_count(conn, chat_id)
    except Exception:
        logger.debug("remember_own_message failed", exc_info=True)
