"""Ingest middleware — stores every group message and triggers memory merges."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Awaitable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update, Message

logger = logging.getLogger("pickme.telegram")


class IngestMiddleware(BaseMiddleware):
    """Outer middleware on ``Update`` — extracts and ingests messages.

    Plan §4.1: runs on every update, fire-and-forget SQLite write, memory trigger
    check, never blocks handler chain.
    """

    async def __call__(  # type: ignore[override]
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        app = data.get("app")
        # Only process Update events that carry a Message or edited_message.
        # Other updates (callback, inline, etc.) pass through immediately.
        if not isinstance(event, Update):
            return await handler(event, data)

        # Extract Message (or edited_message). Support both.
        message: Message | None = getattr(event, "message", None)
        is_edited = False
        if message is None:
            # Try edited_message
            edited = getattr(event, "edited_message", None)
            if edited is not None:
                message = edited  # type: ignore[assignment]
                is_edited = True
            else:
                return await handler(event, data)

        if not isinstance(message, Message):
            return await handler(event, data)

        # Skip service messages (new_chat_members, left_chat_member, new_chat_title, pinned_message, etc.)
        try:
            if getattr(message, "new_chat_members", None):
                return await handler(event, data)
            if getattr(message, "left_chat_member", None) is not None:
                return await handler(event, data)
            if getattr(message, "new_chat_title", None):
                return await handler(event, data)
            if getattr(message, "new_chat_photo", None):
                return await handler(event, data)
            if getattr(message, "delete_chat_photo", None):
                # This is bool
                if getattr(message, "delete_chat_photo"):
                    return await handler(event, data)
            if getattr(message, "pinned_message", None) is not None:
                return await handler(event, data)
            if getattr(message, "group_chat_created", None):
                return await handler(event, data)
            if getattr(message, "supergroup_chat_created", None):
                return await handler(event, data)
            if getattr(message, "channel_chat_created", None):
                return await handler(event, data)
            if getattr(message, "message_auto_delete_timer_changed", None) is not None:
                return await handler(event, data)
            if getattr(message, "migrate_to_chat_id", None) is not None:
                return await handler(event, data)
            if getattr(message, "migrate_from_chat_id", None) is not None:
                return await handler(event, data)
        except Exception:
            pass

        if app is None:
            # No app — still pass through, but log.
            logger.warning("IngestMiddleware missing app, skipping ingest")
            return await handler(event, data)

        # Skip messages from the bot itself.
        try:
            bot_id = int(getattr(app, "bot_id", 0) or 0)
            from_user = getattr(message, "from_user", None)
            if from_user is not None and bot_id != 0 and int(getattr(from_user, "id", 0) or 0) == bot_id:
                return await handler(event, data)
        except Exception:
            pass

        # Extract fields.
        try:
            chat = getattr(message, "chat", None)
            if chat is None:
                return await handler(event, data)
            chat_id = int(getattr(chat, "id", 0) or 0)
            chat_title = getattr(chat, "title", None)
            # In private chats title may be None; keep as None.

            from_user = getattr(message, "from_user", None)
            if from_user is not None:
                try:
                    user_id = int(getattr(from_user, "id", 0) or 0)
                    # aiogram types: username may be None.
                    username = getattr(from_user, "username", None)
                    first_name = getattr(from_user, "first_name", "") or ""
                    last_name = getattr(from_user, "last_name", "") or ""
                    display_name = (first_name + (" " + last_name if last_name else "")).strip() or None
                    # If display_name ends up empty, keep None.
                except Exception:
                    user_id = 0
                    username = None
                    display_name = None
                # If from_user id is 0 -> treat as anon/system.
                if user_id == 0:
                    user_id = None  # type: ignore[assignment]
            else:
                user_id = None  # type: ignore[assignment]
                username = None
                display_name = None

            # Text or caption
            text = getattr(message, "text", None)
            if text is None:
                text = getattr(message, "caption", None)
            # Keep text as None if empty? Store as None for media-only messages.
            if text is not None and not str(text).strip():
                # Keep empty? But ingest_message expects str|None; we preserve None for truly empty.
                # Leave as text.
                pass

            # reply_to_message
            _reply = getattr(message, "reply_to_message", None)
            reply_to_message_id: int | None = None
            if _reply is not None:
                try:
                    reply_to_message_id = int(getattr(_reply, "message_id", 0) or 0) or None
                except Exception:
                    reply_to_message_id = None

            # Media detection
            media_type: str | None = None
            media_meta: str | None = None
            try:
                meta_dict: dict[str, Any] = {}
                if getattr(message, "photo", None):
                    # photo is list of PhotoSize, pick last (largest)
                    photos = getattr(message, "photo")
                    if photos:
                        largest = photos[-1]  # type: ignore[index]
                        media_type = "photo"
                        meta_dict["file_id"] = getattr(largest, "file_id", "")
                        meta_dict["width"] = getattr(largest, "width", None)
                        meta_dict["height"] = getattr(largest, "height", None)
                elif getattr(message, "video", None):
                    vid = getattr(message, "video")
                    media_type = "video"
                    meta_dict["file_id"] = getattr(vid, "file_id", "")
                    meta_dict["duration"] = getattr(vid, "duration", None)
                elif getattr(message, "document", None):
                    doc = getattr(message, "document")
                    media_type = "document"
                    meta_dict["file_id"] = getattr(doc, "file_id", "")
                    meta_dict["file_name"] = getattr(doc, "file_name", None)
                elif getattr(message, "sticker", None):
                    stk = getattr(message, "sticker")
                    media_type = "sticker"
                    meta_dict["file_id"] = getattr(stk, "file_id", "")
                    meta_dict["emoji"] = getattr(stk, "emoji", None)
                elif getattr(message, "voice", None):
                    v = getattr(message, "voice")
                    media_type = "voice"
                    meta_dict["file_id"] = getattr(v, "file_id", "")
                    meta_dict["duration"] = getattr(v, "duration", None)
                elif getattr(message, "video_note", None):
                    vn = getattr(message, "video_note")
                    media_type = "video_note"
                    meta_dict["file_id"] = getattr(vn, "file_id", "")
                    meta_dict["duration"] = getattr(vn, "duration", None)
                elif getattr(message, "audio", None):
                    a = getattr(message, "audio")
                    media_type = "document"  # fallback mapping; spec says photo/video/document/sticker/voice/video_note
                    meta_dict["file_id"] = getattr(a, "file_id", "")
                if media_type:
                    # Ensure file_id present; else drop meta.
                    if not meta_dict.get("file_id"):
                        meta_dict["file_id"] = ""
                    media_meta = json.dumps(meta_dict, ensure_ascii=False)
            except Exception:
                logger.debug("media detection failed", exc_info=True)
                media_type = None
                media_meta = None

            # Date -> epoch ms
            created_at: int
            try:
                dt = getattr(message, "date", None)
                if dt is not None and hasattr(dt, "timestamp"):
                    created_at = int(dt.timestamp() * 1000)
                else:
                    created_at = int(time.time() * 1000)
            except Exception:
                created_at = int(time.time() * 1000)

            # If edited_message update, is_edited already True; else check message.edit_date
            try:
                if not is_edited and getattr(message, "edit_date", None) is not None:
                    is_edited = True
            except Exception:
                pass

            is_bot = False
            try:
                from_user_obj = getattr(message, "from_user", None)
                if from_user_obj is not None:
                    is_bot = bool(getattr(from_user_obj, "is_bot", False))
            except Exception:
                pass

            # Call ingest_message (import lazily, since pipelines lane may be missing at import time).
            try:
                from pickme.pipelines import ingest_message as _ingest  # type: ignore
            except Exception as e:
                # Pipelines not yet available — log and continue.
                logger.debug("ingest_message import failed %s", e)
                return await handler(event, data)

            # app.db: Database with .conn
            db = getattr(app, "db", None)
            settings = getattr(app, "settings", None)
            if db is None or settings is None:
                logger.warning("ingest missing db/settings")
                return await handler(event, data)

            # ingest_message stores + upserts; returns (message_id, memory_due)
            try:
                _res = await _ingest(
                    db,
                    settings,
                    chat_id=chat_id,
                    chat_title=chat_title,
                    user_id=user_id,
                    username=username,
                    display_name=display_name,
                    text=text,
                    reply_to_message_id=reply_to_message_id,
                    media_type=media_type,
                    media_meta=media_meta,
                    created_at=created_at,
                    is_bot=is_bot,
                    is_edited=is_edited,
                )
                # _res is tuple (message_id, memory_due)
                memory_due = False
                try:
                    if isinstance(_res, tuple) and len(_res) == 2:
                        memory_due = bool(_res[1])
                    elif isinstance(_res, dict):
                        memory_due = bool(_res.get("memory_due"))
                except Exception:
                    memory_due = False

                if memory_due and user_id is not None:
                    # Coalescing-safe background job
                    try:
                        queue = getattr(app, "queue", None)
                        if queue is not None:
                            queue.submit_background("memory_merge", chat_id, {"chat_id": chat_id, "user_id": int(user_id)})  # type: ignore[arg-type]
                            logger.debug("memory_merge enqueued chat=%s user=%s", chat_id, user_id)
                    except Exception:
                        logger.debug("memory_merge enqueue failed", exc_info=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("ingest_message failed: %s", exc, exc_info=True)

        except Exception as exc:  # noqa: BLE001
            logger.warning("IngestMiddleware outer failure %s", exc, exc_info=True)

        # Always pass through.
        return await handler(event, data)
