import pytest

from orderforge.exceptions import RetryExhaustedError, TransientError
from orderforge.in_memory.generation import InMemoryGenerationService
from orderforge.in_memory.publisher import InMemoryResultPublisher
from orderforge.in_memory.queue import InMemoryOrderQueue
from orderforge.in_memory.repository import InMemoryArtifactRepository
from orderforge.models import Order
from orderforge.retry import RetryingProxy, RetryConfig
from orderforge.worker import UnresolvedOrderRegistry, run


def test_retries_transient_failure_with_exponential_backoff():
    calls = []
    sleeps = []

    class Flaky:
        def execute(self):
            calls.append(1)
            if len(calls) < 3:
                raise TransientError("temporary outage")
            return "ok"

    proxy = RetryingProxy(
        Flaky(),
        RetryConfig(
            max_attempts=3,
            initial_delay_seconds=0.5,
            backoff_multiplier=2.0,
            max_delay_seconds=10.0,
            jitter_ratio=0.0,
        ),
        sleeper=sleeps.append,
        dependency_name="dependency",
    )

    assert proxy.execute() == "ok"
    assert len(calls) == 3
    assert sleeps == [0.5, 1.0]


def test_retry_delay_is_capped():
    calls = 0
    sleeps = []

    class Flaky:
        def execute(self):
            nonlocal calls
            calls += 1
            if calls < 4:
                raise TransientError("temporary outage")
            return "ok"

    proxy = RetryingProxy(
        Flaky(),
        RetryConfig(
            max_attempts=4,
            initial_delay_seconds=1,
            backoff_multiplier=2,
            max_delay_seconds=2,
            jitter_ratio=0.0,
        ),
        sleeper=sleeps.append,
    )

    assert proxy.execute() == "ok"
    assert sleeps == [1, 2, 2]


def test_retry_exhaustion_raises_specific_error_with_original_cause():
    calls = []

    class AlwaysFlaky:
        def execute(self):
            calls.append(1)
            raise TransientError("still down")

    proxy = RetryingProxy(
        AlwaysFlaky(),
        RetryConfig(max_attempts=3, initial_delay_seconds=0),
        sleeper=lambda _: None,
        dependency_name="asset_generation",
    )

    with pytest.raises(RetryExhaustedError) as exc_info:
        proxy.execute()

    assert len(calls) == 3
    assert exc_info.value.operation == "asset_generation.execute"
    assert exc_info.value.attempts == 3
    assert isinstance(exc_info.value.cause, TransientError)


def test_non_transient_exception_is_not_retried():
    calls = []

    class Broken:
        def execute(self):
            calls.append(1)
            raise RuntimeError("bad request")

    proxy = RetryingProxy(
        Broken(),
        RetryConfig(max_attempts=3),
        sleeper=lambda _: None,
    )

    with pytest.raises(RuntimeError):
        proxy.execute()

    assert calls == [1]


def test_unresolved_registry_is_inspectable():
    registry = UnresolvedOrderRegistry()
    order = Order(order_id="o1")
    error = RetryExhaustedError(
        "asset_generation.queue",
        3,
        TransientError("dependency unavailable"),
    )

    registry.record(order, error)

    assert len(registry) == 1
    assert registry.orders[0].order == order
    assert registry.orders[0].operation == "asset_generation.queue"
    assert "dependency unavailable" in registry.orders[0].reason


def test_queue_retry_exhaustion_propagates_when_no_order_is_known():
    class AlwaysFailingQueue(InMemoryOrderQueue):
        def take(self):
            raise TransientError("queue unavailable")

    with pytest.raises(RetryExhaustedError) as exc_info:
        run(
            AlwaysFailingQueue(),
            InMemoryGenerationService(),
            InMemoryGenerationService(),
            InMemoryArtifactRepository(),
            InMemoryResultPublisher(),
            UnresolvedOrderRegistry(),
            retry=RetryConfig(
                max_attempts=2,
                initial_delay_seconds=0,
            ),
        )

    assert exc_info.value.operation == "order_queue.take"


def test_worker_records_unresolved_order_and_continues():
    class AssetService(InMemoryGenerationService):
        def queue(self, order, idempotency_key=None):
            if order.order_id == "o1":
                raise TransientError("asset dependency unavailable")

            return super().queue(
                order,
                idempotency_key=idempotency_key,
            )

    orders = [
        Order(order_id="o1"),
        Order(order_id="o2"),
    ]

    queue = InMemoryOrderQueue(orders)
    publisher = InMemoryResultPublisher()
    unresolved = UnresolvedOrderRegistry()

    taken_count = run(
        queue,
        AssetService(),
        InMemoryGenerationService(),
        InMemoryArtifactRepository(),
        publisher,
        unresolved,
        retry=RetryConfig(
            max_attempts=2,
            initial_delay_seconds=0,
        ),
    )

    assert taken_count == 2
    assert [item.order.order_id for item in unresolved.orders] == ["o1"]
    assert unresolved.orders[0].operation == "asset_generation.queue"
    assert len(publisher.shippable_orders) == 1
    assert publisher.shippable_orders[0].order_id == "o2"
