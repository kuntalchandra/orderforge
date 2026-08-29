"""Phase 4 concurrency contracts."""

from __future__ import annotations

import inspect
import threading

from orderforge.idempotency import (
    IdempotentGenerationService,
    IdempotentResultPublisher,
)
from orderforge.in_memory.generation import InMemoryGenerationService
from orderforge.in_memory.publisher import InMemoryResultPublisher
from orderforge.in_memory.queue import InMemoryOrderQueue
from orderforge.in_memory.repository import InMemoryArtifactRepository
from orderforge.models import FailedOrder, FailureStage, Order
from orderforge.retry import RetryingProxy, RetryConfig
from orderforge.worker import (
    DEFAULT_WORKER_POOL_SIZE,
    UnresolvedOrderRegistry,
    run,
)


class _CoordinatedGetDict(dict):
    """Expose a check-then-act race deterministically in tests.

    get() captures the value before waiting at the barrier.

    Without a surrounding production lock, every racing thread can
    therefore capture the same missing value before any of them mutates
    the dict.

    With the production lock, the first caller times out at the barrier
    while holding the lock, completes the mutation, and later callers see
    the committed record.
    """

    def __init__(self, parties: int):
        super().__init__()
        self._barrier = threading.Barrier(parties)

    def get(self, key, default=None):
        value = super().get(key, default)

        try:
            self._barrier.wait(timeout=0.05)
        except threading.BrokenBarrierError:
            pass

        return value


class _CoordinatedCounter(int):
    """Force two unlocked read-modify-write operations to share a snapshot."""

    def __new__(cls, value: int, barrier: threading.Barrier):
        instance = super().__new__(cls, value)
        instance._barrier = barrier
        return instance

    def __add__(self, other):
        value = int(self) + other

        try:
            self._barrier.wait(timeout=0.05)
        except threading.BrokenBarrierError:
            pass

        return value


def _run_concurrently(num_threads: int, fn) -> None:
    barrier = threading.Barrier(num_threads)

    def wrapped():
        barrier.wait()
        fn()

    threads = [
        threading.Thread(target=wrapped)
        for _ in range(num_threads)
    ]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()


def test_concurrent_take_never_double_serves_or_loses_an_order():
    n = 200
    orders = [Order(order_id=f"o{i}") for i in range(n)]
    queue = InMemoryOrderQueue(orders)

    taken: list[Order] = []
    taken_lock = threading.Lock()

    def worker():
        while True:
            order = queue.take()

            if order is None:
                return

            with taken_lock:
                taken.append(order)

    _run_concurrently(20, worker)

    assert len(taken) == n
    assert len({order.order_id for order in taken}) == n
    assert len(queue) == 0


def test_generation_idempotency_check_and_create_is_atomic():
    num_threads = 16

    target = InMemoryGenerationService()
    target._idempotency_records = _CoordinatedGetDict(num_threads)

    service = IdempotentGenerationService(
        target,
        stage="asset",
    )
    order = Order(order_id="o1")

    results: list[str] = []
    results_lock = threading.Lock()

    def call():
        job_id = service.queue(order)

        with results_lock:
            results.append(job_id)

    _run_concurrently(num_threads, call)

    assert target.job_count == 1
    assert len(set(results)) == 1


def test_shared_job_poll_counter_is_atomic():
    target = InMemoryGenerationService(
        pending_polls_before_terminal=100,
    )

    job_id = target.queue(
        Order(order_id="o1"),
        idempotency_key="generation:asset:o1",
    )

    barrier = threading.Barrier(2)
    target._jobs[job_id].polls_seen = _CoordinatedCounter(
        0,
        barrier,
    )

    _run_concurrently(
        2,
        lambda: target.get_status(job_id),
    )

    assert target._jobs[job_id].polls_seen == 2


def test_result_idempotency_check_append_and_record_is_atomic():
    num_threads = 16

    target = InMemoryResultPublisher()
    target._idempotency_records = _CoordinatedGetDict(num_threads)

    publisher = IdempotentResultPublisher(target)

    order = Order(order_id="o1")
    failed = FailedOrder(
        order_id="o1",
        order=order,
        stage=FailureStage.ASSET_GENERATION,
        reason="asset generation failed for order o1",
    )

    _run_concurrently(
        num_threads,
        lambda: publisher.submit_failed(failed),
    )

    assert len(target.failed_orders) == 1


