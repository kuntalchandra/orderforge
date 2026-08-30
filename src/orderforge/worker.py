"""Phase 4 concurrent worker pool.

Concurrency is protected at shared-state boundaries rather than by locking
the whole order-processing workflow.

A worker owns one physical queue entry until that entry reaches a terminal
submission or is recorded as unresolved. Duplicate queue entries can still
represent the same logical order, so downstream components independently
protect shared logical state such as idempotent jobs and terminal results.

If taking from the queue itself exhausts retries, the failure is not scoped
to an individual order. A shared stop event asks the other workers to wind
down, while the first queue-level error is re-raised by run().
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import List

from .cache import TTLCache
from .caching import CachingArtifactRepository

from .circuit_breaker import (
    CircuitBreakerConfig,
    CircuitBreakingProxy,
    build_operation_breakers,
)
from .exceptions import CircuitOpenError, RetryExhaustedError
from .idempotency import (
    IdempotentGenerationService,
    IdempotentResultPublisher,
)
from .interfaces import (
    ArtifactRepository,
    GenerationService,
    OrderQueue,
    ResultPublisher,
)
from .models import Order
from .orchestrator import PollConfig, process_order
from .retry import RetryingProxy, RetryConfig


DEFAULT_WORKER_POOL_SIZE = 150

@dataclass(frozen=True)
class UnresolvedOrder:
    """An order whose processing could not reach a known terminal outcome."""

    order: Order
    operation: str
    reason: str


class UnresolvedOrderRegistry:
    """Thread-safe in-memory store for unresolved orders."""

    def __init__(self) -> None:
        self._orders: List[UnresolvedOrder] = []
        self._lock = threading.Lock()

    def record(
        self,
        order: Order,
        error: BaseException,
    ) -> None:
        unresolved = UnresolvedOrder(
            order=order,
            operation=getattr(
                error,
                "operation",
                "circuit_breaker",
            ),
            reason=str(error),
        )

        with self._lock:
            self._orders.append(unresolved)

    @property
    def orders(self) -> List[UnresolvedOrder]:
        with self._lock:
            return list(self._orders)

    def __len__(self) -> int:
        with self._lock:
            return len(self._orders)


def _worker_loop(
    queue: OrderQueue,
    asset_generation_service: GenerationService,
    metadata_generation_service: GenerationService,
    repository: ArtifactRepository,
    publisher: ResultPublisher,
    unresolved: UnresolvedOrderRegistry,
    poll: PollConfig,
    stop_event: threading.Event,
    first_error_box: List[BaseException],
    first_error_lock: threading.Lock,
) -> int:
    taken_count = 0

    while not stop_event.is_set():
        try:
            order = queue.take()
        except (
            RetryExhaustedError,
            CircuitOpenError,
        ) as exc:
            with first_error_lock:
                if not first_error_box:
                    first_error_box.append(exc)

            stop_event.set()
            break

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
        except (
            RetryExhaustedError,
            CircuitOpenError,
        ) as exc:
            unresolved.record(order, exc)

    return taken_count


def run(
    queue: OrderQueue,
    asset_generation_service: GenerationService,
    metadata_generation_service: GenerationService,
    repository: ArtifactRepository,
    publisher: ResultPublisher,
    unresolved: UnresolvedOrderRegistry,
    poll: PollConfig = PollConfig(),
    retry: RetryConfig = RetryConfig(),
    num_workers: int = DEFAULT_WORKER_POOL_SIZE,
    circuit_breaker: CircuitBreakerConfig | None = None,
) -> int:
    """Drain the queue concurrently and return total orders taken."""

    if num_workers < 1:
        raise ValueError("num_workers must be >= 1")

    breaker_config = (
        circuit_breaker
        if circuit_breaker is not None
        else CircuitBreakerConfig()
    )

    queue = CircuitBreakingProxy(
        RetryingProxy(
            queue,
            config=retry,
            dependency_name="order_queue",
        ),
        dependency_name="order_queue",
        breakers=build_operation_breakers(
            ["take"],
            breaker_config,
        ),
    )

    asset_generation_service = CircuitBreakingProxy(
        RetryingProxy(
            IdempotentGenerationService(
                asset_generation_service,
                stage="asset",
            ),
            config=retry,
            dependency_name="asset_generation",
        ),
        dependency_name="asset_generation",
        breakers=build_operation_breakers(
            [
                "queue",
                "get_status",
            ],
            breaker_config,
        ),
    )

    metadata_generation_service = CircuitBreakingProxy(
        RetryingProxy(
            IdempotentGenerationService(
                metadata_generation_service,
                stage="metadata",
            ),
            config=retry,
            dependency_name="metadata_generation",
        ),
        dependency_name="metadata_generation",
        breakers=build_operation_breakers(
            [
                "queue",
                "get_status",
            ],
            breaker_config,
        ),
    )

    repository = CachingArtifactRepository(
        CircuitBreakingProxy(
            RetryingProxy(
                repository,
                config=retry,
                dependency_name="artifact_repository",
            ),
            dependency_name="artifact_repository",
            breakers=build_operation_breakers(
                [
                    "get_assets",
                    "get_asset_detail",
                    "get_metadata",
                ],
                breaker_config,
            ),
        ),
        TTLCache(),
    )

    publisher = CircuitBreakingProxy(
        RetryingProxy(
            IdempotentResultPublisher(publisher),
            config=retry,
            dependency_name="result_publisher",
        ),
        dependency_name="result_publisher",
        breakers=build_operation_breakers(
            [
                "submit_shippable",
                "submit_failed",
            ],
            breaker_config,
        ),
    )

    stop_event = threading.Event()
    first_error_box: List[BaseException] = []
    first_error_lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=num_workers) as pool:
        futures = [
            pool.submit(
                _worker_loop,
                queue,
                asset_generation_service,
                metadata_generation_service,
                repository,
                publisher,
                unresolved,
                poll,
                stop_event,
                first_error_box,
                first_error_lock,
            )
            for _ in range(num_workers)
        ]

        taken_count = sum(future.result() for future in futures)

    if first_error_box:
        raise first_error_box[0]

    return taken_count
