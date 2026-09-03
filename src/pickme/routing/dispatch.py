"""NL-addressed dispatch — two-stage router then pipeline execution."""

from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

from aiogram.types import Message

from pickme.llm.prompts import HELP_TEXT
from pickme.routing.fastpath import match as fastpath_match
from pickme.routing.intent import route

logger = logging.getLogger("pickme.routing")


def _split(text: str, limit: int = 4096) -> list[str]:
    try:
        from pickme.telegram.bot import split_html  # type: ignore

        return split_html(text, limit)
    except Exception:
        if len(text) <= limit:
            return [text]
        return [text[i : i + limit] for i in range(0, len(text), limit)]


async def _send_html(message: Message, text: str, reply: bool = False) -> None:
    """Send HTML chunked; ``reply=True`` reply-links the first chunk."""
    if not text:
        text = "(пусто)"
    chunks = _split(text, 4096)
    for idx, chunk in enumerate(chunks):
        if idx > 0:
            try:
                await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")  # type: ignore[union-attr]
            except Exception:
                pass
        try:
            if idx == 0 and reply:
                await message.reply(chunk, parse_mode="HTML")
            else:
                await message.answer(chunk, parse_mode="HTML")
        except Exception:
            try:
                await message.answer(chunk)
            except Exception:
                pass


async def _remember_own(app: Any, chat_id: int, text: str) -> None:
    """Store a sent bot reply so future LLM prompts include it (labeled 'bot')."""
    try:
        from pickme.pipelines.ingest import remember_own_message

        db = getattr(app, "db", None)
        if db is not None:
            await remember_own_message(db, chat_id, text)
    except Exception:
        logger.debug("remember own message failed", exc_info=True)


