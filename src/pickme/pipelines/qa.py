"""Q&A pipeline — grounded answer with rolling summary, profiles, recent window."""

from __future__ import annotations

import logging

from pickme.db import queries
from pickme.llm.client import LLMError  # type: ignore
from pickme.llm.prompts import QA_SYSTEM
from pickme.pipelines.render import build_aliases, dealias, estimate_tokens, render_transcript, sanitize_html
from pickme.schemas.profile import UserProfile

logger = logging.getLogger("pickme.pipelines")

class PipelineError(Exception):  # type: ignore[no-redef]
    pass


async def run_qa(
    db, llm, settings, chat_id: int, question: str, asker_id: int | None = None, quote: str | None = None
) -> str:
    """Answer a question grounded in chat context.

    The asker's identity is passed to the LLM as an alias (privacy rule §4);
    ``dealias`` renders the real display name in the final reply.
    Raises :class:`PipelineError` for user-facing errors or LLM failures.
    """
    from pickme.pipelines import PipelineError as _PE

    conn = db.conn

    # Check existence of stored messages
    try:
        recent_check = await queries.get_last_messages(conn, chat_id, 1)
    except Exception as e:
        raise _PE(f"Failed to fetch messages: {e}") from e

    if not recent_check:
        raise _PE("Я ещё ничего не видел в этом чате — напишите что-нибудь!")

    # Chat row for rolling_summary
    try:
        chat = await queries.get_chat(conn, chat_id)
    except Exception:
        chat = None

    rolling_summary = ""
    if chat is not None:
        try:
            rolling_summary = str(chat.get("rolling_summary") or "")  # type: ignore[union-attr]
        except Exception:
            rolling_summary = ""
        # Limit to <=2k tokens ~8000 chars
        if estimate_tokens(rolling_summary) > 2000:
            rolling_summary = rolling_summary[:8000]

    # Top profiles (5) rendered compactly — small prompt = fast prefill on free tier
    try:
        profiles = await queries.top_profiles(conn, chat_id, 5)
    except Exception:
        profiles = []

    # Recent window: last 60 messages, trimmed to <=10000 tokens
    # (older history is already covered by the rolling summary)
    try:
        window_rows = await queries.get_last_messages(conn, chat_id, 60)
    except Exception as e:
        raise _PE(f"Failed to fetch context: {e}") from e

    aliases = build_aliases(window_rows)

    # Build profile rendering with shared aliases (extend aliases for profile-only users)
    # Extend aliases to include profile users not in window
    extended_aliases = dict(aliases)
    next_idx = len(extended_aliases) + 1
    for prof in profiles:
        try:
            uid = int(prof["user_id"])  # type: ignore[index]
            if uid not in extended_aliases:
                extended_aliases[uid] = f"user_{next_idx}"
                next_idx += 1
        except Exception:
            continue

    # Ensure the asker has an alias so identity questions work end-to-end:
    # the LLM answers with the alias; dealias renders the real name locally.
    asker_alias: str | None = None
    if asker_id is not None:
        try:
            aid = int(asker_id)
            if aid not in extended_aliases:
                extended_aliases[aid] = f"user_{next_idx}"
                next_idx += 1
            asker_alias = extended_aliases[aid]
        except Exception:
            asker_alias = None

    # Render profiles compactly
    profile_lines: list[str] = []
    for prof in profiles:
        try:
            uid = int(prof["user_id"])  # type: ignore[index]
            alias = extended_aliases.get(uid, f"user_{uid}")
            pj = prof.get("profile_json", "{}")  # type: ignore[union-attr]
            try:
                up = UserProfile.model_validate_json(pj)  # type: ignore[arg-type]
                narrative = up.narrative or ""
                topics = ", ".join(up.topics) if up.topics else ""
                if narrative and topics:
                    line = f"{alias}: {narrative} — topics: {topics}"
                elif narrative:
                    line = f"{alias}: {narrative}"
                elif topics:
                    line = f"{alias}: topics: {topics}"
                else:
                    line = f"{alias}: (no profile narrative)"
            except Exception:
                line = f"{alias}: {pj[:200]}"
            profile_lines.append(line)
        except Exception:
            continue
    profiles_rendered = "\n".join(profile_lines) if profile_lines else "(no profiles yet)"

    # Trim window to token budget
    transcript = render_transcript(window_rows, aliases, with_time=True)
    # Trim oldest-first until <=10000
    while window_rows and estimate_tokens(transcript) > 10000:
        window_rows = window_rows[1:]
        aliases = build_aliases(window_rows)
        transcript = render_transcript(window_rows, aliases, with_time=True)

    # Build final context prompt
    parts: list[str] = []
    if rolling_summary:
        parts.append(f"Rolling summary:\n{rolling_summary}")
    parts.append(f"Top profiles:\n{profiles_rendered}")
    parts.append(f"Recent messages:\n{transcript}")
    if quote:
        q = str(quote).strip()
        if len(q) > 500:
            q = q[:500] + "…"
        parts.append(f"The asker replied to this earlier message:\n{q}")
    if asker_alias:
        parts.append(f"Question (from {asker_alias}): {question}")
    else:
        parts.append(f"Question: {question}")
    user_content = "\n\n".join(parts)

    try:
        res = await llm.chat(
            [
                {"role": "system", "content": QA_SYSTEM},
                {"role": "user", "content": user_content},
            ],
            feature="qa",
        )
    except LLMError as e:
        raise _PE(str(e) or "LLM unavailable for Q&A.") from e
    except Exception as e:  # pragma: no cover
        raise _PE(f"Q&A failed: {e}") from e

    raw_answer = res.text or ""

    # Build names map for dealias: fetch display names for all involved user_ids
    # Collect user_ids from window_rows + profiles
    user_ids: set[int] = set()
    for r in window_rows:
        uid = r.get("user_id")
        if uid is not None:
            try:
                user_ids.add(int(uid))  # type: ignore[arg-type]
            except Exception:
                pass
    for prof in profiles:
        try:
            user_ids.add(int(prof["user_id"]))  # type: ignore[index]
        except Exception:
            pass
    if asker_id is not None:
        try:
            user_ids.add(int(asker_id))
        except Exception:
            pass

    names: dict[int, str] = {}
    if user_ids:
        try:
            placeholders = ",".join("?" for _ in user_ids)
            cursor = await conn.execute(
                f"SELECT user_id, display_name FROM users WHERE user_id IN ({placeholders})",
                tuple(user_ids),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            for r in rows:
                try:
                    uid = int(r["user_id"])  # type: ignore[index]
                    disp = r.get("display_name")  # type: ignore[union-attr]
                    if disp:
                        names[uid] = str(disp)
                except Exception:
                    continue
        except Exception:
            logger.debug("fetch names for QA dealias failed", exc_info=True)

    # Dealias then sanitize (spec says final is sanitized, dealiased HTML)
    # Need aliases for dealias: use extended_aliases (covers profiles + window)
    dealiased = dealias(raw_answer, extended_aliases, names)
    sanitized = sanitize_html(dealiased)
    return sanitized
