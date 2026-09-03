"""Command handlers for the Telegram bot."""

from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

from aiogram import Dispatcher
from aiogram.filters import Command
from aiogram.types import Message

from pickme.llm.prompts import HELP_TEXT, START_TEXT

logger = logging.getLogger("pickme.telegram")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _split(text: str, limit: int = 4096) -> list[str]:
    """Local split wrapper to avoid circular import; mirrors bot.split_html."""
    try:
        from pickme.telegram.bot import split_html  # type: ignore

        return split_html(text, limit)
    except Exception:
        if len(text) <= limit:
            return [text]
        return [text[i : i + limit] for i in range(0, len(text), limit)]


async def _send_html(message: Message, text: str, reply: bool = False) -> None:
    """Send HTML text chunked at 4096 chars, with fallback sending.

    ``reply=True`` sends the first chunk as a Telegram reply to the
    triggering message (``reply_to_message_id``).
    """
    if not text:
        text = "(пусто)"
    try:
        chunks = _split(text, 4096)
    except Exception:
        chunks = [text]
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
        except Exception as exc:  # noqa: BLE001
            logger.warning("send_html failed chunk %d: %s", idx, exc, exc_info=True)
            try:
                await message.answer(chunk)
            except Exception:
                pass


def _parse_count_arg(text: str | None, default: int, cap: int) -> int:
    if not text:
        return default
    parts = text.strip().split()
    if len(parts) < 2:
        return default
    raw = parts[1].strip().strip(",")
    try:
        n = int(raw)
        if n < 1:
            n = 1
        if n > cap:
            n = cap
        return n
    except (ValueError, TypeError):
        return default


async def _remember_own(app: Any, chat_id: int, text: str) -> None:
    """Store a sent bot reply so future LLM prompts include it (labeled 'bot')."""
    try:
        from pickme.pipelines.ingest import remember_own_message

        db = getattr(app, "db", None)
        if db is not None:
            await remember_own_message(db, chat_id, text)
    except Exception:
        logger.debug("remember own message failed", exc_info=True)