def test_unresolved_registry_records_every_concurrent_entry():
    from orderforge.exceptions import RetryExhaustedError, TransientError

    registry = UnresolvedOrderRegistry()
    n = 100
    barrier = threading.Barrier(n)

    def record(index):
        barrier.wait()

        registry.record(
            Order(order_id=f"o{index}"),
            RetryExhaustedError(
                "op",
                3,
                TransientError("down"),
            ),
        )

    threads = [
        threading.Thread(target=record, args=(index,))
        for index in range(n)
    ]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()

    assert len(registry) == n
    assert len(
        {entry.order.order_id for entry in registry.orders}
    ) == n


def test_worker_pool_processes_every_order_once_with_mixed_outcomes():
    n = 60
    orders = [Order(order_id=f"o{i}") for i in range(n)]
    queue = InMemoryOrderQueue(orders)

    def asset_should_fail(order: Order) -> bool:
        return int(order.order_id[1:]) % 5 == 0

    def metadata_should_fail(order: Order) -> bool:
        index = int(order.order_id[1:])
        return index % 5 != 0 and index % 7 == 0

    asset_service = InMemoryGenerationService(
        should_fail=asset_should_fail,
    )
    metadata_service = InMemoryGenerationService(
        should_fail=metadata_should_fail,
    )
    repository = InMemoryArtifactRepository()
    publisher = InMemoryResultPublisher()
    unresolved = UnresolvedOrderRegistry()

    taken_count = run(
        queue,
        asset_service,
        metadata_service,
        repository,
        publisher,
        unresolved,
        retry=RetryConfig(
            max_attempts=1,
            jitter_ratio=0.0,
        ),
        num_workers=10,
    )

    expected_asset_failures = sum(
        1
        for order in orders
        if asset_should_fail(order)
    )
    expected_metadata_failures = sum(
        1
        for order in orders
        if metadata_should_fail(order)
    )

    expected_shippable = (
        n
        - expected_asset_failures
        - expected_metadata_failures
    )

    assert taken_count == n
    assert len(queue) == 0
    assert len(publisher.shippable_orders) == expected_shippable
    assert len(publisher.failed_orders) == (
        expected_asset_failures
        + expected_metadata_failures
    )

    seen = [
        order.order_id
        for order in publisher.shippable_orders
    ] + [
        order.order_id
        for order in publisher.failed_orders
    ]

    assert len(seen) == n
    assert len(set(seen)) == n


def test_duplicate_logical_orders_still_publish_once():
    queue = InMemoryOrderQueue(
        [
            Order(order_id="same-order")
            for _ in range(50)
        ]
    )

    publisher = InMemoryResultPublisher()

    taken_count = run(
        queue,
        InMemoryGenerationService(),
        InMemoryGenerationService(),
        InMemoryArtifactRepository(),
        publisher,
        UnresolvedOrderRegistry(),
        retry=RetryConfig(
            max_attempts=1,
            jitter_ratio=0.0,
        ),
        num_workers=20,
    )

    assert taken_count == 50

    terminal_count = (
        len(publisher.shippable_orders)
        + len(publisher.failed_orders)
    )

    assert terminal_count == 1


def test_retry_jitter_is_enabled_by_default():
    assert RetryConfig().jitter_ratio > 0.0


def test_jitter_never_exceeds_max_delay():
    proxy = RetryingProxy(
        object(),
        config=RetryConfig(
            initial_delay_seconds=2.0,
            max_delay_seconds=2.0,
            jitter_ratio=0.5,
        ),
        rand=lambda: 1.0,
    )

    assert proxy._delay_for_attempt(1) == 2.0


def test_run_uses_declared_worker_pool_default():
    assert (
        inspect.signature(run).parameters["num_workers"].default
        == DEFAULT_WORKER_POOL_SIZE
        == 150
    )


def test_run_rejects_invalid_worker_count():
    try:
        run(
            InMemoryOrderQueue(),
            InMemoryGenerationService(),
            InMemoryGenerationService(),
            InMemoryArtifactRepository(),
            InMemoryResultPublisher(),
            UnresolvedOrderRegistry(),
            num_workers=0,
        )
    except ValueError as exc:
        assert str(exc) == "num_workers must be >= 1"
    else:
        raise AssertionError("expected ValueError")
