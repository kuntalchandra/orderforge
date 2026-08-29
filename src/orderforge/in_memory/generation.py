"""Thread-safe in-memory GenerationService.

queue() protects the idempotency check and job creation as one atomic
operation.

get_status() also protects lookup and polls_seen mutation. Different worker
threads can process duplicate queue entries representing the same logical
order. Idempotency deliberately maps those calls to the same job_id, so the
job record can be shared even though each physical queue entry is owned by
one worker.
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass
from typing import Callable, Dict

from ..exceptions import IdempotencyConflictError, UnknownJobError
from ..interfaces import GenerationService
from ..models import JobStatus, Order


@dataclass
class _JobRecord:
    order: Order
    polls_seen: int = 0


@dataclass(frozen=True)
class _IdempotentGenerationRecord:
    order: Order
    job_id: str


class InMemoryGenerationService(GenerationService):
    def __init__(
        self,
        should_fail: Callable[[Order], bool] = lambda order: False,
        pending_polls_before_terminal: int = 1,
    ):
        self._should_fail = should_fail
        self._pending_polls_before_terminal = pending_polls_before_terminal
        self._jobs: Dict[str, _JobRecord] = {}
        self._idempotency_records: Dict[str, _IdempotentGenerationRecord] = {}
        self._id_counter = itertools.count(1)
        self._lock = threading.Lock()

    def queue(
        self,
        order: Order,
        idempotency_key: str | None = None,
    ) -> str:
        with self._lock:
            if idempotency_key is not None:
                existing = self._idempotency_records.get(idempotency_key)

                if existing is not None:
                    if existing.order != order:
                        raise IdempotencyConflictError(
                            f"generation key {idempotency_key!r} "
                            "reused for a different order"
                        )

                    return existing.job_id

            job_id = f"job-{next(self._id_counter)}"
            self._jobs[job_id] = _JobRecord(order=order)

            if idempotency_key is not None:
                self._idempotency_records[idempotency_key] = (
                    _IdempotentGenerationRecord(
                        order=order,
                        job_id=job_id,
                    )
                )

            return job_id

    @property
    def job_count(self) -> int:
        with self._lock:
            return len(self._jobs)

    def get_status(self, job_id: str) -> JobStatus:
        with self._lock:
            try:
                job = self._jobs[job_id]
            except KeyError:
                raise UnknownJobError(f"no such job: {job_id}")

            job.polls_seen += 1
            polls_seen = job.polls_seen
            order = job.order

        if polls_seen <= self._pending_polls_before_terminal:
            return JobStatus.PENDING

        # Do not hold the shared-state lock while invoking caller-supplied
        # behaviour. The state required for this decision was copied above.
        return (
            JobStatus.FAILED
            if self._should_fail(order)
            else JobStatus.SUCCESS
        )
