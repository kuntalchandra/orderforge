"""Stable idempotency-key composition for mutating downstream operations."""

from __future__ import annotations

from .interfaces import GenerationService, ResultPublisher
from .models import FailedOrder, JobStatus, Order, ShippableOrder


def generation_key(stage: str, order_id: str) -> str:
    return f"generation:{stage}:{order_id}"


def result_key(order_id: str) -> str:
    return f"result:{order_id}"


class IdempotentGenerationService(GenerationService):
    """Attach a stable stage+order key to every generation trigger."""

    def __init__(self, target: GenerationService, stage: str) -> None:
        self._target = target
        self._stage = stage

    def queue(self, order: Order, idempotency_key: str | None = None) -> str:
        key = idempotency_key or generation_key(self._stage, order.order_id)
        return self._target.queue(order, idempotency_key=key)

    def get_status(self, job_id: str) -> JobStatus:
        return self._target.get_status(job_id)


class IdempotentResultPublisher(ResultPublisher):
    """Attach a stable order key to every terminal result submission."""

    def __init__(self, target: ResultPublisher) -> None:
        self._target = target

    def submit_shippable(
        self,
        order: ShippableOrder,
        idempotency_key: str | None = None,
    ) -> None:
        key = idempotency_key or result_key(order.order_id)
        self._target.submit_shippable(order, idempotency_key=key)

    def submit_failed(
        self,
        order: FailedOrder,
        idempotency_key: str | None = None,
    ) -> None:
        key = idempotency_key or result_key(order.order_id)
        self._target.submit_failed(order, idempotency_key=key)
