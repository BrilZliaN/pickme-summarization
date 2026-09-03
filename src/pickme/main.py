"""Application entrypoint — wiring config, db, LLM, worker, Telegram polling and background timers."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pickme.config import get_settings, Settings
from pickme.db.connection import Database
from pickme.llm.client import LLMClient, TokenBucket
from pickme.llm.registry import ProviderRegistry
from pickme.pipelines import (
    PipelineError,
    drain_pending,
    maybe_update_rolling,
    reaper_tick,
    run_evaluate,
    run_memory_merge,
    run_qa,
    run_summarize,
)
from pickme.schemas.profile import EvalTarget
from pickme.worker.queue import JobQueue

logger = logging.getLogger("pickme.main")


@dataclass
class App:
    """Application container."""

    settings: Settings
    db: Database
    llm: LLMClient
    registry: ProviderRegistry
    queue: JobQueue
    bot: Any | None = None  # aiogram.Bot
    bot_id: int = 0
    bot_username: str = ""


# ---------------------------------------------------------------------------
# Logging setup — minimal JSON formatter to stdout
# ---------------------------------------------------------------------------


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _setup_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        root.addHandler(handler)
    else:
        root.handlers = [handler]
    try:
        lvl = getattr(logging, level.upper(), logging.INFO)
        if isinstance(lvl, int):
            root.setLevel(lvl)
            handler.setLevel(lvl)
    except Exception:
        root.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    """Entrypoint: wire everything and run polling."""
    settings = get_settings()
    _setup_logging(getattr(settings, "log_level", "INFO"))

    db_path = str(Path(settings.data_dir) / "pickme.db")
    db = Database(db_path)
    await db.connect()
    logger.info("database connected at %s", db_path)

    # Single shared token bucket for ALL LLM traffic (worker, router, health polls)
    bucket = TokenBucket(capacity=int(settings.rate_limit_per_60s), per_seconds=60.0)
    registry = ProviderRegistry(settings, bucket)  # type: ignore[arg-type]
    llm = LLMClient(settings, registry, bucket=bucket)  # type: ignore[arg-type]

    async def _log_sink(feature: str, provider: str, model: str, ptoks: int, ctoks: int, ok: bool, error: str | None) -> None:
        try:
            from pickme.db import queries as q  # type: ignore

            now_ms = int(time.time() * 1000)
            await q.insert_llm_log(db.conn, feature, provider, model, int(ptoks), int(ctoks), bool(ok), error, now_ms)
        except Exception as exc:  # noqa: BLE001
            logger.debug("log_sink failed %s", exc, exc_info=True)

    llm.set_log_sink(_log_sink)  # type: ignore[attr-defined]

    # Wire queue
    queue = JobQueue()

    async def _summarize_wrapper(payload: dict[str, Any]) -> Any:
        return await run_summarize(db, llm, settings, int(payload["chat_id"]), int(payload["count"]))

    async def _evaluate_wrapper(payload: dict[str, Any]) -> Any:
        target = EvalTarget.model_validate(payload["target"])
        return await run_evaluate(db, llm, settings, int(payload["chat_id"]), int(payload["requester_id"]), target)

    async def _qa_wrapper(payload: dict[str, Any]) -> Any:
        return await run_qa(
            db,
            llm,
            settings,
            int(payload["chat_id"]),
            str(payload["question"]),
            asker_id=payload.get("asker_id"),
            quote=payload.get("quote"),
        )

    async def _memory_wrapper(payload: dict[str, Any]) -> Any:
        return await run_memory_merge(db, llm, settings, int(payload["chat_id"]), int(payload["user_id"]))

    async def _rolling_wrapper(payload: dict[str, Any]) -> Any:
        return await maybe_update_rolling(db, llm, settings, int(payload["chat_id"]))

    for kind, fn in [
        ("summarize", _summarize_wrapper),
        ("evaluate", _evaluate_wrapper),
        ("qa", _qa_wrapper),
        ("memory_merge", _memory_wrapper),
        ("rolling_summary", _rolling_wrapper),
    ]:
        queue.register(kind, fn)

    app = App(settings=settings, db=db, llm=llm, registry=registry, queue=queue)

    # Build bot + dispatcher
    from pickme.telegram.bot import build_bot  # type: ignore

    bot, dp = build_bot(app)  # type: ignore[arg-type]
    app.bot = bot

    # Cache bot identity BEFORE polling (required for AddressedToBot filter & ingest middleware)
    try:
        me = await bot.get_me()
        app.bot_id = int(getattr(me, "id", 0) or 0)
        app.bot_username = str(getattr(me, "username", "") or "")
        logger.info("bot identity cached id=%s username=%s", app.bot_id, app.bot_username)
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to cache bot identity %s", exc, exc_info=True)

    # Start queue + llm
    await queue.start()
    try:
        await llm.start()  # delegates to registry.start()
    except Exception as exc:  # noqa: BLE001
        logger.warning("llm.start failed %s", exc, exc_info=True)

    # Background timers
    stop_event = asyncio.Event()
    bg_tasks: list[asyncio.Task[Any]] = []

    async def _rolling_timer() -> None:
        """Every 30s: for each chat, if >=25 new messages since rolling_summary_at, enqueue rolling_summary."""
        try:
            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=30.0)
                    if stop_event.is_set():
                        break
                except asyncio.TimeoutError:
                    pass
                if stop_event.is_set():
                    break
                try:
                    from pickme.db import queries as q  # type: ignore

                    chats = await q.list_chats(db.conn)  # type: ignore[attr-defined]
                    for ch in chats:
                        try:
                            cid = int(ch.get("chat_id") or ch.get("id") or 0)  # type: ignore
                            if not cid:
                                continue
                            at = ch.get("rolling_summary_at")
                            since = int(at) if at is not None else 0
                            cnt = await q.count_messages_since(db.conn, cid, since)
                            if cnt >= 25:
                                app.queue.submit_background("rolling_summary", cid, {"chat_id": cid})  # type: ignore[attr-defined]
                                logger.debug("rolling_summary enqueued chat=%s cnt=%s", cid, cnt)
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("rolling enqueue failed chat=%r %s", ch, exc, exc_info=True)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("rolling timer failed %s", exc, exc_info=True)
        except asyncio.CancelledError:
            logger.info("rolling timer cancelled")
            raise

    async def _drain_timer() -> None:
        """Every 300s (run once immediately): drain pending memory merges."""
        try:
            try:
                n = await drain_pending(db, llm, settings)
                logger.info("drain_pending startup -> %s", n)
            except Exception as exc:  # noqa: BLE001
                logger.debug("drain_pending startup failed %s", exc, exc_info=True)
            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=300.0)
                    if stop_event.is_set():
                        break
                except asyncio.TimeoutError:
                    pass
                if stop_event.is_set():
                    break
                try:
                    n = await drain_pending(db, llm, settings)
                    if n:
                        logger.info("drain_pending -> %s", n)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("drain_pending failed %s", exc, exc_info=True)
        except asyncio.CancelledError:
            logger.info("drain timer cancelled")
            raise

    async def _reaper_timer() -> None:
        """Every 3600s: decay inactive profiles."""
        try:
            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=3600.0)
                    if stop_event.is_set():
                        break
                except asyncio.TimeoutError:
                    pass
                if stop_event.is_set():
                    break
                try:
                    n = await reaper_tick(db, settings)
                    if n:
                        logger.info("reaper_tick -> %s", n)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("reaper_tick failed %s", exc, exc_info=True)
        except asyncio.CancelledError:
            logger.info("reaper timer cancelled")
            raise

    for coro in [_rolling_timer(), _drain_timer(), _reaper_timer()]:
        t = asyncio.create_task(coro)

        def _log_bg_error(fut: asyncio.Future[Any], _name: str = coro.__class__.__name__) -> None:  # type: ignore
            try:
                fut.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning("background task %s failed %s", _name, exc, exc_info=True)

        t.add_done_callback(_log_bg_error)
        bg_tasks.append(t)

    logger.info("app started — polling")

    try:
        await dp.start_polling(bot)
    finally:
        logger.info("shutting down")
        stop_event.set()
        for t in bg_tasks:
            t.cancel()
        if bg_tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*bg_tasks, return_exceptions=True), timeout=5.0)
            except asyncio.TimeoutError:
                pass
        try:
            await queue.stop()
        except Exception as exc:  # noqa: BLE001
            logger.debug("queue.stop failed %s", exc, exc_info=True)
        try:
            await llm.stop()
        except Exception as exc:  # noqa: BLE001
            logger.debug("llm.stop failed %s", exc, exc_info=True)
        try:
            await db.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("db.close failed %s", exc, exc_info=True)
        try:
            await bot.session.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("bot.session.close failed %s", exc, exc_info=True)
        logger.info("shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
