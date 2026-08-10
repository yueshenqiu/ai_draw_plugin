# -*- coding: utf-8 -*-
"""Small asyncio job queue used by the drawing plugin.

The manager deliberately accepts *coroutine factories* instead of coroutine
objects.  A queued job therefore owns no live coroutine until a concurrency
slot is available, which also makes cancelling queued work warning-free.

All public methods that inspect or mutate state are asynchronous.  A manager
instance is intended to be used from one asyncio event loop.
"""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from collections import Counter, deque
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable, Deque, Dict, List, Literal, Optional


JobState = Literal["queued", "running", "completed", "failed", "cancelled"]
CoroutineFactory = Callable[[], Awaitable[Any]]
CleanupCallback = Callable[[], Any]


class JobManagerError(RuntimeError):
    """Base class for job manager submission errors."""


class JobManagerClosedError(JobManagerError):
    """Raised when work is submitted after :meth:`JobManager.shutdown`."""


class QueueFullError(JobManagerError):
    """Raised when all running slots and queue slots are occupied."""


class SessionLimitError(JobManagerError):
    """Raised when a session already owns its maximum active job count."""


@dataclass(frozen=True)
class JobInfo:
    """Read-only snapshot returned by :meth:`JobManager.status`."""

    job_id: str
    session_key: str
    label: str
    status: JobState
    created_at: float
    started_at: Optional[float]
    finished_at: Optional[float]
    error: Optional[str]
    queue_position: Optional[int]
    cancel_requested: bool

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-friendly representation of this snapshot."""

        return asdict(self)


@dataclass
class _JobRecord:
    job_id: str
    session_key: str
    label: str
    factory: Optional[CoroutineFactory]
    cleanup: Optional[CleanupCallback]
    status: JobState
    created_at: float
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    finished_monotonic: Optional[float] = None
    error: Optional[str] = None
    task: Optional[asyncio.Task] = None
    runner_started: bool = False
    finalizing: bool = False
    cancel_requested: bool = False


class JobManager:
    """Bounded FIFO queue with global and per-session concurrency controls.

    ``max_queued`` limits only jobs waiting for a running slot.  A submission
    can still start immediately when ``max_queued`` is zero and global
    concurrency is available.  ``per_session_limit`` counts both queued and
    running jobs, while ``max_concurrent_per_session`` limits only jobs that
    are running at the same time for one session.
    """

    def __init__(
        self,
        *,
        max_concurrent: int = 2,
        max_concurrent_per_session: int = 1,
        max_queued: int = 20,
        per_session_limit: int = 2,
        history_ttl: float = 3600.0,
        history_limit: int = 100,
    ) -> None:
        self._require_int("max_concurrent", max_concurrent, minimum=1)
        self._require_int(
            "max_concurrent_per_session",
            max_concurrent_per_session,
            minimum=1,
        )
        self._require_int("max_queued", max_queued, minimum=0)
        self._require_int("per_session_limit", per_session_limit, minimum=1)
        self._require_int("history_limit", history_limit, minimum=0)
        if isinstance(history_ttl, bool) or not isinstance(history_ttl, (int, float)):
            raise TypeError("history_ttl must be a number")
        if history_ttl < 0:
            raise ValueError("history_ttl must be >= 0")

        self.max_concurrent = max_concurrent
        self.max_concurrent_per_session = max_concurrent_per_session
        self.max_queued = max_queued
        self.per_session_limit = per_session_limit
        self.history_ttl = float(history_ttl)
        self.history_limit = history_limit

        self._lock = asyncio.Lock()
        self._jobs: Dict[str, _JobRecord] = {}
        self._pending: Deque[str] = deque()
        self._finished: Deque[str] = deque()
        self._active_by_session: Counter[str] = Counter()
        self._running_by_session: Counter[str] = Counter()
        self._runner_tasks: set[asyncio.Task] = set()
        self._running_count = 0
        self._queued_count = 0
        self._closed = False

    @staticmethod
    def _require_int(name: str, value: int, *, minimum: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} must be an integer")
        if value < minimum:
            raise ValueError(f"{name} must be >= {minimum}")

    @staticmethod
    def _validate_session_key(session_key: str) -> None:
        if not isinstance(session_key, str):
            raise TypeError("session_key must be a string")
        if not session_key:
            raise ValueError("session_key must not be empty")

    @property
    def closed(self) -> bool:
        """Whether the manager has begun permanent shutdown."""

        return self._closed

    async def submit(
        self,
        factory: CoroutineFactory,
        session_key: str,
        label: str = "",
        cleanup: Optional[CleanupCallback] = None,
    ) -> str:
        """Submit work and return its opaque job id.

        ``factory`` is called only after a global running slot is assigned.
        It must return an awaitable.  Queue and session limit failures leave
        the factory and ``cleanup`` untouched.

        Once submission succeeds, the manager owns ``cleanup`` and invokes it
        exactly once when the job reaches any terminal state.  The callback is
        synchronous and its return value is ignored.  It must be fast and
        non-blocking because it may run while the manager lock is held.
        """

        if not callable(factory):
            raise TypeError("factory must be callable")
        self._validate_session_key(session_key)
        if not isinstance(label, str):
            raise TypeError("label must be a string")
        if cleanup is not None and not callable(cleanup):
            raise TypeError("cleanup must be callable or None")

        async with self._lock:
            self._prune_finished_locked(time.monotonic())
            if self._closed:
                raise JobManagerClosedError("job manager is shut down")
            if self._active_by_session[session_key] >= self.per_session_limit:
                raise SessionLimitError(
                    f"session {session_key!r} already has "
                    f"{self.per_session_limit} active job(s)"
                )

            starts_now = (
                self._running_count < self.max_concurrent
                and self._running_by_session[session_key]
                < self.max_concurrent_per_session
            )
            if not starts_now and self._queued_count >= self.max_queued:
                raise QueueFullError(
                    f"job queue is full ({self.max_queued} waiting job(s))"
                )

            job_id = uuid.uuid4().hex[:12]
            record = _JobRecord(
                job_id=job_id,
                session_key=session_key,
                label=label,
                factory=factory,
                cleanup=cleanup,
                status="running" if starts_now else "queued",
                created_at=time.time(),
            )
            self._jobs[job_id] = record
            self._active_by_session[session_key] += 1

            if starts_now:
                self._launch_locked(record)
            else:
                self._pending.append(job_id)
                self._queued_count += 1
            return job_id

    async def status(self, session_key: str) -> List[JobInfo]:
        """Return oldest-first snapshots for one session.

        The result includes active jobs and retained terminal history.  Queue
        positions are global, one-based positions and are present only for
        jobs whose status is ``queued``.
        """

        self._validate_session_key(session_key)
        async with self._lock:
            self._prune_finished_locked(time.monotonic())
            positions = {
                job_id: index
                for index, job_id in enumerate(self._pending, start=1)
            }
            return [
                JobInfo(
                    job_id=record.job_id,
                    session_key=record.session_key,
                    label=record.label,
                    status=record.status,
                    created_at=record.created_at,
                    started_at=record.started_at,
                    finished_at=record.finished_at,
                    error=record.error,
                    queue_position=positions.get(record.job_id),
                    cancel_requested=record.cancel_requested,
                )
                for record in self._jobs.values()
                if record.session_key == session_key
            ]

    async def cancel(
        self,
        session_key: str,
        job_id: Optional[str] = None,
    ) -> List[str]:
        """Cancel one job, or all active jobs owned by ``session_key``.

        Returns the ids for which a new cancellation request was accepted.
        Queued jobs become ``cancelled`` immediately.  Running jobs are sent
        ``Task.cancel()`` and reach ``cancelled`` when their coroutine exits.
        A job id belonging to another session is deliberately ignored.
        """

        self._validate_session_key(session_key)
        if job_id is not None and not isinstance(job_id, str):
            raise TypeError("job_id must be a string or None")

        async with self._lock:
            self._prune_finished_locked(time.monotonic())
            candidates = [
                record
                for record in self._jobs.values()
                if record.session_key == session_key
                and record.status in ("queued", "running")
                and (job_id is None or record.job_id == job_id)
                and not record.cancel_requested
            ]
            if not candidates:
                return []

            accepted = [record.job_id for record in candidates]
            queued_ids = {
                record.job_id for record in candidates if record.status == "queued"
            }
            if queued_ids:
                self._pending = deque(
                    pending_id
                    for pending_id in self._pending
                    if pending_id not in queued_ids
                )
                self._queued_count = len(self._pending)

            now_wall = time.time()
            now_mono = time.monotonic()
            current_task = asyncio.current_task()
            for record in candidates:
                record.cancel_requested = True
                if record.status == "queued":
                    self._finish_queued_cancel_locked(record, now_wall, now_mono)
                    continue

                task = record.task
                if (
                    task is not None
                    and task is not current_task
                    and record.runner_started
                    and not record.finalizing
                ):
                    task.cancel()

            self._prune_finished_locked(now_mono)
            return accepted

    async def shutdown(self) -> None:
        """Reject new work, cancel queued/running jobs, and await runners.

        Shutdown is idempotent.  A factory that suppresses cancellation can
        delay this method; callers that need a hard deadline may wrap it with
        :func:`asyncio.wait_for`.
        """

        current_task = asyncio.current_task()
        async with self._lock:
            self._closed = True
            now_wall = time.time()
            now_mono = time.monotonic()

            queued_ids = list(self._pending)
            self._pending.clear()
            self._queued_count = 0
            for queued_id in queued_ids:
                record = self._jobs.get(queued_id)
                if record is None or record.status != "queued":
                    continue
                record.cancel_requested = True
                self._finish_queued_cancel_locked(record, now_wall, now_mono)

            for record in self._jobs.values():
                if record.status != "running":
                    continue
                record.cancel_requested = True
                task = record.task
                if (
                    task is not None
                    and task is not current_task
                    and record.runner_started
                    and not record.finalizing
                ):
                    task.cancel()

            runners = [
                task for task in self._runner_tasks if task is not current_task
            ]
            self._prune_finished_locked(now_mono)

        if runners:
            await asyncio.gather(*runners, return_exceptions=True)

        async with self._lock:
            self._prune_finished_locked(time.monotonic())

    async def __aenter__(self) -> "JobManager":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.shutdown()

    def _launch_locked(self, record: _JobRecord) -> None:
        record.status = "running"
        record.started_at = time.time()
        self._running_count += 1
        self._running_by_session[record.session_key] += 1
        task = asyncio.create_task(
            self._run_job(record.job_id),
            name=f"ai-draw-job-{record.job_id}",
        )
        record.task = task
        self._runner_tasks.add(task)
        task.add_done_callback(self._consume_runner_result)

    def _consume_runner_result(self, task: asyncio.Task) -> None:
        self._runner_tasks.discard(task)
        try:
            task.exception()
        except asyncio.CancelledError:
            pass
        except BaseException:
            # The runner normally converts errors into job state.  This final
            # guard prevents an unexpected manager failure from producing an
            # unhandled-task warning during interpreter shutdown.
            pass

    async def _run_job(self, job_id: str) -> None:
        record = self._jobs.get(job_id)
        if record is None:
            return
        record.runner_started = True

        outcome: JobState = "completed"
        error: Optional[str] = None
        if record.cancel_requested:
            outcome = "cancelled"
            record.factory = None
        else:
            factory = record.factory
            record.factory = None
            try:
                if factory is None:
                    raise RuntimeError("job coroutine factory is missing")
                awaitable = factory()
                if not inspect.isawaitable(awaitable):
                    raise TypeError("job factory must return an awaitable")
                await awaitable
            except asyncio.CancelledError:
                outcome = "cancelled"
            except BaseException as exc:
                outcome = "failed"
                error = self._format_error(exc)

        # From this point cancellation requests must not cancel the runner:
        # doing so while it waits for the lock could leak a running slot.
        record = self._jobs.get(job_id)
        if record is not None:
            record.finalizing = True
        await self._finish_running(job_id, outcome, error)

    async def _finish_running(
        self,
        job_id: str,
        outcome: JobState,
        error: Optional[str],
    ) -> None:
        async with self._lock:
            record = self._jobs.get(job_id)
            if record is None or record.status != "running":
                return
            if record.cancel_requested:
                outcome = "cancelled"
                error = None

            record.status = outcome
            record.error = error
            record.finished_at = time.time()
            record.finished_monotonic = time.monotonic()
            record.factory = None
            record.task = None
            record.finalizing = False
            self._run_cleanup_locked(record)
            self._running_count -= 1
            self._decrement_running_session_locked(record.session_key)
            self._decrement_session_locked(record.session_key)
            self._finished.append(job_id)

            if not self._closed:
                self._pump_locked()
            self._prune_finished_locked(time.monotonic())

    def _finish_queued_cancel_locked(
        self,
        record: _JobRecord,
        now_wall: float,
        now_mono: float,
    ) -> None:
        record.status = "cancelled"
        record.finished_at = now_wall
        record.finished_monotonic = now_mono
        record.factory = None
        record.error = None
        self._run_cleanup_locked(record)
        self._decrement_session_locked(record.session_key)
        self._finished.append(record.job_id)

    @staticmethod
    def _run_cleanup_locked(record: _JobRecord) -> None:
        """Invoke and release a job cleanup callback without leaking errors."""

        cleanup = record.cleanup
        record.cleanup = None
        if cleanup is None:
            return
        try:
            cleanup()
        except BaseException:
            # Cleanup is best-effort and must never strand queue bookkeeping or
            # prevent another job from being scheduled.
            pass

    def _pump_locked(self) -> None:
        while self._running_count < self.max_concurrent:
            record = self._take_next_runnable_locked()
            if record is None:
                break
            self._launch_locked(record)

    def _take_next_runnable_locked(self) -> Optional[_JobRecord]:
        """Remove and return the oldest pending job that can run now.

        A blocked job stays in its original queue position.  Scanning past it
        prevents one session at its running limit from idling global slots or
        blocking runnable jobs owned by other sessions.
        """

        for job_id in list(self._pending):
            record = self._jobs.get(job_id)
            if record is None or record.status != "queued":
                self._pending.remove(job_id)
                self._queued_count -= 1
                continue
            if (
                self._running_by_session[record.session_key]
                >= self.max_concurrent_per_session
            ):
                continue
            self._pending.remove(job_id)
            self._queued_count -= 1
            return record
        return None

    def _decrement_session_locked(self, session_key: str) -> None:
        remaining = self._active_by_session[session_key] - 1
        if remaining > 0:
            self._active_by_session[session_key] = remaining
        else:
            self._active_by_session.pop(session_key, None)

    def _decrement_running_session_locked(self, session_key: str) -> None:
        remaining = self._running_by_session[session_key] - 1
        if remaining > 0:
            self._running_by_session[session_key] = remaining
        else:
            self._running_by_session.pop(session_key, None)

    def _prune_finished_locked(self, now_monotonic: float) -> None:
        while self._finished:
            job_id = self._finished[0]
            record = self._jobs.get(job_id)
            if record is None:
                self._finished.popleft()
                continue
            finished_at = record.finished_monotonic
            expired = (
                finished_at is not None
                and now_monotonic - finished_at >= self.history_ttl
            )
            over_limit = len(self._finished) > self.history_limit
            if not expired and not over_limit:
                break
            self._finished.popleft()
            self._jobs.pop(job_id, None)

    @staticmethod
    def _format_error(exc: BaseException) -> str:
        text = str(exc).strip()
        message = type(exc).__name__ if not text else f"{type(exc).__name__}: {text}"
        return message[:1000]


__all__ = [
    "CleanupCallback",
    "CoroutineFactory",
    "JobInfo",
    "JobManager",
    "JobManagerClosedError",
    "JobManagerError",
    "JobState",
    "QueueFullError",
    "SessionLimitError",
]
