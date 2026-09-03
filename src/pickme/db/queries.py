"""Parameterized SQL helpers — one function per query, no SQLite-only functions."""

from __future__ import annotations

import aiosqlite

# ---------------------------------------------------------------------------
# chats
# ---------------------------------------------------------------------------


async def upsert_chat(conn: aiosqlite.Connection, chat_id: int, title: str | None, now_ms: int) -> None:
    """Insert a chat row if missing and refresh its title.

    Uses INSERT OR IGNORE for the initial row, then updates the title so that
    renames are reflected without overwriting other columns.
    """
    await conn.execute(
        "INSERT OR IGNORE INTO chats(chat_id, title, created_at, message_count) VALUES (?, ?, ?, 0)",
        (chat_id, title, now_ms),
    )
    if title is not None:
        await conn.execute("UPDATE chats SET title = ? WHERE chat_id = ?", (title, chat_id))
    await conn.commit()


async def get_chat(conn: aiosqlite.Connection, chat_id: int) -> dict | None:
    """Return a chat row as a dict, or ``None`` if not found."""
    cursor = await conn.execute("SELECT * FROM chats WHERE chat_id = ?", (chat_id,))
    row = await cursor.fetchone()
    await cursor.close()
    return row  # type: ignore[return-value]


async def set_rolling_summary(conn: aiosqlite.Connection, chat_id: int, summary: str, at_ms: int) -> None:
    """Update the rolling summary and its timestamp for a chat."""
    await conn.execute(
        "UPDATE chats SET rolling_summary = ?, rolling_summary_at = ? WHERE chat_id = ?",
        (summary, at_ms, chat_id),
    )
    await conn.commit()


