"""Keyword fast-path intent matcher (0 LLM calls).

Binding implementation of bot-plan §4.6 stage 1.
"""

from __future__ import annotations

import re
import logging

from pickme.schemas.intent import Intent

logger = logging.getLogger("pickme.routing")

# Patterns per spec — high-confidence regex, any language.
SUMMARIZE_RE = re.compile(r"\b(summar\w*|tldr|tl;?dr|резюме|перекаж\w*|opowi\w*|zusammenfass\w*)\b", re.IGNORECASE)
EVALUATE_RE = re.compile(r"\b(evaluat\w*|assess\w*|оцен\w*|rank\w*|рейтинг\w*)\b", re.IGNORECASE)
COUNT_RE = re.compile(r"(?:last|последние?|letzte|ostatnie)\s+(\d{1,4})", re.IGNORECASE)

# Target helpers
MENTION_RE = re.compile(r"@(\w{1,64})")
EVERYONE_RE = re.compile(r"\b(everyone|всех|all)\b", re.IGNORECASE)
ME_RE = re.compile(r"\b(me|меня|mnie)\b", re.IGNORECASE)

# Also handle Cyrillic everyone variants: "все" etc. include broader
EVERYONE_ALT_RE = re.compile(r"\b(все|всем|всех)\b", re.IGNORECASE)


def match(text: str) -> Intent | None:
    """High-confidence keyword fast-path.

    Returns ``Intent`` on clear match, else ``None`` to fall through to LLM router.
    - Summarize requires SUMMARIZE_RE; count extracted via COUNT_RE.
    - Evaluate requires EVALUATE_RE; target required additionally extracted (@mention, everyone, me).
    """
    if not text or not text.strip():
        return None
    s = text.strip()

    # Summarize: keyword present -> summarize intent (count optional)
    if SUMMARIZE_RE.search(s):
        m = COUNT_RE.search(s)
        count: int | None = None
        if m:
            try:
                count = int(m.group(1))
            except (ValueError, IndexError):
                count = None
        logger.debug("fastpath summarize match count=%s text=%r", count, text[:80])
        return Intent(action="summarize", count=count, target=None)

    # Evaluate: requires evaluate keyword; target alone insufficient
    if EVALUATE_RE.search(s):
        # Try @mention first
        m = MENTION_RE.search(s)
        if m:
            target = f"@{m.group(1)}"
            logger.debug("fastpath evaluate @mention %s", target)
            return Intent(action="evaluate", count=None, target=target)
        # Everyone (also через всем/все)
        if EVERYONE_RE.search(s) or EVERYONE_ALT_RE.search(s):
            logger.debug("fastpath evaluate everyone")
            return Intent(action="evaluate", count=None, target="everyone")
        if ME_RE.search(s):
            logger.debug("fastpath evaluate me")
            return Intent(action="evaluate", count=None, target="me")
        # Check isolated "all" / "everyone" in other languages? already covered
        # If keyword present but no explicit target, still return evaluate with target None
        # — caller may interpret as "me" fallback. Spec: evaluate -> Intent(action="evaluate", target=str|None)
        logger.debug("fastpath evaluate no target")
        return Intent(action="evaluate", count=None, target=None)

    return None
