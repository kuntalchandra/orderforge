"""
In-memory GenerationService.

Design note: jobs resolve deterministically after a configurable number of
poll calls (`pending_polls_before_terminal`), rather than via a background
thread + wall-clock delay. This still genuinely exercises the
orchestrator's poll-until-terminal loop (get_status returns PENDING some
number of times before a terminal status), without introducing any
threading — Phase 1 is single-threaded end to end. Realistic wall-clock
async simulation, if wanted, is a Phase 4+ concern once the worker pool
itself is concurrent; adding it here would pull concurrency handling
forward for no correctness benefit.

One instance of this class is used per generation kind (asset, metadata) —
each gets its own `should_fail` predicate and its own job state.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Callable, Dict

from ..exceptions import UnknownJobError
from ..interfaces import GenerationService
from ..models import JobStatus, Order


@dataclass
class _JobRecord:
    order: Order
    polls_seen: int = 0


class InMemoryGenerationService(GenerationService):
    def __init__(
        self,
        should_fail: Callable[[Order], bool] = lambda order: False,
        pending_polls_before_terminal: int = 1,
    ):
        self._should_fail = should_fail
        self._pending_polls_before_terminal = pending_polls_before_terminal
        self._jobs: Dict[str, _JobRecord] = {}
        self._id_counter = itertools.count(1)

    def queue(self, order: Order) -> str:
        job_id = f"job-{next(self._id_counter)}"
        self._jobs[job_id] = _JobRecord(order=order)
        return job_id

    def get_status(self, job_id: str) -> JobStatus:
        try:
            job = self._jobs[job_id]
        except KeyError:
            raise UnknownJobError(f"no such job: {job_id}")

        job.polls_seen += 1
        if job.polls_seen <= self._pending_polls_before_terminal:
            return JobStatus.PENDING
        return JobStatus.FAILED if self._should_fail(job.order) else JobStatus.SUCCESS