async def _queue_run_and_send(
    app: Any,
    message: Message,
    kind: str,
    payload: dict[str, Any],
    ack: str,
    ack_msg: Any = None,
) -> None:
    """Ack, submit, handle errors, send result HTML.

    The ⏳ ack (own, or pre-sent via *ack_msg* by the caller) is deleted once
    a terminal reply is sent; the result is sent as a Telegram reply to the
    triggering message and remembered in DB.
    """
    if ack_msg is None:
        try:
            await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")  # type: ignore[union-attr]
        except Exception:
            pass
        try:
            ack_msg = await message.answer(ack, parse_mode="HTML")
        except Exception:
            ack_msg = None

    async def _delete_ack() -> None:
        if ack_msg is not None:
            try:
                await ack_msg.delete()
            except Exception:
                pass

    try:
        result = await app.queue.submit(kind, message.chat.id, payload)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        name = type(exc).__name__
        msg = str(exc)
        await _delete_ack()
        if name == "JobAlreadyRunning":
            await _send_html(message, "Уже занят этим — одну секундочку ⏳")
            return
        if isinstance(exc, asyncio.TimeoutError) or name == "TimeoutError":
            await _send_html(message, "😔 Не успел за отведённое время — давай ещё раз?")
            return
        if name == "LLMError" or "all tiers" in msg.lower() or "all llm tiers" in msg.lower():
            await _send_html(message, "⚠️ Нейросети сейчас не отвечают. Попробуй через минутку!")
            return
        if name == "PipelineError":
            await _send_html(message, f"⚠️ {html.escape(msg)}")
            return
        if "llm" in msg.lower() and "unavailable" in msg.lower():
            await _send_html(message, "⚠️ Нейросети сейчас не отвечают. Попробуй через минутку!")
            return
        logger.warning("queue %s failed %s: %s", kind, name, msg, exc_info=True)
        await _send_html(message, f"⚠️ Что-то пошло не так: {html.escape(msg)}" if msg else "😔 Что-то поломалось. Попробуй ещё раз чуть позже.")
        return

    html_text: str
    if hasattr(result, "html"):
        html_text = str(getattr(result, "html"))
    elif isinstance(result, str):
        html_text = result
    elif result is None:
        html_text = "Готово."
    else:
        html_text = str(result)
    try:
        await _send_html(message, html_text, reply=True)
        await _remember_own(app, message.chat.id, html_text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("send result failed %s", exc, exc_info=True)
    finally:
        await _delete_ack()


def _clamp_count(n: int | None, default: int, cap: int) -> int:
    if n is None:
        return default
    try:
        v = int(n)
    except Exception:
        return default
    if v < 1:
        v = 1
    if v > cap:
        v = cap
    return v


def _eval_target_from_str(raw: str | None, requester_id: int) -> dict[str, Any]:
    if not raw:
        return {"kind": "me"}
    s = raw.strip()
    low = s.lower()
    if low in ("everyone", "всех", "все", "all", "всем"):
        return {"kind": "everyone"}
    if low in ("me", "меня", "mnie"):
        return {"kind": "me"}
    if s.startswith("@"):
        uname = s.lstrip("@").strip().split()[0]
        return {"kind": "user", "username": uname}
    return {"kind": "user", "username": s.split()[0]}


async def _get_member_hint(app: Any, chat_id: int) -> list[str] | None:
    try:
        from pickme.db import queries as q  # type: ignore

        db = getattr(app, "db", None)
        if db is None:
            return None
        rows = await q.get_last_messages(db.conn, chat_id, 50)
        if not rows:
            return None
        aliases: dict[int, str] | None = None
        try:
            from pickme.pipelines.render import build_aliases as _build_aliases  # type: ignore

            aliases = _build_aliases(rows)
        except Exception:
            aliases = {}
            nxt = 1
            for r in rows:
                uid = r.get("user_id")
                if uid is None:
                    continue
                try:
                    uid_int = int(uid)  # type: ignore[arg-type]
                except Exception:
                    continue
                if uid_int not in aliases:
                    aliases[uid_int] = f"user_{nxt}"
                    nxt += 1
        if not aliases:
            return None
        return list(aliases.values())
    except Exception as exc:  # noqa: BLE001
        logger.debug("member_hint failed %s", exc, exc_info=True)
        return None


async def handle_addressed(app: Any, message: Message, text: str) -> None:
    settings = getattr(app, "settings", None)
    chat_id = int(message.chat.id)
    requester_id = 0
    try:
        fu = getattr(message, "from_user", None)
        if fu is not None:
            requester_id = int(getattr(fu, "id", 0) or 0)
    except Exception:
        pass

    raw_text = text.strip() if text is not None else ""
    if not raw_text:
        await _send_html(message, HELP_TEXT)
        return

    # Immediate feedback BEFORE the (possibly slow) LLM router call:
    # typing + early ack — the queue helper reuses and later deletes it.
    try:
        await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")  # type: ignore[union-attr]
    except Exception:
        pass
    early_ack = None
    try:
        early_ack = await message.answer("⏳ Секунду, думаю…", parse_mode="HTML")
    except Exception:
        early_ack = None

    async def _drop_early_ack() -> None:
        if early_ack is not None:
            try:
                await early_ack.delete()
            except Exception:
                pass

    intent = None
    try:
        use_fast = bool(getattr(settings, "router_fastpath", True)) if settings else True
        if use_fast:
            from pickme.routing.fastpath import match as _match  # type: ignore

            intent = _match(raw_text)
            if intent is not None:
                logger.info("fastpath hit %s -> %s", raw_text[:60], intent.action)
    except Exception as exc:  # noqa: BLE001
        logger.debug("fastpath failed %s", exc, exc_info=True)
        intent = None

    if intent is None:
        try:
            llm = getattr(app, "llm", None)
            if llm is None:
                from pickme.schemas.intent import Intent  # type: ignore

                intent = Intent(action="qa", count=None, target=None)
            else:
                member_hint = await _get_member_hint(app, chat_id)
                intent = await route(llm, settings, raw_text, member_hint=member_hint)
        except Exception as exc:  # noqa: BLE001
            logger.warning("route failed %s", exc, exc_info=True)
            from pickme.schemas.intent import Intent  # type: ignore

            intent = Intent(action="qa", count=None, target=None)

    try:
        default = int(getattr(settings, "summarize_default", 100)) if settings else 100
        cap = int(getattr(settings, "summarize_cap", 500)) if settings else 500
        action = getattr(intent, "action", "qa")
        logger.info("dispatch addressed action=%s text=%r", action, raw_text[:80])

        if action == "summarize":
            count_raw = getattr(intent, "count", None)
            try:
                cnt = _clamp_count(int(count_raw) if count_raw is not None else None, default, cap)
            except Exception:
                cnt = default
            await _queue_run_and_send(
                app,
                message,
                "summarize",
                {"chat_id": chat_id, "count": cnt},
                f"⏳ Секунду, листаю последние {cnt} сообщений…",
                ack_msg=early_ack,
            )
            return

        if action == "evaluate":
            raw_target = getattr(intent, "target", None)
            target_payload = _eval_target_from_str(raw_target, requester_id)
            if target_payload.get("kind") == "user" and target_payload.get("username") and not str(raw_target or "").startswith("@"):
                try:
                    from pickme.db import queries as q  # type: ignore

                    db = getattr(app, "db", None)
                    if db is not None:
                        uname = target_payload.get("username", "")
                        row = None
                        try:
                            row = await q.find_user_by_display_name(db.conn, chat_id, uname)  # type: ignore[attr-defined]
                        except AttributeError:
                            row = None
                        if row is not None:
                            uid = int(row.get("user_id") or 0)  # type: ignore
                            if uid:
                                target_payload = {"kind": "user", "user_id": uid, "username": uname}
                except Exception:
                    pass

            ack = "⏳ Смотрю на самых активных участников — дай мне минутку…" if target_payload.get("kind") == "everyone" else "⏳ Вспоминаю переписку…"
            payload = {"chat_id": chat_id, "requester_id": requester_id, "target": target_payload}
            await _queue_run_and_send(app, message, "evaluate", payload, ack, ack_msg=early_ack)
            return

        if action == "qa":
            replied = getattr(message, "reply_to_message", None)
            reply_quote = (
                (getattr(replied, "text", None) or getattr(replied, "caption", None) or "").strip()
                if replied is not None
                else ""
            )
            await _queue_run_and_send(
                app,
                message,
                "qa",
                {
                    "chat_id": chat_id,
                    "question": raw_text,
                    "asker_id": requester_id or None,
                    "quote": reply_quote or None,
                },
                "⏳ Секунду, думаю…",
                ack_msg=early_ack,
            )
            return

        if action == "forget":
            await _drop_early_ack()
            try:
                from pickme.db import queries as q  # type: ignore

                db = getattr(app, "db", None)
                if db is None:
                    await _send_html(message, "⚠️ База данных недоступна.")
                    return
                raw_target = getattr(intent, "target", None)
                tgt = (raw_target or "me").strip().lower()
                if tgt == "chat":
                    await q.delete_chat_data(db.conn, chat_id)
                    await _send_html(message, "🗑 Всё, чат чист — данные удалены.")
                    return
                if tgt in ("me", "меня", "mnie"):
                    if requester_id == 0:
                        await _send_html(message, "Хм, не понял, кто это 🤔")
                        return
                    await q.delete_profile(db.conn, chat_id, requester_id)
                    await q.delete_user_messages(db.conn, chat_id, requester_id)
                    try:
                        await q.delete_memory_job(db.conn, chat_id, requester_id)
                    except Exception:
                        pass
                    await _send_html(message, "🗑 Готово — твои данные из этого чата удалены!")
                    return
                if tgt.startswith("@"):
                    uname = tgt.lstrip("@").strip().split()[0]
                    row = await q.find_user_by_username(db.conn, uname)
                    if row is None:
                        await _send_html(message, "Хм, не понял, кто это 🤔")
                        return
                    tid = int(row.get("user_id") or row.get("id") or 0)  # type: ignore
                    await q.delete_profile(db.conn, chat_id, tid)
                    await q.delete_user_messages(db.conn, chat_id, tid)
                    try:
                        await q.delete_memory_job(db.conn, chat_id, tid)
                    except Exception:
                        pass
                    await _send_html(message, f"🗑 Данные участника {html.escape(uname)} из этого чата удалены.")
                    return
                await _send_html(message, "Хм, не понял, кто это 🤔")
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("forget dispatch failed %s", exc, exc_info=True)
                await _send_html(message, "😔 Что-то поломалось. Попробуй ещё раз чуть позже.")
                return

        if action == "help":
            await _drop_early_ack()
            await _send_html(message, HELP_TEXT)
            return

        replied_ft = getattr(message, "reply_to_message", None)
        quote_ft = (
            (getattr(replied_ft, "text", None) or getattr(replied_ft, "caption", None) or "").strip()
            if replied_ft is not None
            else ""
        )
        await _queue_run_and_send(
            app,
            message,
            "qa",
            {
                "chat_id": chat_id,
                "question": raw_text,
                "asker_id": requester_id or None,
                "quote": quote_ft or None,
            },
            "⏳ Секунду, думаю…",
            ack_msg=early_ack,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("handle_addressed failed %s", exc, exc_info=True)
        try:
            await _send_html(message, "😔 Что-то поломалось. Попробуй ещё раз чуть позже.")
        except Exception:
            pass


__all__ = ["handle_addressed", "_queue_run_and_send", "_send_html"]
