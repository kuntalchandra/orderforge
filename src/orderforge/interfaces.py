"""Interfaces used by the order orchestration client.

Phase 3 makes idempotency participation explicit for remote mutations.
Callers may omit an idempotency key, but the worker composes wrappers that
supply stable keys for generation triggers and terminal submissions.
"""

from __future__ import annotations

import abc
from typing import List, Optional

from .models import (
    Asset,
    AssetDetail,
    FailedOrder,
    JobStatus,
    Metadata,
    Order,
    ShippableOrder,
)


class OrderQueue(abc.ABC):
    @abc.abstractmethod
    def take(self) -> Optional[Order]:
        """Return the next order, or None if the queue is currently empty."""


class GenerationService(abc.ABC):
    @abc.abstractmethod
    def queue(self, order: Order, idempotency_key: str | None = None) -> str:
        """Kick off generation and return a job id.

        When idempotency_key is supplied, repeating the same logical request
        with that key must return the original job rather than create another.
        """

    @abc.abstractmethod
    def get_status(self, job_id: str) -> JobStatus:
        ...


class ArtifactRepository(abc.ABC):
    @abc.abstractmethod
    def get_assets(self, order_id: str) -> List[Asset]:
        ...

    @abc.abstractmethod
    def get_asset_detail(self, asset_id: str, order_id: str) -> AssetDetail:
        ...

    @abc.abstractmethod
    def get_metadata(self, order_id: str) -> Metadata:
        ...


class ResultPublisher(abc.ABC):
    @abc.abstractmethod
    def submit_shippable(
        self,
        order: ShippableOrder,
        idempotency_key: str | None = None,
    ) -> None:
        """Submit a shippable result, deduplicating when a key is supplied."""

    @abc.abstractmethod
    def submit_failed(
        self,
        order: FailedOrder,
        idempotency_key: str | None = None,
    ) -> None:
        """Submit a failed result, deduplicating when a key is supplied."""
