"""Telegram filters — addressed-to-bot detection."""

from __future__ import annotations

import logging
from typing import Any

from aiogram.filters import BaseFilter
from aiogram.types import Message

logger = logging.getLogger("pickme.telegram")


class AddressedToBot(BaseFilter):
    """True when message is addressed to the bot (mention prefix or reply).

    Returns ``{"addressed_text": str}`` on match (mention-stripped), else ``False``.
    False for: no text, text starting with "/", service messages (handled in middleware).
    Bot identity is read from ``app`` (``app.bot_id``, ``app.bot_username``) injected
    via ``dp["app"]`` workflow data.
    """

    async def __call__(self, message: Message, **kwargs: Any) -> bool | dict[str, Any]:
        # Retrieve app from workflow data (dp["app"] injection).
        app = kwargs.get("app")
        # Fallback: try to get from message.bot context if app not injected directly.
        # We need bot_id / username; if not available, we cannot determine addressing.
        if app is None:
            # Try to look up via event data: 'app' may be in other kwargs?
            logger.debug("AddressedToBot missing app in kwargs keys=%s", list(kwargs.keys()))
            return False

        text: str | None = getattr(message, "text", None)
        # Caption may be used? Spec says message.text; we only handle text.
        if not text or not text.strip():
            return False

        stripped = text.strip()
        # Reject command-prefixed text (commands are handled earlier; observer is fallback)
        if stripped.startswith("/"):
            return False

        bot_id: int = int(getattr(app, "bot_id", 0) or 0)
        bot_username: str = str(getattr(app, "bot_username", "") or "").lstrip("@")
        if not bot_id and not bot_username:
            logger.debug("AddressedToBot missing bot identity")
            return False

        # 1) Reply to bot's own message
        reply = getattr(message, "reply_to_message", None)
        if reply is not None:
            try:
                from_user = getattr(reply, "from_user", None)
                if from_user is not None and int(getattr(from_user, "id", 0) or 0) == bot_id and bot_id != 0:
                    logger.debug("addressed via reply_to_bot")
                    return {"addressed_text": stripped}
            except Exception:
                pass

        # 2) Mention entity equal to bot username at start
        # Check text startswith @bot_username and entity offset 0
        entities = getattr(message, "entities", None) or []
        # Find mention at offset 0
        mention_stripped: str | None = None
        for ent in entities:
            try:
                etype = getattr(ent, "type", "")
                offset = int(getattr(ent, "offset", -1) or -1)
                length = int(getattr(ent, "length", 0) or 0)
                if etype == "mention" and offset == 0 and length > 1:
                    # Slice text (original, not stripped, to respect offset)
                    # Use message.text (original) slicing
                    raw_text = message.text or ""
                    mention = raw_text[offset : offset + length]
                    mention_name = mention.lstrip("@")
                    if mention_name.lower() == bot_username.lower():
                        # Strip the mention from stripped text (which already stripped leading space)
                        # The stripped text starts with "@bot ..."
                        # Compute addressed_text by removing mention part
                        # Note: stripped still has mention at start
                        after = stripped[length:].lstrip()
                        # If after is empty, we treat addressed_text as empty but still addressed?
                        # Return stripped (empty) -> caller may treat as no-op.
                        mention_stripped = after
                        break
            except Exception:
                continue

        if mention_stripped is not None:
            logger.debug("addressed via mention, text=%r", mention_stripped[:80])
            return {"addressed_text": mention_stripped}

        # Fallback: raw startswith @bot_username without entity (some clients)
        if bot_username:
            lower_stripped = stripped.lower()
            lower_bot = f"@{bot_username.lower()}"
            if lower_stripped.startswith(lower_bot):
                after = stripped[len(lower_bot):].lstrip()
                # Ensure either end or whitespace following? We already checked prefix.
                logger.debug("addressed via raw @ prefix")
                return {"addressed_text": after}

        return False
