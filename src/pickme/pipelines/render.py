"""Shared rendering helpers — privacy aliasing, transcript, HTML sanitization."""

from __future__ import annotations

import html
import re
from collections.abc import Iterable
from datetime import datetime, timezone


def build_aliases(rows: Iterable[dict]) -> dict[int, str]:
    """Map user_id -> stable alias numbered by first appearance order.

    Only non-None integer user_ids are considered; numbering is deterministic
    based on input iteration order.
    """
    aliases: dict[int, str] = {}
    next_idx = 1
    for row in rows:
        uid = row.get("user_id")
        if uid is None:
            continue
        # Ensure int key
        try:
            uid_int = int(uid)  # type: ignore[arg-type]
        except Exception:
            continue
        if uid_int not in aliases:
            aliases[uid_int] = f"user_{next_idx}"
            next_idx += 1
    return aliases


def render_transcript(
    rows: Iterable[dict],
    aliases: dict[int, str],
    with_time: bool = True,
) -> str:
    """Render messages to aliased transcript lines.

    Each line: ``[HH:MM] alias: text`` when ``with_time``; else ``alias: text``.
    Media-only messages render as ``[media_type]``. Edited messages get
    ``" (edited)"`` suffix. ``user_id`` None maps to ``"system"``; the bot's
    own stored replies (``user_id`` None + ``is_bot``) map to ``"bot"``.
    """
    lines: list[str] = []
    for row in rows:
        created_at = row.get("created_at")
        alias: str
        uid = row.get("user_id")
        is_bot_row = False
        try:
            is_bot_row = bool(int(row.get("is_bot") or 0))  # type: ignore[arg-type]
        except Exception:
            is_bot_row = bool(row.get("is_bot"))
        if is_bot_row and uid is None:
            # Our own stored replies (user_id NULL + is_bot) — labeled "bot"
            # so the LLM knows these are ITS OWN earlier messages.
            alias = "bot"
        elif uid is None:
            alias = "system"
        else:
            try:
                alias = aliases.get(int(uid), f"user_{uid}")  # type: ignore[arg-type]
            except Exception:
                alias = "system"

        # Time prefix
        prefix = ""
        if with_time:
            try:
                ms = int(created_at) if created_at is not None else 0  # type: ignore[arg-type]
                dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
                prefix = f"[{dt.strftime('%H:%M')}] "
            except Exception:
                prefix = "[??:??] "

        # Body
        text = row.get("text")
        media_type = row.get("media_type")
        # text takes precedence even if media present; media-only when text empty/None
        if text is not None:
            # Ensure string
            body = str(text)
            # Treat empty string as missing -> fall through to media placeholder if exists
            if body == "" and media_type:
                body = f"[{media_type}]"
        else:
            if media_type:
                body = f"[{media_type}]"
            else:
                body = ""

        # Edited suffix
        is_edited = row.get("is_edited")
        if is_edited:
            try:
                if int(is_edited):  # type: ignore[arg-type]
                    body = f"{body} (edited)" if body else "(edited)"
            except Exception:
                if bool(is_edited):
                    body = f"{body} (edited)" if body else "(edited)"

        lines.append(f"{prefix}{alias}: {body}")
    return "\n".join(lines)
    return "\n".join(lines)


def dealias(text: str, aliases: dict[int, str], names: dict[int, str]) -> str:
    """Replace alias tokens in *text* with display names.

    Missing name keeps alias unchanged. Replacement is word-boundary aware
    so ``user_1`` inside ``user_10`` is not mangled.
    """
    if not text or not aliases or not names:
        # Still need to handle case where aliases empty -> nothing to replace
        if not aliases or not names:
            return text
    # Build alias string -> display name map
    alias_to_name: dict[str, str] = {}
    for uid, alias in aliases.items():
        name = names.get(uid)
        if name:
            alias_to_name[alias] = name
    if not alias_to_name:
        return text

    # Sort aliases by length descending to prefer longer (user_10 before user_1) though \b handles it
    # but ensures correct ordering for alternation.
    sorted_aliases = sorted(alias_to_name.keys(), key=len, reverse=True)
    pattern = re.compile(r"\b(" + "|".join(re.escape(a) for a in sorted_aliases) + r")\b")

    def _repl(m: re.Match[str]) -> str:
        alias = m.group(1)
        return alias_to_name.get(alias, alias)

    return pattern.sub(_repl, text)


def estimate_tokens(text: str) -> int:
    """Estimate token count via ``len(text)//4`` heuristic."""
    if not text:
        return 0
    return len(text) // 4


def _markdown_to_html(text: str) -> str:
    """Convert common Markdown to Telegram-HTML.

    Order: code → bold → italic → headings → bullets → hr.
    Idempotent for already-HTML text.
    """
    if not text:
        return text
    # 1. Inline code: `code`
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    # 2. Bold: **bold** and __bold__
    text = re.sub(r"\*\*(?!\s)([^*\n]+?)(?<!\s)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__([^\s_][^_\n]*?)__", r"<b>\1</b>", text)
    # 3. Italic: *italic* and _italic_ (word-boundary guarded for _)
    text = re.sub(r"\*(?!\s)([^*\n]+?)(?<!\s)\*", r"<i>\1</i>", text)
    text = re.sub(r"(?<![\w])_([^_\n]+?)_(?![\w])", r"<i>\1</i>", text)
    # 4. Headings: #..###### Title → <b>Title</b>
    text = re.sub(r"(?m)^#{1,6}[ \t]+(.+?)\s*$", r"<b>\1</b>", text)
    # 5. Bullets: leading [-*+] → •
    text = re.sub(r"(?m)^([ \t]*)[-*+][ \t]+", r"\1• ", text)
    # 6. Horizontal rules → empty
    text = re.sub(r"(?m)^[ \t]*([-*_][ \t]*){3,}$", "", text)
    return text


def sanitize_html(text: str) -> str:
    """Sanitize to Telegram-HTML: keep only ``<b>``, ``<i>``, ``<code>``.

    Escapes ``&`` and stray ``<`` / ``>``. Converts Markdown first.
    """
    if not text:
        return ""

    # Convert Markdown → HTML first so converted tags survive allowlist
    text = _markdown_to_html(text)

    # Placeholders for allowed tags to protect them from escaping
    tag_map = {
        "<b>": "\x00B_OPEN\x00",
        "</b>": "\x00B_CLOSE\x00",
        "<i>": "\x00I_OPEN\x00",
        "</i>": "\x00I_CLOSE\x00",
        "<code>": "\x00C_OPEN\x00",
        "</code>": "\x00C_CLOSE\x00",
    }
    # Protect allowed tags (case-sensitive exactly as spec)
    protected = text
    for tag, ph in tag_map.items():
        protected = protected.replace(tag, ph)

    # Now escape & and < >
    # First & to avoid double-escaping later entities
    protected = protected.replace("&", "&amp;")
    protected = protected.replace("<", "&lt;")
    protected = protected.replace(">", "&gt;")

    # Restore allowed tags
    for tag, ph in tag_map.items():
        protected = protected.replace(ph, tag)

    return protected
