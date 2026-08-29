"""Read-through caching for artifact repository reads."""

from __future__ import annotations

import copy
from typing import Callable, TypeVar

from .cache import TTLCache
from .interfaces import ArtifactRepository
from .models import Asset, AssetDetail, Metadata


T = TypeVar("T")


class CachingArtifactRepository(ArtifactRepository):
    """Thread-safe read-through cache for completed artifacts.

    This decorator preserves the ArtifactRepository contract exactly.

    Cached values are treated as snapshots. The cache stores its own defensive
    copy and every caller receives a new defensive copy so callers cannot
    mutate shared cached state through aliases.
    """

    def __init__(
        self,
        target: ArtifactRepository,
        cache: TTLCache,
    ):
        self._target = target
        self._cache = cache

    def get_assets(self, order_id: str) -> list[Asset]:
        return self._get_or_load_snapshot(
            key=f"assets:{order_id}",
            loader=lambda: self._target.get_assets(order_id),
        )

    def get_asset_detail(
        self,
        asset_id: str,
        order_id: str,
    ) -> AssetDetail:
        return self._get_or_load_snapshot(
            key=f"asset-detail:{order_id}:{asset_id}",
            loader=lambda: self._target.get_asset_detail(
                asset_id,
                order_id,
            ),
        )

    def get_metadata(self, order_id: str) -> Metadata:
        return self._get_or_load_snapshot(
            key=f"metadata:{order_id}",
            loader=lambda: self._target.get_metadata(order_id),
        )

    def _get_or_load_snapshot(
        self,
        key: str,
        loader: Callable[[], T],
    ) -> T:
        cached_snapshot = self._cache.get_or_load(
            key,
            lambda: copy.deepcopy(loader()),
        )

        return copy.deepcopy(cached_snapshot)
