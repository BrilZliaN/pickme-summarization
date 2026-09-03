"""LLM-backed intent router (stage 2)."""

from __future__ import annotations

import logging

from pickme.llm.prompts import ROUTER_SYSTEM
from pickme.schemas.intent import Intent

logger = logging.getLogger("pickme.routing")


async def route(llm, settings, text: str, member_hint: list[str] | None = None) -> Intent:
    """Classify utterance via LLM router.

    Builds a tiny prompt with ROUTER_SYSTEM, calls ``llm.chat`` with
    ``feature="route"`` and ``json_mode=True``, parses via ``extract_json``
    and ``Intent.model_validate``. Any failure degrades to ``qa``.
    """
    # Lazily import extract_json to avoid hard dependency at module import time.
    try:
        from pickme.llm.client import extract_json  # type: ignore
    except Exception:  # pragma: no cover
        def extract_json(_t: str):  # type: ignore
            return None

    # Build member hint line (aliased only)
    hint_part = ""
    if member_hint:
        # Join aliases, e.g. "user_1, user_2"
        hint_part = "\nKNOWN MEMBERS (aliased): " + ", ".join(member_hint)
    user_content = f"TEXT: {text}{hint_part}"

    messages = [
        {"role": "system", "content": ROUTER_SYSTEM},
        {"role": "user", "content": user_content},
    ]

    try:
        result = await llm.chat(messages, feature="route", json_mode=True)
        raw = result.text or ""
        logger.debug("router llm raw=%r", raw[:400])
        parsed = extract_json(raw)
        if parsed is None:
            logger.warning("router extract_json failed, defaulting to qa raw=%r", raw[:200])
            return Intent(action="qa", count=None, target=None)
        # Intent expects strict JSON; handle both dict validation and extra fields.
        if isinstance(parsed, dict):
            try:
                intent = Intent.model_validate(parsed)
                return intent
            except Exception as exc:  # noqa: BLE001
                logger.warning("router Intent validation failed %s parsed=%r", exc, parsed)
                return Intent(action="qa", count=None, target=None)
        elif isinstance(parsed, list):
            logger.warning("router returned list, default to qa")
            return Intent(action="qa", count=None, target=None)
        else:
            return Intent(action="qa", count=None, target=None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("router llm.chat failed %s, defaulting to qa", exc, exc_info=True)
        return Intent(action="qa", count=None, target=None)
