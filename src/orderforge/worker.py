"""
Phase 1 worker: single-threaded, drains the queue until empty. No
concurrency, no backoff-on-empty loop — the queue is a fixed in-memory
collection for now, so "empty" reliably means "done," not "temporarily
empty." That distinction (and the backoff it implies against a live
queue) becomes relevant starting Phase 4.
"""

from __future__ import annotations

from .interfaces import ArtifactRepository, GenerationService, OrderQueue, ResultPublisher
from .orchestrator import PollConfig, process_order


def run(
    queue: OrderQueue,
    asset_generation_service: GenerationService,
    metadata_generation_service: GenerationService,
    repository: ArtifactRepository,
    publisher: ResultPublisher,
    poll: PollConfig = PollConfig(),
) -> int:
    """Process every order currently in the queue. Returns the count processed."""
    count = 0
    while True:
        order = queue.take()
        if order is None:
            break
        process_order(
            order,
            asset_generation_service,
            metadata_generation_service,
            repository,
            publisher,
            poll,
        )
        count += 1
    return count