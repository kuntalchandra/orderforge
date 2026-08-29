"""Phase 2 worker: single-threaded queue draining with uniform retry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .exceptions import RetryExhaustedError
from .interfaces import ArtifactRepository, GenerationService, OrderQueue, ResultPublisher
from .models import Order
from .orchestrator import PollConfig, process_order
from .retry import RetryingProxy, RetryConfig


@dataclass(frozen=True)
class UnresolvedOrder:
    """An order whose processing could not reach a known terminal outcome."""

    order: Order
    operation: str
    reason: str


class UnresolvedOrderRegistry:
    """Inspectable in-memory store for orders left unresolved by retry exhaustion."""

    def __init__(self) -> None:
        self._orders: List[UnresolvedOrder] = []

    def record(self, order: Order, error: RetryExhaustedError) -> None:
        self._orders.append(
            UnresolvedOrder(
                order=order,
                operation=error.operation,
                reason=str(error),
            )
        )

    @property
    def orders(self) -> List[UnresolvedOrder]:
        return list(self._orders)

    def __len__(self) -> int:
        return len(self._orders)


def run(
    queue: OrderQueue,
    asset_generation_service: GenerationService,
    metadata_generation_service: GenerationService,
    repository: ArtifactRepository,
    publisher: ResultPublisher,
    unresolved: UnresolvedOrderRegistry,
    poll: PollConfig = PollConfig(),
    retry: RetryConfig = RetryConfig(),
) -> int:
    """Drain the queue and return the number of orders taken from it.

    Retry exhaustion after an order has been taken is recorded as unresolved,
    and processing continues with the next order. The registry is a required
    dependency so unresolved state cannot disappear when this function returns.

    If queue.take() itself exhausts retries, no order identity is available to
    record, so RetryExhaustedError propagates to the caller.
    """
    queue = RetryingProxy(queue, config=retry, dependency_name="order_queue")
    asset_generation_service = RetryingProxy(
        asset_generation_service,
        config=retry,
        dependency_name="asset_generation",
    )
    metadata_generation_service = RetryingProxy(
        metadata_generation_service,
        config=retry,
        dependency_name="metadata_generation",
    )
    repository = RetryingProxy(
        repository,
        config=retry,
        dependency_name="artifact_repository",
    )
    publisher = RetryingProxy(
        publisher,
        config=retry,
        dependency_name="result_publisher",
    )

    taken_count = 0
    while True:
        order = queue.take()
        if order is None:
            break

        taken_count += 1
        try:
            process_order(
                order,
                asset_generation_service,
                metadata_generation_service,
                repository,
                publisher,
                poll,
            )
        except RetryExhaustedError as exc:
            unresolved.record(order, exc)

    return taken_count
