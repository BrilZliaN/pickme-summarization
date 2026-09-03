"""Single-worker FIFO job queue with per-chat serialization, coalescing and metrics.

Binding implementation of bot-plan §1 and §5.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("pickme.worker")


class JobAlreadyRunning(Exception):
    """Raised when (kind, chat_id) is already queued or running (coalesced away)."""


@dataclass
class Job:
    """Queued job descriptor."""

    kind: str
    chat_id: int
    payload: dict
    future: asyncio.Future = field(repr=False)


class JobQueue:
    """Single-worker FIFO queue with per-chat serialization, coalescing, and metrics.

    Semantics (plan §1, §5):
    - Exactly one worker task.
    - Per-chat ``asyncio.Lock`` ensures jobs for the same chat never race.
    - Coalescing: if (kind, chat_id) already queued or running -> ``JobAlreadyRunning``.
    - Registered handler ``async def fn(payload: dict) -> Any`` per kind.
    - Exceptions never kill the worker loop; they are forwarded to the awaiter.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        self._handlers: dict[str, object] = {}
        self._inflight: set[tuple[str, int]] = set()
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._worker_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._metrics: dict[str, int] = {
            "queued": 0,
            "active": 0,
            "done": 0,
            "failed": 0,
            "depth": 0,
        }
        self._metrics_lock = asyncio.Lock()

    # -- registration ------------------------------------------------------

    def register(self, kind: str, fn) -> None:
        """Register an async handler for a kind.

        Raises ``ValueError`` on duplicate registration (programmer error).
        """
        if kind in self._handlers:
            raise ValueError(f"handler for kind {kind!r} already registered")
        if not asyncio.iscoroutinefunction(fn):
            logger.warning("registering non-coroutine handler for kind %r", kind)
        self._handlers[kind] = fn

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Spawn exactly one worker task."""
        if self._worker_task is not None and not self._worker_task.done():
            logger.debug("JobQueue.start() called but worker already running")
            return
        self._stop_event.clear()
        self._worker_task = asyncio.create_task(self._worker_loop(), name="pickme-worker")
        logger.info("JobQueue worker started")

    async def stop(self) -> None:
        """Graceful stop: signal, wait <=5s, cancel if needed."""
        self._stop_event.set()
        if self._worker_task is None:
            return
        try:
            await asyncio.wait_for(self._worker_task, timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("JobQueue worker did not stop in 5s, cancelling")
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        except asyncio.CancelledError:
            pass
        finally:
            self._worker_task = None
            logger.info("JobQueue stopped")

    # -- submit ------------------------------------------------------------

    async def submit(self, kind: str, chat_id: int, payload: dict, timeout: float = 300.0) -> object:
        """Enqueue a job and await its result with timeout.

        Coalescing: if (kind, chat_id) already queued or running -> raise ``JobAlreadyRunning``.
        Timeout on submit cancels the wait but lets the job finish in background (future not cancelled).
        Job exceptions propagate to caller.
        """
        key = (kind, chat_id)
        if key in self._inflight:
            raise JobAlreadyRunning(f"job {key} already queued or running")

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        job = Job(kind=kind, chat_id=chat_id, payload=payload, future=future)

        self._inflight.add(key)
        await self._queue.put(job)
        async with self._metrics_lock:
            self._metrics["queued"] += 1
            self._metrics["depth"] = self._queue.qsize() + self._metrics["active"]

        try:
            # Wait for job completion with timeout; do not cancel the job future on timeout.
            result = await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
            return result
        except asyncio.TimeoutError:
            # Cancel the wait but let job finish in background.
            # The future remains pending; worker will set result/exception.
            # Attach a done callback to avoid unhandled exception warnings.
            def _ignore(_f: asyncio.Future) -> None:
                try:
                    _f.result()
                except Exception:
                    logger.debug("background job %s timeout — result ignored", key, exc_info=True)

            future.add_done_callback(_ignore)
            raise
        finally:
            # Depth will also be updated in worker loop; update now for timely metrics.
            async with self._metrics_lock:
                self._metrics["depth"] = self._queue.qsize() + self._metrics["active"]

    def submit_background(self, kind: str, chat_id: int, payload: dict) -> asyncio.Future | None:
        """Fire-and-forget variant; returns None if coalesced away.

        Exceptions are logged, never raised to caller.
        """
        key = (kind, chat_id)
        if key in self._inflight:
            logger.info("coalesced background job %s", key)
            return None

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        job = Job(kind=kind, chat_id=chat_id, payload=payload, future=future)

        self._inflight.add(key)
        self._queue.put_nowait(job)
        # Metrics — use try to avoid blocking.
        try:
            # Synchronous update if no contention; best-effort.
            self._metrics["queued"] += 1
            self._metrics["depth"] = self._queue.qsize() + self._metrics["active"]
        except Exception:
            pass

        def _log_bg(f: asyncio.Future) -> None:
            try:
                f.result()
            except Exception as exc:  # noqa: BLE001
                logger.warning("background job %s failed: %s", key, exc, exc_info=True)

        future.add_done_callback(_log_bg)
        return future

    def metrics(self) -> dict:
        """Return a snapshot of queue metrics.

        Keys: queued, active, done, failed, depth.
        """
        return dict(self._metrics)

    # -- worker loop -------------------------------------------------------

    async def _worker_loop(self) -> None:
        """Worker: pop job -> per-chat lock -> await handler -> set future result."""
        logger.info("worker loop entered")
        while not self._stop_event.is_set():
            try:
                # Wait for job with short poll so stop_event is checked promptly.
                try:
                    job: Job = await asyncio.wait_for(self._queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
            except asyncio.CancelledError:
                break

            key = (job.kind, job.chat_id)
            # Ensure per-chat lock exists.
            lock = self._chat_locks.get(job.chat_id)
            if lock is None:
                lock = asyncio.Lock()
                self._chat_locks[job.chat_id] = lock

            # Update active metrics.
            async with self._metrics_lock:
                self._metrics["active"] += 1
                self._metrics["depth"] = self._queue.qsize() + self._metrics["active"]

            try:
                async with lock:
                    fn = self._handlers.get(job.kind)
                    if fn is None:
                        msg = f"unregistered job kind {job.kind!r}"
                        logger.error(msg)
                        if not job.future.done():
                            job.future.set_exception(RuntimeError(msg))
                        async with self._metrics_lock:
                            self._metrics["failed"] += 1
                        continue

                    try:
                        result = await fn(job.payload)  # type: ignore[operator]
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("job %s failed: %s", key, exc, exc_info=True)
                        if not job.future.done():
                            job.future.set_exception(exc)
                        async with self._metrics_lock:
                            self._metrics["failed"] += 1
                    except BaseException as exc:  # noqa: BLE001
                        # BaseException (e.g. CancelledError) — still propagate.
                        logger.warning("job %s raised BaseException: %s", key, exc, exc_info=True)
                        if not job.future.done():
                            # BaseException cannot be set_exception with BaseException subclass that is not Exception
                            # Wrap cancellation.
                            if isinstance(exc, asyncio.CancelledError):
                                job.future.cancel()
                            else:
                                job.future.set_exception(RuntimeError(str(exc)))
                        async with self._metrics_lock:
                            self._metrics["failed"] += 1
                    else:
                        if not job.future.done():
                            job.future.set_result(result)
                        async with self._metrics_lock:
                            self._metrics["done"] += 1
            finally:
                # Always remove from inflight and mark queue done.
                self._inflight.discard(key)
                self._queue.task_done()
                async with self._metrics_lock:
                    self._metrics["active"] -= 1
                    if self._metrics["active"] < 0:
                        self._metrics["active"] = 0
                    self._metrics["depth"] = self._queue.qsize() + self._metrics["active"]

        logger.info("worker loop exited")


__all__ = ["JobAlreadyRunning", "Job", "JobQueue"]
