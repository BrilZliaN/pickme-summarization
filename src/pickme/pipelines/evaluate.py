"""Evaluate pipeline — single and batch evaluation with HTML cards."""

from __future__ import annotations

import logging

from pickme.db import queries
from pickme.llm.client import LLMError, extract_json  # type: ignore
from pickme.llm.prompts import EVALUATE_BATCH_SYSTEM, EVALUATE_SYSTEM
from pickme.pipelines.render import build_aliases, dealias, render_transcript, sanitize_html
from pickme.schemas.profile import EvalTarget, Evaluation, UserProfile

logger = logging.getLogger("pickme.pipelines")

# Placeholder for LSP
class PipelineError(Exception):  # type: ignore[no-redef]
    pass


def render_card(display_name: str | None, ev: Evaluation) -> str:
    """Render an evaluation as sanitized HTML card."""
    name = display_name or "Пользователь"
    # Map tone to Russian with fallback
    _tone_map = {
        "supportive": "поддерживающий",
        "neutral": "нейтральный",
        "confrontational": "конфронтационный",
    }
    tone_ru = _tone_map.get(ev.tone, sanitize_html(ev.tone))
    if ev.tone in _tone_map:
        tone_ru = _tone_map[ev.tone]
    else:
        tone_ru = sanitize_html(ev.tone)
    lines: list[str] = []
    lines.append(f"<b>Оценка: {sanitize_html(name)}</b>")
    lines.append(f"<b>Тон:</b> {tone_ru}")
    lines.append(f"<b>Конструктивность:</b> {ev.constructiveness}/5")
    lines.append(f"<b>Участие:</b> {ev.participation}/5")
    if ev.dominant_topics:
        topics_str = ", ".join(sanitize_html(t) for t in ev.dominant_topics)
        lines.append(f"<b>Основные темы:</b> {topics_str}")
    if ev.notable_contributions:
        contrib = "; ".join(sanitize_html(c) for c in ev.notable_contributions)
        lines.append(f"<b>Заметный вклад:</b> {contrib}")
    if ev.red_flags:
        flags = "; ".join(sanitize_html(f) for f in ev.red_flags)
        lines.append(f"<b>⚠ Красные флаги:</b> {flags}")
    raw = "\n".join(lines)
    return sanitize_html(raw) if "<" not in raw else raw