async def _queue_submit_and_send(
    app: Any,
    message: Message,
    kind: str,
    payload: dict[str, Any],
    ack: str,
) -> None:
    """Shared helper: ack, submit to queue, handle errors, send result HTML.

    The ⏳ ack is deleted once a terminal reply is sent; the result is sent
    as a Telegram reply to the triggering message and remembered in DB.
    """
    try:
        await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")  # type: ignore[union-attr]
    except Exception:
        pass
    ack_msg = None
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
        logger.warning("_queue_submit_and_send %s failed %s: %s", kind, name, msg, exc_info=True)
        await _send_html(message, f"⚠️ Что-то пошло не так: {html.escape(msg)}" if msg else "😔 Что-то поломалось. Попробуй ещё раз чуть позже.")
        return

    html_text: str
    try:
        if hasattr(result, "html"):
            html_text = str(getattr(result, "html"))
        elif isinstance(result, str):
            html_text = result
        elif result is None:
            html_text = "Готово."
        else:
            html_text = str(result)
    except Exception:
        html_text = str(result) if result is not None else "Готово."

    try:
        await _send_html(message, html_text, reply=True)
        await _remember_own(app, message.chat.id, html_text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("send result failed %s", exc, exc_info=True)
    finally:
        await _delete_ack()


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_start(message: Message, app: Any) -> None:
    try:
        await _send_html(message, START_TEXT)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cmd_start failed %s", exc, exc_info=True)


async def cmd_help(message: Message, app: Any) -> None:
    try:
        await _send_html(message, HELP_TEXT)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cmd_help failed %s", exc, exc_info=True)


async def cmd_summarize(message: Message, app: Any) -> None:
    try:
        settings = getattr(app, "settings", None)
        default = int(getattr(settings, "summarize_default", 100)) if settings else 100
        cap = int(getattr(settings, "summarize_cap", 500)) if settings else 500
        n = _parse_count_arg(getattr(message, "text", ""), default, cap)
        await _queue_submit_and_send(
            app,
            message,
            "summarize",
            {"chat_id": message.chat.id, "count": n},
            f"⏳ Секунду, листаю последние {n} сообщений…",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("cmd_summarize failed %s", exc, exc_info=True)
        try:
            await _send_html(message, "😔 Что-то поломалось. Попробуй ещё раз чуть позже.")
        except Exception:
            pass


async def cmd_evaluate(message: Message, app: Any) -> None:
    try:
        text = getattr(message, "text", "") or ""
        parts = text.strip().split(maxsplit=1)
        raw_arg = parts[1].strip() if len(parts) > 1 else ""
        if not raw_arg:
            payload_target: dict[str, Any] = {"kind": "me"}
        else:
            lowered = raw_arg.lower()
            if lowered in ("everyone", "всех", "все", "all"):
                payload_target = {"kind": "everyone"}
            elif lowered in ("me", "меня", "mnie"):
                payload_target = {"kind": "me"}
            elif raw_arg.startswith("@"):
                uname = raw_arg.lstrip("@").strip().split()[0]
                payload_target = {"kind": "user", "username": uname}
            else:
                payload_target = {"kind": "user", "username": raw_arg.strip().split()[0]}

        if payload_target.get("kind") == "everyone":
            ack = "⏳ Смотрю на самых активных участников — дай мне минутку…"
        else:
            ack = "⏳ Вспоминаю переписку…"

        requester_id = 0
        try:
            fu = getattr(message, "from_user", None)
            if fu is not None:
                requester_id = int(getattr(fu, "id", 0) or 0)
        except Exception:
            requester_id = 0

        payload: dict[str, Any] = {
            "chat_id": message.chat.id,
            "requester_id": requester_id,
            "target": payload_target,
        }
        await _queue_submit_and_send(app, message, "evaluate", payload, ack)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cmd_evaluate failed %s", exc, exc_info=True)
        try:
            await _send_html(message, "😔 Что-то поломалось. Попробуй ещё раз чуть позже.")
        except Exception:
            pass


async def cmd_ask(message: Message, app: Any) -> None:
    try:
        text = getattr(message, "text", "") or ""
        question = ""
        stripped = text.strip()
        if stripped.lower().startswith("/ask"):
            space = stripped.find(" ")
            if space != -1:
                question = stripped[space + 1 :].strip()
            else:
                question = ""
        else:
            question = stripped

        if not question:
            await _send_html(message, "Напиши вопрос сразу после команды: /ask о чём мы говорили?")
            return

        reply_quote = ""
        replied = getattr(message, "reply_to_message", None)
        if replied is not None:
            reply_quote = (getattr(replied, "text", None) or getattr(replied, "caption", None) or "").strip()
        payload: dict[str, Any] = {
            "chat_id": message.chat.id,
            "question": question,
            "asker_id": getattr(getattr(message, "from_user", None), "id", None),
            "quote": reply_quote or None,
        }
        await _queue_submit_and_send(app, message, "qa", payload, "⏳ Секунду, думаю…")
    except Exception as exc:  # noqa: BLE001
        logger.warning("cmd_ask failed %s", exc, exc_info=True)
        try:
            await _send_html(message, "😔 Что-то поломалось. Попробуй ещё раз чуть позже.")
        except Exception:
            pass


async def cmd_forget(message: Message, app: Any) -> None:
    try:
        from pickme.db import queries as q  # type: ignore

        db = getattr(app, "db", None)
        if db is None or not hasattr(db, "conn"):
            await _send_html(message, "⚠️ База данных недоступна.")
            return

        text = getattr(message, "text", "") or ""
        parts = text.strip().split(maxsplit=1)
        raw_arg = parts[1].strip() if len(parts) > 1 else "me"
        if not raw_arg:
            raw_arg = "me"
        arg_lower = raw_arg.lower()

        from_user = getattr(message, "from_user", None)
        requester_id = int(getattr(from_user, "id", 0) or 0) if from_user is not None else 0
        chat_id = int(message.chat.id)

        if arg_lower == "chat":
            await q.delete_chat_data(db.conn, chat_id)
            await _send_html(message, "🗑 Всё, чат чист — данные удалены.")
            return

        if arg_lower in ("me", "меня", "mnie"):
            target_id = requester_id
            if target_id == 0:
                await _send_html(message, "Хм, не понял, кто это 🤔")
                return
            await q.delete_profile(db.conn, chat_id, target_id)
            await q.delete_user_messages(db.conn, chat_id, target_id)
            try:
                await q.delete_memory_job(db.conn, chat_id, target_id)
            except Exception:
                pass
            await _send_html(message, "🗑 Готово — твои данные из этого чата удалены!")
            return

        if raw_arg.startswith("@"):
            uname = raw_arg.lstrip("@").strip().split()[0]
            row = await q.find_user_by_username(db.conn, uname)
            if row is None:
                await _send_html(message, "Хм, не понял, кто это 🤔")
                return
            target_id = int(row.get("user_id") or row.get("id") or 0)  # type: ignore[arg-type]
            if target_id == 0:
                target_id = int(row.get("user_id", 0) or 0)  # type: ignore
            await q.delete_profile(db.conn, chat_id, target_id)
            await q.delete_user_messages(db.conn, chat_id, target_id)
            try:
                await q.delete_memory_job(db.conn, chat_id, target_id)
            except Exception:
                pass
            safe = html.escape(uname)
            await _send_html(message, f"🗑 Данные участника {safe} из этого чата удалены.")
            return

        try:
            row = await q.find_user_by_display_name(db.conn, chat_id, raw_arg.strip())  # type: ignore[attr-defined]
        except AttributeError:
            row = None
        if row is not None:
            target_id = int(row.get("user_id") or 0)  # type: ignore
            await q.delete_profile(db.conn, chat_id, target_id)
            await q.delete_user_messages(db.conn, chat_id, target_id)
            try:
                await q.delete_memory_job(db.conn, chat_id, target_id)
            except Exception:
                pass
            safe = html.escape(raw_arg.strip())
            await _send_html(message, f"🗑 Данные участника {safe} из этого чата удалены.")
            return

        await _send_html(message, "Хм, не понял, кто это 🤔")
    except Exception as exc:  # noqa: BLE001
        logger.warning("cmd_forget failed %s", exc, exc_info=True)
        try:
            await _send_html(message, "😔 Что-то поломалось. Попробуй ещё раз чуть позже.")
        except Exception:
            pass


async def cmd_status(message: Message, app: Any) -> None:
    try:
        from pickme.telegram.bot import split_html as _split_html  # local to avoid circular top

        lines: list[str] = []
        lines.append("<b>🩺 Диагностика</b>")

        try:
            registry = getattr(app, "registry", None)
            if registry is not None:
                statuses = registry.status()  # type: ignore[attr-defined]
                lines.append("\n<b>Провайдеры</b>")
                if not statuses:
                    lines.append("  нет настроенных провайдеров")
                for s in statuses:
                    name = getattr(s, "name", "?")
                    model = getattr(s, "model", "?")
                    healthy = getattr(s, "healthy", False)
                    breaker = getattr(s, "breaker_open", False)
                    last = getattr(s, "last_error", None) or "-"
                    if healthy and not breaker:
                        status_txt = "здоров"
                        icon = "✅"
                    elif breaker:
                        status_txt = "недоступен (breaker)"
                        icon = "⛔"
                    else:
                        status_txt = "ошибка"
                        icon = "⚠️"
                    lines.append(f"  {icon} <b>{html.escape(str(name))}</b> <code>{html.escape(str(model))}</code> — {status_txt} last={html.escape(str(last))}")
            else:
                lines.append("\n<b>Провайдеры</b> — недоступны")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"\n<b>Провайдеры</b> ошибка: {html.escape(str(exc))}")

        try:
            queue = getattr(app, "queue", None)
            if queue is not None:
                qm = queue.metrics()  # type: ignore[attr-defined]
                lines.append("\n<b>Очередь</b>")
                lines.append(f"  в очереди={qm.get('queued',0)} активных={qm.get('active',0)} выполнено={qm.get('done',0)} ошибок={qm.get('failed',0)} глубина={qm.get('depth',0)}")
            else:
                lines.append("\n<b>Очередь</b> — недоступна")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"\n<b>Очередь</b> ошибка: {html.escape(str(exc))}")

        try:
            llm = getattr(app, "llm", None)
            if llm is not None:
                lm = llm.metrics()  # type: ignore[attr-defined]
                lines.append("\n<b>ИИ</b>")
                lines.append(
                    f"  запросов={lm.get('requests',0)} 429={lm.get('rate_limited_429',0)} переключений={lm.get('tier_failovers',0)} задержка={lm.get('latency_ms_last',0)}мс"
                )
                if "tokens_in" in lm or "tokens_out" in lm:
                    lines.append(f"  токены вход={lm.get('tokens_in',0)} выход={lm.get('tokens_out',0)}")
            else:
                lines.append("\n<b>ИИ</b> — недоступен")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"\n<b>ИИ</b> ошибка: {html.escape(str(exc))}")

        html_text = "\n".join(lines)
        try:
            chunks = _split_html(html_text, 4096)
        except Exception:
            chunks = [html_text]
        for chunk in chunks:
            try:
                await message.answer(chunk, parse_mode="HTML")
            except Exception:
                await message.answer(chunk)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cmd_status failed %s", exc, exc_info=True)
        try:
            await message.answer("😔 Что-то поломалось. Попробуй ещё раз чуть позже.", parse_mode="HTML")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_handlers(dp: Dispatcher) -> None:
    dp.message.register(cmd_start, Command("start"))
    dp.message.register(cmd_help, Command("help"))
    dp.message.register(cmd_summarize, Command("summarize"))
    dp.message.register(cmd_evaluate, Command("evaluate"))
    dp.message.register(cmd_ask, Command("ask"))
    dp.message.register(cmd_forget, Command("forget"))
    dp.message.register(cmd_status, Command("status"))
    logger.info("command handlers registered")
