"""
The four interfaces the orchestrator depends on. In-memory implementations
live under in_memory/; later phases/scope (DB-backed, real API) implement
these same interfaces without the orchestrator changing.

GenerationService is used as TWO separate instances (one for asset
generation, one for metadata generation) rather than one instance taking a
"kind" parameter — the lifecycle shape (queue -> poll) is identical for
both, but each kind has independent state, so two instances of the same
interface is the right shape, not one instance branching internally.
"""

from __future__ import annotations

import abc
from typing import List, Optional

from .models import Asset, AssetDetail, FailedOrder, JobStatus, Metadata, Order, ShippableOrder


class OrderQueue(abc.ABC):
    @abc.abstractmethod
    def take(self) -> Optional[Order]:
        """Return the next order, or None if the queue is currently empty."""


class GenerationService(abc.ABC):
    @abc.abstractmethod
    def queue(self, order: Order) -> str:
        """Kick off generation for an order. Returns a job id."""

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
    def submit_shippable(self, order: ShippableOrder) -> None:
        ...

    @abc.abstractmethod
    def submit_failed(self, order: FailedOrder) -> None:
        ...