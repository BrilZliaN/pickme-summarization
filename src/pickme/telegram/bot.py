"""Bot builder, HTML splitter and global wiring."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import Bot, Dispatcher
from aiogram.filters import Command

logger = logging.getLogger("pickme.telegram")

if TYPE_CHECKING:
    from pickme.main import App


def split_html(text: str, limit: int = 4096) -> list[str]:
    """Split HTML text on ``\\n`` boundaries accumulating ``<= limit``.

    Hard-split fallback mid-line when a single line exceeds ``limit``.
    Never returns empty list for non-empty input. Joining chunks with
    ``"".join`` restores the original text losslessly.
    """
    if text == "":
        return [""]
    if text is None:  # type: ignore[unreachable]
        return [""]
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + limit, n)
        if end == n:
            chunks.append(text[start:end])
            break
        # Try to split on last newline within window to keep chunks <= limit
        window = text[start:end]
        last_nl = window.rfind("\n")
        if last_nl != -1:
            cut = start + last_nl + 1  # include the newline
            # Avoid zero-progress (newline at start)
            if cut == start:
                cut = end
            # Ensure cut does not exceed limit (it is <= end)
            chunks.append(text[start:cut])
            start = cut
        else:
            # No newline — hard-split mid-line
            chunks.append(text[start:end])
            start = end

    # Guarantee non-empty for non-empty input
    if not chunks and text:
        return [text[:limit]]
    # Validate each chunk <= limit (hard-split already ensures, but newline-inclusive cut is <= limit)
    # If any still exceeds (e.g. limit edge), hard-split them
    final: list[str] = []
    for ch in chunks:
        if len(ch) <= limit:
            final.append(ch)
        else:
            for i in range(0, len(ch), limit):
                final.append(ch[i : i + limit])
    if not final and text:
        return [text[:limit]]
    return final


def build_bot(app: "App") -> tuple[Bot, Dispatcher]:
    """Create Bot and Dispatcher, register middleware, handlers and error handler.

    Args:
        app: Application container with settings, db, llm, queue, etc. Stored as
            ``dp["app"]`` for injection into handlers/filters/middleware.

    Returns:
        Tuple ``(bot, dispatcher)``.
    """
    from pickme.telegram.middlewares import IngestMiddleware
    from pickme.telegram.handlers import register_handlers
    from pickme.telegram.filters import AddressedToBot

    bot = Bot(token=app.settings.telegram_bot_token)  # type: ignore[arg-type]
    dp = Dispatcher()
    dp["app"] = app  # type: ignore[index]

    # Outer ingest middleware on UPDATE (must be outer to see every update).
    dp.update.outer_middleware(IngestMiddleware())

    # Register command handlers BEFORE addressed observer.
    register_handlers(dp)

    # Addressed observer — fallback for non-command addressed text.
    # Import dispatch lazily to avoid circular import at top-level.
    from pickme.routing.dispatch import handle_addressed  # local import

    async def _addressed_wrapper(message, app, addressed_text: str):  # type: ignore[no-untyped-def]
        # Delegate to dispatch module.
        await handle_addressed(app, message, addressed_text)

    # The AddressedToBot filter must reject "/"-prefixed text; ordering ensures
    # command handlers win for slash commands.
    dp.message.register(_addressed_wrapper, AddressedToBot())

    # Global error handler — never re-raise, log and reply friendly.
    async def _global_error_handler(event, exception):  # type: ignore[no-untyped-def]
        logger.exception("unhandled dispatcher error: %s", exception, exc_info=exception)
        try:
            # Try to extract a Message to reply to.
            update = getattr(event, "update", None)
            # event may be Update or ErrorEvent with .update
            msg = None
            if update is not None:
                # ErrorEvent.update may be Update
                msg = getattr(update, "message", None)
                if msg is None:
                    msg = getattr(update, "edited_message", None)
            else:
                # event itself may be Update
                msg = getattr(event, "message", None)

            if msg is not None:
                try:
                    await msg.answer("😔 Что-то поломалось. Попробуй ещё раз чуть позже.", parse_mode="HTML")
                except Exception:
                    logger.debug("failed to send error reply", exc_info=True)
        except Exception:
            logger.debug("error handler secondary failure", exc_info=True)
        # Never re-raise

    # Aiogram 3: errors handler registration via dp.errors.register
    # Some versions use dp.error.register — support both.
    try:
        dp.errors.register(_global_error_handler)  # type: ignore[attr-defined]
    except AttributeError:
        try:
            dp.error.register(_global_error_handler)  # type: ignore[attr-defined]
        except Exception:
            logger.warning("failed to register global error handler")

    logger.info("bot and dispatcher built")
    return bot, dp