async def count_messages_since(conn: aiosqlite.Connection, chat_id: int, since_message_id: int) -> int:
    """Count messages in a chat with ``id`` strictly greater than the watermark."""
    cursor = await conn.execute(
        "SELECT COUNT(*) AS cnt FROM messages WHERE chat_id = ? AND id > ?",
        (chat_id, since_message_id),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        return 0
    return int(row["cnt"])


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------


async def upsert_user(
    conn: aiosqlite.Connection,
    user_id: int,
    username: str | None,
    display_name: str | None,
    now_ms: int,
) -> None:
    """Insert or update a user row, refreshing username/display_name/last_seen."""
    await conn.execute(
        "INSERT INTO users(user_id, username, display_name, first_seen_at, last_seen_at)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(user_id) DO UPDATE SET"
        " username=excluded.username,"
        " display_name=excluded.display_name,"
        " last_seen_at=excluded.last_seen_at",
        (user_id, username, display_name, now_ms, now_ms),
    )
    await conn.commit()


async def find_user_by_username(conn: aiosqlite.Connection, username: str) -> dict | None:
    """Find a user by username (without leading ``@``).

    Comparison is case-insensitive in the caller's Python layer via LOWER().
    """
    clean = username.lstrip("@")
    cursor = await conn.execute(
        "SELECT * FROM users WHERE username IS NOT NULL AND LOWER(username) = LOWER(?)",
        (clean,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return row  # type: ignore[return-value]


async def find_user_by_display_name(conn: aiosqlite.Connection, chat_id: int, name: str) -> dict | None:
    """Find a user who has messages in *chat_id* by case-insensitive display name."""
    cursor = await conn.execute(
        "SELECT u.* FROM users u"
        " JOIN messages m ON m.user_id = u.user_id"
        " WHERE m.chat_id = ? AND u.display_name IS NOT NULL AND LOWER(u.display_name) = LOWER(?)"
        " LIMIT 1",
        (chat_id, name),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return row  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------


async def insert_message(
    conn: aiosqlite.Connection,
    chat_id: int,
    user_id: int | None,
    text: str | None,
    reply_to_message_id: int | None,
    media_type: str | None,
    media_meta: str | None,
    created_at: int,
    is_bot: bool | int,
    is_edited: bool | int,
) -> int:
    """Insert a message and return its new ``id``."""
    cursor = await conn.execute(
        "INSERT INTO messages(chat_id, user_id, text, reply_to_message_id, media_type, media_meta, created_at, is_bot, is_edited)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            chat_id,
            user_id,
            text,
            reply_to_message_id,
            media_type,
            media_meta,
            created_at,
            int(bool(is_bot)),
            int(bool(is_edited)),
        ),
    )
    new_id: int = cursor.lastrowid  # type: ignore[assignment]
    await cursor.close()
    await conn.commit()
    return new_id


async def get_last_messages(conn: aiosqlite.Connection, chat_id: int, limit: int) -> list[dict]:
    """Return the last *limit* messages for a chat, oldest first."""
    cursor = await conn.execute(
        "SELECT * FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
        (chat_id, limit),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    # Reverse to ascending order (oldest first).
    return list(reversed(rows))  # type: ignore[arg-type]


async def get_messages_since(
    conn: aiosqlite.Connection,
    chat_id: int,
    since_message_id: int,
    limit: int | None = None,
) -> list[dict]:
    """Return messages with ``id`` > watermark, ordered ascending."""
    if limit is not None:
        cursor = await conn.execute(
            "SELECT * FROM messages WHERE chat_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
            (chat_id, since_message_id, limit),
        )
    else:
        cursor = await conn.execute(
            "SELECT * FROM messages WHERE chat_id = ? AND id > ? ORDER BY id ASC",
            (chat_id, since_message_id),
        )
    rows = await cursor.fetchall()
    await cursor.close()
    return rows  # type: ignore[return-value]


async def get_user_messages_since(
    conn: aiosqlite.Connection,
    chat_id: int,
    user_id: int,
    since_message_id: int,
) -> list[dict]:
    """Return messages for a specific user with ``id`` > watermark."""
    cursor = await conn.execute(
        "SELECT * FROM messages WHERE chat_id = ? AND user_id = ? AND id > ? ORDER BY id ASC",
        (chat_id, user_id, since_message_id),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return rows  # type: ignore[return-value]


async def get_last_user_messages(
    conn: aiosqlite.Connection,
    chat_id: int,
    user_id: int,
    limit: int,
) -> list[dict]:
    """Return the last *limit* messages for a specific user in a chat, oldest first."""
    cursor = await conn.execute(
        "SELECT * FROM messages WHERE chat_id = ? AND user_id = ? ORDER BY id DESC LIMIT ?",
        (chat_id, user_id, limit),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return list(reversed(rows))  # type: ignore[arg-type]


async def count_user_messages_since(
    conn: aiosqlite.Connection,
    chat_id: int,
    user_id: int,
    since_message_id: int,
) -> int:
    """Count messages for a specific user with ``id`` > watermark."""
    cursor = await conn.execute(
        "SELECT COUNT(*) AS cnt FROM messages WHERE chat_id = ? AND user_id = ? AND id > ?",
        (chat_id, user_id, since_message_id),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        return 0
    return int(row["cnt"])


async def increment_chat_message_count(conn: aiosqlite.Connection, chat_id: int) -> None:
    """Increment the denormalized ``message_count`` for a chat."""
    await conn.execute(
        "UPDATE chats SET message_count = message_count + 1 WHERE chat_id = ?",
        (chat_id,),
    )
    await conn.commit()


# ---------------------------------------------------------------------------
# user_profiles
# ---------------------------------------------------------------------------


async def get_profile(conn: aiosqlite.Connection, chat_id: int, user_id: int) -> dict | None:
    """Return a user profile row, or ``None``."""
    cursor = await conn.execute(
        "SELECT * FROM user_profiles WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return row  # type: ignore[return-value]


async def upsert_profile(
    conn: aiosqlite.Connection,
    chat_id: int,
    user_id: int,
    activity_score: float,
    msg_count: int,
    since_message_id: int,
    profile_json: str,
    now_ms: int,
) -> None:
    """Insert or update a profile; ``version`` increments on conflict."""
    await conn.execute(
        "INSERT INTO user_profiles(chat_id, user_id, version, activity_score, msg_count, since_message_id, last_updated_at, profile_json)"
        " VALUES (?, ?, 1, ?, ?, ?, ?, ?)"
        " ON CONFLICT(chat_id, user_id) DO UPDATE SET"
        " version=version + 1,"
        " activity_score=excluded.activity_score,"
        " msg_count=excluded.msg_count,"
        " since_message_id=excluded.since_message_id,"
        " last_updated_at=excluded.last_updated_at,"
        " profile_json=excluded.profile_json",
        (chat_id, user_id, activity_score, msg_count, since_message_id, now_ms, profile_json),
    )
    await conn.commit()


async def top_profiles(conn: aiosqlite.Connection, chat_id: int, k: int) -> list[dict]:
    """Return top-*k* profiles ordered by ``activity_score`` descending."""
    cursor = await conn.execute(
        "SELECT * FROM user_profiles WHERE chat_id = ? ORDER BY activity_score DESC LIMIT ?",
        (chat_id, k),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return rows  # type: ignore[return-value]


async def decay_inactive_profiles(
    conn: aiosqlite.Connection,
    now_ms: int,
    quiet_hours: int,
    factor: float,
) -> int:
    """Decay ``activity_score`` for profiles not updated within *quiet_hours*.

    Returns the number of rows updated.
    """
    threshold = now_ms - quiet_hours * 3600 * 1000
    cursor = await conn.execute(
        "UPDATE user_profiles SET activity_score = activity_score * ? WHERE last_updated_at < ?",
        (factor, threshold),
    )
    rowcount: int = cursor.rowcount  # type: ignore[assignment]
    await cursor.close()
    await conn.commit()
    return rowcount


# ---------------------------------------------------------------------------
# memory_jobs
# ---------------------------------------------------------------------------


async def upsert_memory_job(
    conn: aiosqlite.Connection,
    chat_id: int,
    user_id: int,
    since_message_id: int,
    now_ms: int,
) -> None:
    """Create or replace a pending memory job for ``(chat_id, user_id)``."""
    await conn.execute(
        "INSERT INTO memory_jobs(chat_id, user_id, since_message_id, status, created_at)"
        " VALUES (?, ?, ?, 'pending', ?)"
        " ON CONFLICT(chat_id, user_id) DO UPDATE SET"
        " since_message_id=excluded.since_message_id,"
        " status='pending',"
        " created_at=excluded.created_at",
        (chat_id, user_id, since_message_id, now_ms),
    )
    await conn.commit()


async def pending_memory_jobs(conn: aiosqlite.Connection) -> list[dict]:
    """Return all memory jobs with ``status='pending'``."""
    cursor = await conn.execute("SELECT * FROM memory_jobs WHERE status = 'pending'")
    rows = await cursor.fetchall()
    await cursor.close()
    return rows  # type: ignore[return-value]


async def delete_memory_job(conn: aiosqlite.Connection, chat_id: int, user_id: int) -> None:
    """Delete a memory job for ``(chat_id, user_id)``."""
    await conn.execute(
        "DELETE FROM memory_jobs WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id),
    )
    await conn.commit()


# ---------------------------------------------------------------------------
# llm_log
# ---------------------------------------------------------------------------


async def insert_llm_log(
    conn: aiosqlite.Connection,
    feature: str,
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    ok: bool,
    error: str | None,
    now_ms: int,
) -> None:
    """Insert an LLM call audit row."""
    await conn.execute(
        "INSERT INTO llm_log(feature, provider, model, prompt_tokens, completion_tokens, created_at, ok, error)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (feature, provider, model, prompt_tokens, completion_tokens, now_ms, int(bool(ok)), error),
    )
    await conn.commit()


# ---------------------------------------------------------------------------
# forget
# ---------------------------------------------------------------------------


async def delete_profile(conn: aiosqlite.Connection, chat_id: int, user_id: int) -> None:
    """Delete a single user profile."""
    await conn.execute(
        "DELETE FROM user_profiles WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id),
    )
    await conn.commit()


async def delete_user_messages(conn: aiosqlite.Connection, chat_id: int, user_id: int) -> None:
    """Delete all messages for a specific user in a chat."""
    await conn.execute(
        "DELETE FROM messages WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id),
    )
    await conn.commit()


async def delete_chat_data(conn: aiosqlite.Connection, chat_id: int) -> None:
    """Delete all data for a chat: row, messages, profiles, and memory jobs."""
    await conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
    await conn.execute("DELETE FROM user_profiles WHERE chat_id = ?", (chat_id,))
    await conn.execute("DELETE FROM memory_jobs WHERE chat_id = ?", (chat_id,))
    await conn.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
    await conn.commit()


async def list_chats(conn: aiosqlite.Connection) -> list[dict]:
    """List all chats with their metadata for background timers.

    Returns:
        List of dicts with ``chat_id``, ``title``, ``message_count``,
        ``rolling_summary_at`` (may be ``None`` if never set).
    """
    cursor = await conn.execute(
        "SELECT chat_id, title, message_count, rolling_summary_at FROM chats"
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return rows  # type: ignore[return-value]
