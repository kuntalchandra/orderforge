"""
Per-order orchestration — rules 1-6 from the problem statement, enforced
as control flow rather than as flags:

  1. Taking the order is the caller's job (see worker.py); process_order
     assumes it already has one.
  2/3. queue_metadata's call site only exists in the branch reached AFTER
     asset generation has already succeeded. There is no code path that
     can reach it otherwise.
  4. Asset failure -> FailedOrder, no asset/asset-detail/metadata data.
  5. Metadata failure -> FailedOrder, WITH asset/asset-detail data, no
     metadata.
  6. Both succeed -> ShippableOrder with everything.
"""

from __future__ import annotations

from dataclasses import dataclass

from .exceptions import PollExhaustedError
from .interfaces import ArtifactRepository, GenerationService, ResultPublisher
from .models import FailedOrder, FailureStage, JobStatus, Order, ShippableOrder


@dataclass
class PollConfig:
    max_polls: int = 20


def _poll_until_terminal(get_status, job_id: str, poll: PollConfig) -> JobStatus:
    for _ in range(poll.max_polls):
        status = get_status(job_id)
        if status.is_terminal:
            return status
    raise PollExhaustedError(
        f"job {job_id} did not reach a terminal state within {poll.max_polls} polls"
    )


def process_order(
    order: Order,
    asset_generation_service: GenerationService,
    metadata_generation_service: GenerationService,
    repository: ArtifactRepository,
    publisher: ResultPublisher,
    poll: PollConfig = PollConfig(),
) -> None:
    # --- Asset generation (rules 2, 3, 4) -----------------------------------
    asset_job_id = asset_generation_service.queue(order)
    asset_status = _poll_until_terminal(asset_generation_service.get_status, asset_job_id, poll)

    if asset_status is JobStatus.FAILED:
        publisher.submit_failed(
            FailedOrder(
                order_id=order.order_id,
                order=order,
                stage=FailureStage.ASSET_GENERATION,
                reason=f"asset generation failed for order {order.order_id}",
                assets=None,
                asset_details=None,
            )
        )
        return

    assets = repository.get_assets(order.order_id)
    asset_details = [repository.get_asset_detail(a.asset_id, order.order_id) for a in assets]

    # --- Metadata generation (rules 2, 3, 5, 6) -----------------------------
    # Reaching this line already proves asset generation succeeded.
    metadata_job_id = metadata_generation_service.queue(order)
    metadata_status = _poll_until_terminal(metadata_generation_service.get_status, metadata_job_id, poll)

    if metadata_status is JobStatus.FAILED:
        publisher.submit_failed(
            FailedOrder(
                order_id=order.order_id,
                order=order,
                stage=FailureStage.METADATA_GENERATION,
                reason=f"metadata generation failed for order {order.order_id}",
                assets=assets,
                asset_details=asset_details,
            )
        )
        return

    metadata = repository.get_metadata(order.order_id)
    publisher.submit_shippable(
        ShippableOrder(
            order_id=order.order_id,
            order=order,
            assets=assets,
            asset_details=asset_details,
            metadata=metadata,
        )
    )