"""LLM client with rate limiting, concurrency control, tiered failover and retries.

Binding implementation of bot-plan §5.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass

logger = logging.getLogger("pickme.llm")

# Avoid hard import failure during py_compile / phase where config lane not yet landed.
# We use a string annotation / lazy import pattern, but keep the runtime import.
try:
    from pickme.config import Settings as _Settings  # type: ignore
except Exception:  # pragma: no cover - missing in phase 1
    _Settings = object  # type: ignore

try:
    from pickme.llm.registry import ProviderRegistry as _ProviderRegistry, Tier as _Tier  # type: ignore
except Exception:  # pragma: no cover
    _ProviderRegistry = object  # type: ignore
    _Tier = object  # type: ignore

# Use Any for type hints to avoid LSP variable-in-type errors before foundation lane lands.
from typing import Any as _Any

Settings = _Any  # type: ignore
ProviderRegistry = _Any  # type: ignore
Tier = _Any  # type: ignore


# ---------------------------------------------------------------------------
# Public data structures
# ---------------------------------------------------------------------------

@dataclass
class ChatResult:
    """Result of a successful chat completion."""

    text: str
    provider: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


class LLMError(Exception):
    """Raised when all LLM tiers are exhausted or unavailable."""


class RateLimitedError(Exception):
    """Internal: a tier returned 429. Caller rotates model / fails over immediately."""

    def __init__(self, tier: str, model: str, retry_after: float | None, exc: BaseException) -> None:
        super().__init__(f"429 on {tier}/{model}")
        self.tier = tier
        self.model = model
        self.retry_after = retry_after
        self.exc = exc


# ---------------------------------------------------------------------------
# Token bucket — continuous refill
# ---------------------------------------------------------------------------

class TokenBucket:
    """Async continuous-refill token bucket.

    Shared by worker jobs, inline router calls and health polls — all draw
    from the same budget (plan §4.6, §5).
    """

    def __init__(self, capacity: int, per_seconds: float = 60.0) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        if per_seconds <= 0:
            raise ValueError("per_seconds must be > 0")
        self.capacity: int = capacity
        self.per_seconds: float = per_seconds
        self._rate: float = capacity / per_seconds
        self._tokens: float = float(capacity)
        self._last: float = time.monotonic()
        self._lock: asyncio.Lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until one token is available, then consume it."""
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                # Continuous refill.
                self._tokens = min(float(self.capacity), self._tokens + elapsed * self._rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                # Not enough tokens — compute wait time.
                needed = 1.0 - self._tokens
                wait = needed / self._rate if self._rate > 0 else 0.1
                # Clamp to small positive.
                if wait < 0.001:
                    wait = 0.001
            # Sleep outside the lock so other waiters can refill concurrently.
            await asyncio.sleep(wait)


# ---------------------------------------------------------------------------
# Robust JSON extraction
# ---------------------------------------------------------------------------

def extract_json(text: str) -> dict | list | None:
    """Robust JSON-from-text parser.

    Strips ```json fences, finds the first balanced {..} or [..] (string-aware),
    then ``json.loads``. Returns ``None`` on failure.
    """
    if not text:
        return None
    stripped = text.strip()
    # Strip ``` fences (```json ... ``` or ``` ... ```).
    if stripped.startswith("```"):
        # Remove opening fence line (``` or ```json etc.)
        first_nl = stripped.find("\n")
        if first_nl != -1:
            stripped = stripped[first_nl + 1 :]
        else:
            # Only fence?
            stripped = stripped[3:]
        # Remove trailing fence.
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
        stripped = stripped.strip()

    # Find first '{' or '['.
    start_brace = stripped.find("{")
    start_bracket = stripped.find("[")
    if start_brace == -1 and start_bracket == -1:
        return None
    if start_brace == -1:
        start = start_bracket
        open_c, close_c = "[", "]"
    elif start_bracket == -1:
        start = start_brace
        open_c, close_c = "{", "}"
    else:
        if start_brace < start_bracket:
            start = start_brace
            open_c, close_c = "{", "}"
        else:
            start = start_bracket
            open_c, close_c = "[", "]"

    # String-aware balanced scan.
    depth = 0
    in_string = False
    escape = False
    end: int | None = None
    for i in range(start, len(stripped)):
        ch = stripped[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        else:
            if ch == '"':
                in_string = True
                continue
            if ch == open_c:
                depth += 1
            elif ch == close_c:
                depth -= 1
                if depth == 0:
                    end = i
                    break
                if depth < 0:
                    # Mismatched; no valid balanced region.
                    return None
    if end is None:
        return None
    candidate = stripped[start : end + 1]
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Helpers for error classification and headers
# ---------------------------------------------------------------------------

def _status_from_exception(exc: BaseException) -> int | None:
    """Best-effort extraction of HTTP status code from OpenAI / httpx exceptions."""
    # openai.APIStatusError / APIError have .status_code
    sc = getattr(exc, "status_code", None)
    if isinstance(sc, int):
        return sc
    # httpx / openai may nest response
    resp = getattr(exc, "response", None)
    if resp is not None:
        sc2 = getattr(resp, "status_code", None)
        if isinstance(sc2, int):
            return sc2
    # Some wrappers use .code string
    return None


def _retry_after_seconds(exc: BaseException) -> float | None:
    """Extract Retry-After seconds from exception headers if present."""
    # Try several locations.
    for attr in ("response", "headers"):
        pass
    resp = getattr(exc, "response", None)
    headers = None
    if resp is not None:
        headers = getattr(resp, "headers", None)
    if headers is None:
        headers = getattr(exc, "headers", None)
    if headers is None:
        return None
    try:
        # headers may be case-insensitive dict / httpx Headers
        val = None
        # httpx Headers supports .get
        if hasattr(headers, "get"):
            val = headers.get("Retry-After") or headers.get("retry-after")
        elif isinstance(headers, dict):
            val = headers.get("Retry-After") or headers.get("retry-after")
        if val is None:
            return None
        return float(str(val).strip())
    except Exception:
        return None


def _is_rate_limit(exc: BaseException) -> bool:
    sc = _status_from_exception(exc)
    if sc == 429:
        return True
    # Fallback: check type name / message.
    name = type(exc).__name__.lower()
    if "ratelimit" in name or "rate_limit" in name:
        return True
    msg = str(exc).lower()
    if "rate limit" in msg or "too many requests" in msg:
        # Only treat as rate-limit if not clearly another status.
        if sc is None:
            return True
    return False


def _classify_error(exc: BaseException) -> str:
    """Return kind string for registry.mark_failure.

    Kinds: http5xx | auth | model_missing | quota (quota currently unused directly).
    """
    sc = _status_from_exception(exc)
    if sc is not None:
        if 500 <= sc <= 599:
            return "http5xx"
        if sc in (401, 403):
            return "auth"
        if sc == 404:
            return "model_missing"
    msg = str(exc).lower()
    if "model" in msg and ("not found" in msg or "does not exist" in msg or "missing" in msg or "404" in msg):
        return "model_missing"
    if sc is not None and 500 <= sc <= 599:
        return "http5xx"
    # Quota often surfaces as 429 with quota message or 403; keep mapping simple.
    if "quota" in msg or "billing" in msg:
        return "quota"
    # Fallback for unknown -> http5xx so breaker logic still counts it as failure.
    if sc is None:
        return "http5xx"
    if sc is not None and sc >= 400:
        # For 400 etc not handled specially, treat as http5xx-like tier failure
        # except 429 which is handled separately.
        return "http5xx"
    return "http5xx"


# ---------------------------------------------------------------------------
# LLMClient
# ---------------------------------------------------------------------------

class LLMClient:
    """Tiered LLM client with token bucket, semaphore, retries and failover.

    Binding behaviour from plan §5 (LLM Client Design).
    """

    def __init__(
        self,
        settings: Settings,
        registry: ProviderRegistry,
        bucket: TokenBucket | None = None,
    ) -> None:
        self._settings = settings
        self._registry: ProviderRegistry = registry  # type: ignore[assignment]
        cap = int(getattr(settings, "rate_limit_per_60s", 8))
        conc = int(getattr(settings, "llm_concurrency", 2))
        if bucket is not None:
            self._bucket = bucket
        else:
            self._bucket = TokenBucket(capacity=cap, per_seconds=60.0)
        self._semaphore = asyncio.Semaphore(conc)
        self._log_sink = None  # async callable
        # Metrics counters.
        self._metrics: dict[str, float | int] = {
            "requests": 0,
            "rate_limited_429": 0,
            "failures": 0,
            "latency_ms_last": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "tier_failovers": 0,
        }
        self._metrics_lock = asyncio.Lock()

    def set_log_sink(self, sink) -> None:
        """Set async log sink: ``async def sink(feature, provider, model, prompt_tokens, completion_tokens, ok, error)``.

        Called fire-and-forget; never raises to caller.
        """
        self._log_sink = sink

    async def start(self) -> None:
        """Delegate to registry.start()."""
        await self._registry.start()  # type: ignore[attr-defined]

    async def stop(self) -> None:
        """Delegate to registry.stop()."""
        await self._registry.stop()  # type: ignore[attr-defined]

    def metrics(self) -> dict:
        """Return a snapshot of client metrics."""
        return dict(self._metrics)

    async def _log(self, feature: str, provider: str, model: str, prompt_tokens: int, completion_tokens: int, ok: bool, error: str | None) -> None:
        if self._log_sink is None:
            return
        try:
            maybe = self._log_sink(feature, provider, model, prompt_tokens, completion_tokens, ok, error)
            if asyncio.iscoroutine(maybe):
                # Fire-and-forget: schedule without awaiting indefinitely.
                # We await with a shield-like timeout so we don't block chat().
                try:
                    await asyncio.wait_for(maybe, timeout=2.0)  # type: ignore[arg-type]
                except asyncio.TimeoutError:
                    logger.debug("log_sink timed out")
        except Exception:
            logger.debug("log_sink raised", exc_info=True)

    async def _call_tier(  # noqa: C901
        self,
        tier: Tier,
        messages: list[dict],
        *,
        json_mode: bool,
        temperature: float | None,
        max_tokens: int | None,
        feature: str,
        model_override: str | None = None,
    ) -> ChatResult:
        """Execute ONE chat attempt on a single tier+model.

        Policy: 429 -> raise RateLimitedError immediately (no same-tier backoff;
        the caller rotates to the next model, then the next provider). Non-429
        -> mark_failure (breaker) and re-raise. Hetzner extra_body 400 retries
        once without extra_body. Global backoff lives in chat(), not here.
        """
        is_hetzner = getattr(tier, "name", "") == "hetzner"
        use_extra_body = is_hetzner
        model = model_override or tier.model

        while True:
            # One token per HTTP attempt.
            await self._bucket.acquire()
            async with self._semaphore:
                start_ts = time.monotonic()
                try:
                    kwargs: dict = {
                        "model": model,
                        "messages": messages,  # type: ignore[arg-type]
                    }
                    if temperature is not None:
                        kwargs["temperature"] = temperature
                    if max_tokens is not None:
                        kwargs["max_tokens"] = max_tokens
                    if json_mode:
                        kwargs["response_format"] = {"type": "json_object"}
                    if use_extra_body:
                        kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}

                    # Fire the request.
                    resp = await tier.client.chat.completions.create(**kwargs)  # type: ignore[attr-defined]

                    # Success path.
                    latency_ms = int((time.monotonic() - start_ts) * 1000)
                    async with self._metrics_lock:
                        self._metrics["requests"] = int(self._metrics["requests"]) + 1  # type: ignore[arg-type]
                        self._metrics["latency_ms_last"] = latency_ms

                    # Extract text and usage.
                    text = ""
                    prompt_tokens = 0
                    completion_tokens = 0
                    try:
                        # OpenAI response shape.
                        choice = resp.choices[0]  # type: ignore[attr-defined]
                        # message may be object with .content
                        msg = getattr(choice, "message", None)
                        if msg is not None:
                            text = getattr(msg, "content", "") or ""
                        else:
                            text = getattr(choice, "text", "") or ""
                        usage = getattr(resp, "usage", None)
                        if usage is not None:
                            prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
                            completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
                    except Exception:
                        logger.debug("failed to parse response", exc_info=True)

                    async with self._metrics_lock:
                        self._metrics["tokens_in"] = int(self._metrics["tokens_in"]) + prompt_tokens  # type: ignore[arg-type]
                        self._metrics["tokens_out"] = int(self._metrics["tokens_out"]) + completion_tokens  # type: ignore[arg-type]

                    # Mark success & log.
                    try:
                        self._registry.mark_success(tier.name)  # type: ignore[attr-defined]
                    except Exception:
                        logger.debug("mark_success failed", exc_info=True)
                    # Fire-and-forget log sink.
                    asyncio.create_task(
                        self._log(feature, tier.name, model, prompt_tokens, completion_tokens, True, None)
                    )
                    return ChatResult(
                        text=text or "",
                        provider=tier.name,
                        model=model,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                    )

                except Exception as exc:  # noqa: BLE001
                    latency_ms = int((time.monotonic() - start_ts) * 1000)
                    async with self._metrics_lock:
                        self._metrics["latency_ms_last"] = latency_ms

                    sc = _status_from_exception(exc)

                    # Hetzner extra_body 400 -> retry once without extra_body (non-fatal hint).
                    if use_extra_body and sc == 400:
                        logger.info("hetzner extra_body 400, retrying without extra_body (tier=%s)", tier.name)
                        use_extra_body = False
                        continue

                    # 429 -> immediate failover: no same-tier backoff, no breaker.
                    if _is_rate_limit(exc):
                        async with self._metrics_lock:
                            self._metrics["rate_limited_429"] = int(self._metrics["rate_limited_429"]) + 1  # type: ignore[arg-type]
                        asyncio.create_task(
                            self._log(feature, tier.name, model, 0, 0, False, f"429: {exc}")
                        )
                        raise RateLimitedError(tier.name, model, _retry_after_seconds(exc), exc) from exc

                    # Non-429 terminal failure for this tier -> breaker + failover.
                    kind = _classify_error(exc)
                    try:
                        self._registry.mark_failure(tier.name, kind)  # type: ignore[attr-defined]
                    except Exception:
                        logger.debug("mark_failure failed", exc_info=True)
                    asyncio.create_task(
                        self._log(feature, tier.name, model, 0, 0, False, f"{kind}: {exc}")
                    )
                    async with self._metrics_lock:
                        self._metrics["failures"] = int(self._metrics["failures"]) + 1  # type: ignore[arg-type]
                    raise

    async def chat(
        self,
        messages: list[dict],
        *,
        feature: str = "misc",
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        """Execute a chat completion across tiered providers.

        429 policy: fail over IMMEDIATELY — next model within the tier (Zen
        free-pool rotation, preference order first), then the next provider.
        Only when the whole chain is rate-limited does it wait (Retry-After-
        aware exponential backoff with jitter) and retry the chain, up to 3
        rounds. 429 never opens a circuit breaker; non-429 tier failures do.
        """
        max_rounds = 3
        errors: list[str] = []

        for round_idx in range(max_rounds):
            tiers: list[Tier] = self._registry.active_tiers()  # type: ignore[assignment]
            if not tiers:
                await self._log(feature, "none", "none", 0, 0, False, "all LLM tiers unavailable")
                raise LLMError("all LLM tiers unavailable")

            saw_rate_limit = False
            saw_other_failure = False
            retry_after_hint: float | None = None

            for idx, tier in enumerate(tiers):
                candidates: list[str] = self._registry.tier_model_candidates(tier)  # type: ignore[attr-defined]
                for model in candidates:
                    try:
                        result = await self._call_tier(
                            tier,
                            messages,
                            json_mode=json_mode,
                            temperature=temperature,
                            max_tokens=max_tokens,
                            feature=feature,
                            model_override=model,
                        )
                        if idx > 0 or model != getattr(tier, "model", model):
                            async with self._metrics_lock:
                                self._metrics["tier_failovers"] = int(self._metrics["tier_failovers"]) + 1  # type: ignore[arg-type]
                        return result
                    except RateLimitedError as rle:
                        saw_rate_limit = True
                        if rle.retry_after is not None:
                            retry_after_hint = max(retry_after_hint or 0.0, rle.retry_after)
                        errors.append(f"{rle.tier}/{rle.model}: 429")
                        logger.warning(
                            "429 on %s/%s -> next candidate (round %d/%d)",
                            rle.tier,
                            rle.model,
                            round_idx + 1,
                            max_rounds,
                        )
                        continue  # next model in this tier, then next tier
                    except Exception as exc:  # noqa: BLE001
                        # Non-429: _call_tier already marked the breaker + logged.
                        saw_other_failure = True
                        errors.append(f"{getattr(tier, 'name', '?')}: {exc}")
                        break  # abandon this tier, move to the next provider

            # Round finished without success.
            if not (saw_rate_limit and not saw_other_failure):
                break  # mixed or non-429 failures — no global backoff rounds
            if round_idx >= max_rounds - 1:
                break
            # Whole chain rate-limited -> global backoff, then retry the chain.
            wait = retry_after_hint if (retry_after_hint is not None and retry_after_hint > 0) else float(2 ** round_idx)
            wait *= random.uniform(0.9, 1.2)
            logger.warning(
                "all tiers rate-limited (round %d/%d); backing off %.1fs before retrying the chain",
                round_idx + 1,
                max_rounds,
                wait,
            )
            await asyncio.sleep(wait)

        summary = "; ".join(errors) if errors else "unknown"
        raise LLMError(f"all tiers failed — {summary}")
