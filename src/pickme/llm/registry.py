"""Provider registry with tiered failover, discovery and circuit breakers.

Binding implementation of bot-plan §5.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field

logger = logging.getLogger("pickme.llm")

# Stable per-process session id sent as `x-opencode-session` on opencode-tier
# requests (required by the OpenCode service as of 2026-09-06; one bot
# process lifetime == one session).
_SESSION_ID: str = uuid.uuid4().hex

try:
    from pickme.config import Settings as _Settings  # type: ignore
except Exception:  # pragma: no cover
    _Settings = object  # type: ignore

from typing import Any as _Any

Settings = _Any  # type: ignore

try:
    from pickme.llm.client import TokenBucket as _TokenBucket  # type: ignore
except Exception:  # pragma: no cover
    _TokenBucket = object  # type: ignore

TokenBucket = _Any  # type: ignore


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Tier:
    """Single provider tier (OpenAI-compatible)."""

    name: str  # "hetzner" | "zen" | "go"
    base_url: str
    api_key: str
    model: str
    client: object  # openai.AsyncOpenAI
    fallback_model: str | None = None  # only hetzner carries a fallback


@dataclass
class TierStatus:
    """Status snapshot for /status."""

    name: str
    model: str
    healthy: bool
    breaker_open: bool
    last_error: str | None


@dataclass
class _BreakerState:
    """Per-tier circuit-breaker state."""

    consecutive_5xx: int = 0
    open_until: float | None = None  # monotonic timestamp; None = closed
    # model_missing needs open until next successful health poll (no fixed cooldown)
    open_model_missing: bool = False
    last_error: str | None = None
    healthy: bool = True  # derived from breaker

    def is_open(self, now: float) -> bool:
        if self.open_model_missing:
            return True
        if self.open_until is None:
            return False
        if now >= self.open_until:
            # Cooldown elapsed -> half-open eligible (caller treats as closed for one probe)
            return False
        return True


# ---------------------------------------------------------------------------
# ProviderRegistry
# ---------------------------------------------------------------------------

class ProviderRegistry:
    """Manages tiered providers: discovery, health polling, circuit breakers."""

    def __init__(self, settings: Settings, bucket: TokenBucket) -> None:
        self._settings = settings
        self._bucket: TokenBucket = bucket  # type: ignore[assignment]
        self._tiers: list[Tier] = []
        self._breakers: dict[str, _BreakerState] = {}
        self._health_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._lock = asyncio.Lock()  # guards tier list / breaker updates
        self._build_tiers()

    # -- tier construction -------------------------------------------------

    def _build_tiers(self) -> None:
        """Build cost-ordered tiers, skipping any with empty api_key."""
        s = self._settings
        # Lazy import openai here to avoid import error during py_compile if not installed?
        # py_compile doesn't execute, so safe to import at runtime only.
        # We build tiers now; openai import is attempted here.
        try:
            from openai import AsyncOpenAI  # type: ignore
        except Exception:  # pragma: no cover - missing in phase 1 env

            class _DummyAsyncOpenAI:  # type: ignore
                def __init__(self, **kw: object) -> None:
                    self._kw = kw

                    class _Models:
                        async def list(self, **k: object) -> object:
                            return type("obj", (), {"data": []})()

                    class _Chat:
                        class _Completions:
                            async def create(self, **k: object) -> object:
                                raise RuntimeError("openai not installed")

                        completions = _Completions()

                    self.models = _Models()
                    self.chat = _Chat()

            AsyncOpenAI = _DummyAsyncOpenAI  # type: ignore

        # Hetzner tier (T1)
        hetzner_key: str = str(getattr(s, "hetzner_api_key", "") or "")
        hetzner_base: str = str(getattr(s, "hetzner_base_url", "https://inference.hetzner.com/api/v1"))
        hetzner_primary: str = str(getattr(s, "llm_primary_model", "Qwen/Qwen3.6-35B-A3B-FP8"))
        hetzner_fallback: str = str(getattr(s, "llm_fallback_model", "Qwen3.8-27B"))

        if hetzner_key:
            client = AsyncOpenAI(base_url=hetzner_base, api_key=hetzner_key)
            tier = Tier(
                name="hetzner",
                base_url=hetzner_base,
                api_key=hetzner_key,
                model=hetzner_primary,
                client=client,
                fallback_model=hetzner_fallback,
            )
            self._tiers.append(tier)
            self._breakers[tier.name] = _BreakerState()

        # Zen tier (T2)
        opencode_key: str = str(getattr(s, "opencode_api_key", "") or "")
        zen_base: str = str(getattr(s, "zen_base_url", "https://opencode.ai/zen/v1"))
        zen_model: str = str(getattr(s, "zen_free_model", "mimo-v2.5-free"))

        if opencode_key:
            client = AsyncOpenAI(
                base_url=zen_base,
                api_key=opencode_key,
                default_headers={
                    "User-Agent": "pickme-bot/1.0",
                    "x-opencode-session": _SESSION_ID,
                },
            )
            tier = Tier(
                name="zen",
                base_url=zen_base,
                api_key=opencode_key,
                model=zen_model,
                client=client,
            )
            self._tiers.append(tier)
            self._breakers[tier.name] = _BreakerState()

        # Go tier (T3)
        go_enabled: bool = bool(getattr(s, "go_enabled", True))
        go_base: str = str(getattr(s, "go_base_url", "https://opencode.ai/zen/go/v1"))
        go_model: str = str(getattr(s, "go_model", "mimo-v2.5"))

        if go_enabled and opencode_key:
            client = AsyncOpenAI(
                base_url=go_base,
                api_key=opencode_key,
                default_headers={
                    "User-Agent": "pickme-bot/1.0",
                    "x-opencode-session": _SESSION_ID,
                },
            )
            tier = Tier(
                name="go",
                base_url=go_base,
                api_key=opencode_key,
                model=go_model,
                client=client,
            )
            self._tiers.append(tier)
            self._breakers[tier.name] = _BreakerState()

        # Cost order is already hetzner, zen, go as appended.
        logger.info("provider tiers: %s", [f"{t.name}:{t.model}" for t in self._tiers])

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Initial model discovery + start health-poll task (every 150s)."""
        self._stop_event.clear()
        # Initial discovery for each tier.
        for tier in list(self._tiers):
            try:
                await self._discover_tier(tier)
            except Exception:
                logger.debug("initial discovery failed for %s", tier.name, exc_info=True)
        # Start poll task.
        if self._health_task is None or self._health_task.done():
            self._health_task = asyncio.create_task(self._health_poll_loop(), name="pickme-health-poll")
            logger.info("health poll task started (every 150s)")

    async def stop(self) -> None:
        """Stop health poll task."""
        self._stop_event.set()
        if self._health_task is not None:
            self._health_task.cancel()
            try:
                await asyncio.wait_for(self._health_task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._health_task = None
        logger.info("provider registry stopped")

    # -- active tiers / breaker -------------------------------------------

    def active_tiers(self) -> list[Tier]:
        """Return cost-ordered tiers whose breaker is closed or half-open eligible."""
        now = time.monotonic()
        result: list[Tier] = []
        for tier in self._tiers:
            st = self._breakers.get(tier.name)
            if st is None:
                result.append(tier)
                continue
            # If model_missing open without timeout, stays open until discovery clears it.
            if st.open_model_missing:
                continue
            if st.open_until is not None and now < st.open_until:
                # Still open.
                continue
            # If cooldown elapsed, treat as half-open eligible (include).
            # The next request will be a probe; success closes breaker inside mark_success,
            # failure re-opens inside mark_failure.
            result.append(tier)
        return result

    def mark_success(self, tier_name: str) -> None:
        """Record a successful request — closes breaker."""
        st = self._breakers.get(tier_name)
        if st is None:
            return
        st.consecutive_5xx = 0
        st.open_until = None
        st.open_model_missing = False
        st.last_error = None
        st.healthy = True

    def mark_failure(self, tier_name: str, kind: str) -> None:
        """Record a failure; apply breaker policy by kind.

        kind: "http5xx" | "auth" | "model_missing" | "quota"
        """
        st = self._breakers.get(tier_name)
        if st is None:
            return
        now = time.monotonic()
        kind = kind.lower()
        if kind == "http5xx":
            st.consecutive_5xx += 1
            st.last_error = "http5xx"
            if st.consecutive_5xx >= 5:
                st.open_until = now + 120.0
                st.healthy = False
                logger.warning("breaker OPEN tier=%s kind=http5xx cooldown=120s", tier_name)
            # Zen best-effort: still log but apply same breaker? spec says zen failures logged not fatal.
            # We still apply breaker but treat gracefully; logger notes best-effort.
            if tier_name == "zen":
                logger.info("zen tier failure (best-effort) kind=%s", kind)
        elif kind == "auth":
            st.open_until = now + 3600.0
            st.last_error = "auth"
            st.healthy = False
            st.consecutive_5xx = 0
            logger.warning("breaker OPEN tier=%s kind=auth cooldown=3600s", tier_name)
        elif kind == "model_missing":
            st.open_model_missing = True
            st.open_until = None
            st.last_error = "model_missing"
            st.healthy = False
            st.consecutive_5xx = 0
            logger.warning("breaker OPEN tier=%s kind=model_missing until next health poll", tier_name)
        elif kind == "quota":
            st.open_until = now + 900.0
            st.last_error = "quota"
            st.healthy = False
            st.consecutive_5xx = 0
            logger.warning("breaker OPEN tier=%s kind=quota cooldown=900s", tier_name)
        else:
            # Unknown kind -> treat as http5xx.
            st.consecutive_5xx += 1
            st.last_error = kind
            if st.consecutive_5xx >= 5:
                st.open_until = now + 120.0
                st.healthy = False
                logger.warning("breaker OPEN tier=%s kind=%s (as http5xx) cooldown=120s", tier_name, kind)

    def status(self) -> list[TierStatus]:
        """Return per-tier status for /status."""
        now = time.monotonic()
        out: list[TierStatus] = []
        for tier in self._tiers:
            st = self._breakers.get(tier.name)
            if st is None:
                out.append(TierStatus(name=tier.name, model=tier.model, healthy=True, breaker_open=False, last_error=None))
                continue
            is_open = st.is_open(now)
            out.append(
                TierStatus(
                    name=tier.name,
                    model=tier.model,
                    healthy=st.healthy and not is_open,
                    breaker_open=is_open,
                    last_error=st.last_error,
                )
            )
        return out

    # -- discovery / health polling ---------------------------------------

    async def _discover_tier(self, tier: Tier) -> None:
        """Discover/validate model for a single tier via models.list (timeout 10s).

        Keeps configured model if listed; else for hetzner tries fallback, else first
        listed model. Empty listing -> mark model_missing. 429 means alive but busy
        — no state change. Zen failures logged best-effort.
        """
        # Draw from shared token bucket.
        try:
            if self._bucket is not None:
                await self._bucket.acquire()  # type: ignore[attr-defined]
        except Exception:
            logger.debug("bucket acquire failed during discovery", exc_info=True)

        # Timeout 10s for discovery.
        try:
            resp = await asyncio.wait_for(tier.client.models.list(), timeout=10.0)  # type: ignore[attr-defined]
        except asyncio.TimeoutError:
            logger.warning("discovery timeout for tier %s", tier.name)
            if tier.name == "zen":
                logger.info("zen discovery timeout (best-effort)")
            return
        except Exception as exc:  # noqa: BLE001
            # 429 -> alive but busy, no state change.
            sc = getattr(exc, "status_code", None)
            if sc == 429 or "429" in str(exc) or "rate limit" in str(exc).lower():
                logger.info("discovery 429 for tier %s — alive but busy, no state change", tier.name)
                return
            # Auth etc? mark appropriately but not for zen fatal?
            logger.warning("discovery failed tier=%s err=%s", tier.name, exc)
            if tier.name == "zen":
                # best-effort
                return
            return

        # Parse listing.
        try:
            # openai response: resp.data is list of Model objects with .id
            data = getattr(resp, "data", None)
            if data is None and isinstance(resp, dict):
                data = resp.get("data", [])
            model_ids: list[str] = []
            if data:
                for m in data:  # type: ignore[union-attr]
                    mid = getattr(m, "id", None)
                    if mid is None and isinstance(m, dict):
                        mid = m.get("id")
                    if isinstance(mid, str):
                        model_ids.append(mid)
        except Exception:
            logger.debug("failed to parse models listing for %s", tier.name, exc_info=True)
            return

        if not model_ids:
            logger.warning("empty model listing for tier %s -> mark model_missing", tier.name)
            # Empty listing -> mark model_missing (but not for zen fatal? spec says zen best-effort)
            if tier.name != "zen":
                self.mark_failure(tier.name, "model_missing")
            return

        # Check if configured model is listed.
        if tier.model in model_ids:
            # Healthy; if was model_missing, clear it.
            st = self._breakers.get(tier.name)
            if st and st.open_model_missing:
                self.mark_success(tier.name)
                logger.info("tier %s model %s now listed -> breaker closed", tier.name, tier.model)
            # Also mark success to close any open breaker from prior discovery? Only if model_missing case.
            return

        # Not listed.
        if tier.name == "hetzner" and tier.fallback_model and tier.fallback_model in model_ids:
            old = tier.model
            tier.model = tier.fallback_model
            logger.warning("hetzner primary model %s not listed, switching to fallback %s", old, tier.model)
            # Clear model_missing if was open.
            st = self._breakers.get(tier.name)
            if st and st.open_model_missing:
                self.mark_success(tier.name)
            return

        # Fallback to first listed model (rename only, log).
        old = tier.model
        tier.model = model_ids[0]
        logger.warning("tier %s model %s not listed, switching to first listed %s", tier.name, old, tier.model)
        st = self._breakers.get(tier.name)
        if st and st.open_model_missing:
            self.mark_success(tier.name)

    async def _health_poll_loop(self) -> None:
        """Periodic health poll every 150s (infrequent; drives failover/fail-back)."""
        try:
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=150.0)
                except asyncio.TimeoutError:
                    pass
                if self._stop_event.is_set():
                    break
                for tier in list(self._tiers):
                    try:
                        await self._discover_tier(tier)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.debug("health poll error for %s", tier.name, exc_info=True)
                # If a cheaper tier's poll succeeded, it automatically rejoins via
                # clearing its breaker in _discover_tier / mark_success (fail-back).
        except asyncio.CancelledError:
            logger.info("health poll cancelled")
            raise
