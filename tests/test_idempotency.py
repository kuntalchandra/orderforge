import pytest

from orderforge.exceptions import (
    IdempotencyConflictError,
    TransientError,
)
from orderforge.idempotency import (
    IdempotentGenerationService,
    IdempotentResultPublisher,
)
from orderforge.in_memory.generation import InMemoryGenerationService
from orderforge.in_memory.publisher import InMemoryResultPublisher
from orderforge.models import (
    FailedOrder,
    FailureStage,
    Metadata,
    Order,
    ShippableOrder,
)
from orderforge.retry import RetryingProxy, RetryConfig


def make_shippable(
    order_id: str = "o1",
    xml: str = "<metadata />",
) -> ShippableOrder:
    order = Order(order_id=order_id)

    return ShippableOrder(
        order_id=order_id,
        order=order,
        assets=[],
        asset_details=[],
        metadata=Metadata(
            order_id=order_id,
            xml=xml,
        ),
    )


def make_failed(
    order_id: str = "o1",
    reason: str = "failed",
) -> FailedOrder:
    order = Order(order_id=order_id)

    return FailedOrder(
        order_id=order_id,
        order=order,
        stage=FailureStage.ASSET_GENERATION,
        reason=reason,
    )


def test_generation_retry_after_ambiguous_success_reuses_original_job():
    class ResponseLostOnce(InMemoryGenerationService):
        def __init__(self):
            super().__init__()
            self._lose_response = True

        def queue(self, order, idempotency_key=None):
            job_id = super().queue(
                order,
                idempotency_key=idempotency_key,
            )

            if self._lose_response:
                self._lose_response = False
                raise TransientError(
                    "response lost after server committed job"
                )

            return job_id

    target = ResponseLostOnce()

    service = RetryingProxy(
        IdempotentGenerationService(
            target,
            stage="asset",
        ),
        config=RetryConfig(
            max_attempts=2,
            initial_delay_seconds=0,
        ),
        sleeper=lambda _: None,
        dependency_name="asset_generation",
    )

    assert service.queue(Order(order_id="o1")) == "job-1"
    assert target.job_count == 1


def test_asset_and_metadata_generation_have_independent_keys():
    target = InMemoryGenerationService()
    order = Order(order_id="o1")

    asset = IdempotentGenerationService(
        target,
        stage="asset",
    )

    metadata = IdempotentGenerationService(
        target,
        stage="metadata",
    )

    assert asset.queue(order) == "job-1"
    assert metadata.queue(order) == "job-2"
    assert target.job_count == 2


def test_generation_key_reuse_for_different_order_fails_fast():
    target = InMemoryGenerationService()

    target.queue(
        Order(order_id="o1"),
        idempotency_key="fixed-key",
    )

    with pytest.raises(IdempotencyConflictError):
        target.queue(
            Order(order_id="o2"),
            idempotency_key="fixed-key",
        )


def test_result_retry_after_ambiguous_success_does_not_duplicate_submission():
    class ResponseLostOnce(InMemoryResultPublisher):
        def __init__(self):
            super().__init__()
            self._lose_response = True

        def submit_shippable(
            self,
            order,
            idempotency_key=None,
        ):
            super().submit_shippable(
                order,
                idempotency_key=idempotency_key,
            )

            if self._lose_response:
                self._lose_response = False
                raise TransientError(
                    "response lost after server accepted result"
                )

    target = ResponseLostOnce()

    publisher = RetryingProxy(
        IdempotentResultPublisher(target),
        config=RetryConfig(
            max_attempts=2,
            initial_delay_seconds=0,
        ),
        sleeper=lambda _: None,
        dependency_name="result_publisher",
    )

    publisher.submit_shippable(make_shippable())

    assert len(target.shippable_orders) == 1


def test_duplicate_failed_submission_is_suppressed():
    target = InMemoryResultPublisher()
    publisher = IdempotentResultPublisher(target)
    failed = make_failed()

    publisher.submit_failed(failed)
    publisher.submit_failed(failed)

    assert len(target.failed_orders) == 1


def test_conflicting_terminal_result_for_same_order_fails_fast():
    target = InMemoryResultPublisher()
    publisher = IdempotentResultPublisher(target)

    publisher.submit_shippable(make_shippable())

    with pytest.raises(IdempotencyConflictError):
        publisher.submit_failed(make_failed())


def test_same_terminal_kind_with_changed_payload_is_a_conflict():
    target = InMemoryResultPublisher()
    publisher = IdempotentResultPublisher(target)

    publisher.submit_shippable(
        make_shippable(xml="<v1 />")
    )

    with pytest.raises(IdempotencyConflictError):
        publisher.submit_shippable(
            make_shippable(xml="<v2 />")
        )