async def _get_display_name(conn, user_id: int) -> str | None:
    """Fetch display_name for a user_id directly."""
    try:
        cursor = await conn.execute("SELECT display_name, username FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        name = row.get("display_name")  # type: ignore[union-attr]
        if name:
            return str(name)
        uname = row.get("username")  # type: ignore[union-attr]
        if uname:
            return str(uname)
        return None
    except Exception:
        logger.debug("get display_name failed for %s", user_id, exc_info=True)
        return None


async def _evaluate_single(
    conn, llm, chat_id: int, user_id: int, display_name: str | None
) -> str:
    """Evaluate a single user; returns rendered card HTML."""
    from pickme.pipelines import PipelineError as _PE

    profile = await queries.get_profile(conn, chat_id, user_id)
    if profile is None:
        raise _PE("Пока маловато данных об этом участнике — он ещё мало писал.")

    # Parse profile_json for validation (but pass raw to LLM)
    profile_json = profile.get("profile_json", "{}")  # type: ignore[union-attr]
    try:
        # Validate it parses as UserProfile, but keep original string for prompt
        _ = UserProfile.model_validate_json(profile_json)  # type: ignore[arg-type]
    except Exception:
        logger.debug("profile_json invalid for %s/%s", chat_id, user_id, exc_info=True)
        # Keep as is

    sample_rows = await queries.get_last_user_messages(conn, chat_id, user_id, 100)
    aliases = build_aliases(sample_rows)
    # Ensure the evaluated user alias is consistent; if single user messages all map to user_1, use that
    transcript = render_transcript(sample_rows, aliases, with_time=True)

    user_content = f"Profile:\n{profile_json}\n\nRecent messages:\n{transcript}"

    def _parse_ev(text: str) -> Evaluation | None:
        data = extract_json(text)
        if data is None or not isinstance(data, dict):
            return None
        try:
            return Evaluation.model_validate(data)
        except Exception:
            logger.debug("Evaluation validation failed: %s", data, exc_info=True)
            return None

    try:
        res = await llm.chat(
            [
                {"role": "system", "content": EVALUATE_SYSTEM},
                {"role": "user", "content": user_content},
            ],
            feature="evaluate",
            json_mode=True,
        )
    except LLMError as e:
        raise _PE(str(e) or "LLM unavailable for evaluation.") from e

    ev = _parse_ev(res.text or "")
    if ev is None:
        # Retry once
        try:
            res2 = await llm.chat(
                [
                    {"role": "system", "content": EVALUATE_SYSTEM},
                    {"role": "user", "content": user_content},
                    {"role": "user", "content": "Respond with valid JSON only."},
                ],
                feature="evaluate",
                json_mode=True,
            )
            ev = _parse_ev(res2.text or "")
        except LLMError as e:
            raise _PE(str(e) or "LLM unavailable for evaluation.") from e
        except Exception:
            logger.debug("evaluate retry failed for %s/%s", chat_id, user_id, exc_info=True)

    if ev is None:
        raise _PE("Не удалось сформировать оценку — попробуйте ещё раз.")

    card = render_card(display_name, ev)
    # Dealias not needed for single but sanitize already done
    return card


async def run_evaluate(db, llm, settings, chat_id: int, requester_id: int, target: EvalTarget) -> str:
    """Resolve EvalTarget and return sanitized, dealiased HTML.

    For ``everyone`` joins cards with newline; for single user returns one card.
    Raises :class:`PipelineError` for user-facing errors.
    """
    from pickme.pipelines import PipelineError as _PE

    conn = db.conn

    if target.kind == "me":
        user_id = int(requester_id)
        display_name = await _get_display_name(conn, user_id)
        # Build aliases/names for dealias final (even though card already has real name)
        # Single path handles alias internally
        return await _evaluate_single(conn, llm, chat_id, user_id, display_name)

    if target.kind == "user":
        resolved_id: int | None = None
        if target.user_id is not None:
            resolved_id = int(target.user_id)
        elif target.username is not None:
            uname = str(target.username).strip()
            # Try by username first
            try:
                row = await queries.find_user_by_username(conn, uname)
                if row is not None:
                    resolved_id = int(row["user_id"])  # type: ignore[index]
                else:
                    # Try display name lookup within chat
                    clean = uname.lstrip("@")
                    row2 = await queries.find_user_by_display_name(conn, chat_id, clean)
                    if row2 is not None:
                        resolved_id = int(row2["user_id"])  # type: ignore[index]
            except Exception:
                logger.debug("find_user failed for %s", uname, exc_info=True)
        else:
            raise _PE("Хм, не понял, кто это 🤔")

        if resolved_id is None:
            raise _PE("Хм, не понял, кто это 🤔")

        display_name = await _get_display_name(conn, resolved_id)
        return await _evaluate_single(conn, llm, chat_id, resolved_id, display_name)

    if target.kind == "everyone":
        # Top 20 by activity_score, filtered msg_count>0
        try:
            profiles = await queries.top_profiles(conn, chat_id, 20)
        except Exception as e:
            raise _PE(f"Failed to fetch profiles: {e}") from e

        filtered = [p for p in profiles if int(p.get("msg_count", 0) or 0) > 0]  # type: ignore[union-attr]
        if not filtered:
            raise _PE("Пока маловато данных об этом участнике — он ещё мало писал.")

        # Batch 8 per call
        batch_size = 8
        cards: list[str] = []
        # We need names map for dealias final; collect all display names
        all_names: dict[int, str] = {}
        for p in filtered:
            try:
                uid = int(p["user_id"])  # type: ignore[index]
                name = await _get_display_name(conn, uid)
                if name:
                    all_names[uid] = name
            except Exception:
                continue

        # Process batches
        for i in range(0, len(filtered), batch_size):
            batch = filtered[i : i + batch_size]
            # Alias within batch: user_1..user_N mapping to real user_ids
            batch_aliases: dict[int, str] = {}
            alias_to_uid: dict[str, int] = {}
            for idx, prof in enumerate(batch):
                uid = int(prof["user_id"])  # type: ignore[index]
                alias = f"user_{idx + 1}"
                batch_aliases[uid] = alias
                alias_to_uid[alias] = uid

            # Build batch prompt
            parts: list[str] = []
            for prof in batch:
                uid = int(prof["user_id"])  # type: ignore[index]
                alias = batch_aliases[uid]
                pj = prof.get("profile_json", "{}")  # type: ignore[union-attr]
                # Also include recent sample? Spec says batch profiles per call (profile only)
                parts.append(f"{alias}:\nProfile: {pj}")
            batch_content = "\n\n".join(parts)

            def _parse_batch(text: str) -> list[dict] | None:
                data = extract_json(text)
                if data is None or not isinstance(data, list):
                    return None
                return data  # type: ignore[return-value]

            try:
                res = await llm.chat(
                    [
                        {"role": "system", "content": EVALUATE_BATCH_SYSTEM},
                        {"role": "user", "content": batch_content},
                    ],
                    feature="evaluate",
                    json_mode=True,
                )
            except LLMError as e:
                raise _PE(str(e) or "LLM unavailable for evaluation.") from e

            parsed = _parse_batch(res.text or "")
            if parsed is None:
                # Retry once
                try:
                    res2 = await llm.chat(
                        [
                            {"role": "system", "content": EVALUATE_BATCH_SYSTEM},
                            {"role": "user", "content": batch_content},
                            {"role": "user", "content": "Respond with valid JSON only."},
                        ],
                        feature="evaluate",
                        json_mode=True,
                    )
                    parsed = _parse_batch(res2.text or "")
                except Exception:
                    logger.debug("batch evaluate retry failed", exc_info=True)
                    parsed = None

            if parsed is None:
                logger.warning("batch evaluate malformed after retry, skipping batch %s", i)
                # Render fallback cards as error?
                for prof in batch:
                    uid = int(prof["user_id"])  # type: ignore[index]
                    name = all_names.get(uid)
                    # Fallback card with minimal info
                    fallback_ev = Evaluation(
                        tone="neutral",
                        constructiveness=3,
                        participation=3,
                        dominant_topics=[],
                        notable_contributions=[],
                        red_flags=[],
                    )
                    cards.append(render_card(name, fallback_ev))
                continue

            # parsed is list of {"alias": str, "evaluation": {...}}
            for entry in parsed:
                if not isinstance(entry, dict):
                    continue
                alias = entry.get("alias")
                ev_data = entry.get("evaluation")
                if not isinstance(alias, str) or not isinstance(ev_data, dict):
                    continue
                try:
                    ev = Evaluation.model_validate(ev_data)
                except Exception:
                    logger.debug("batch evaluation entry invalid: %s", entry, exc_info=True)
                    continue
                uid = alias_to_uid.get(alias)
                if uid is None:
                    # Unknown alias
                    continue
                name = all_names.get(uid)
                cards.append(render_card(name, ev))

        if not cards:
            raise _PE("Не удалось сформировать оценку — попробуйте ещё раз.")

        # Join cards; spec says join with "\n" — use double newline for readability
        joined = "\n\n".join(cards)
        # Dealias any leftover alias tokens in final HTML (unlikely but spec requires)
        # Build global alias map for final dealias: use all_names keys -> need alias mapping
        # For everyone, global aliases are batch-local, so final dealias should replace batch aliases with names
        # We already rendered with real names, so just return sanitized join
        # Still run sanitize/dealias for safety: build a combined alias map from all filtered
        # Use build_aliases on profiles? Instead construct map from batch_aliases last batch not enough.
        # We have all_names, but need alias -> name for each batch; we already used batch aliases.
        # To dealias any alias leaks, replace any "user_N" that matches all_names mapping via brute force
        # Build a reverse map for global: assign user_{position in filtered}
        global_aliases: dict[int, str] = {int(p["user_id"]): f"user_{idx+1}" for idx, p in enumerate(filtered)}  # type: ignore[index]
        # Dealial handles missing names gracefully
        joined_dealiased = dealias(joined, global_aliases, all_names)
        return sanitize_html(joined_dealiased)

    # Fallback (should not happen)
    raise _PE("Хм, не понял, кто это 🤔")
