"""
Domain models.

Plain dataclasses: no external dependency, and this exercise doesn't need
validation beyond what's noted in exceptions.py / orchestrator.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class JobStatus(str, Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        return self in (JobStatus.SUCCESS, JobStatus.FAILED)


class FailureStage(str, Enum):
    ASSET_GENERATION = "ASSET_GENERATION"
    METADATA_GENERATION = "METADATA_GENERATION"


@dataclass
class Order:
    order_id: str
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Asset:
    asset_id: str
    order_id: str
    asset_type: str  # e.g. "AUDIO_WAV", "COVERART_TIFF"
    uri: str


@dataclass
class AssetDetail:
    asset_id: str
    order_id: str
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Metadata:
    order_id: str
    xml: str


@dataclass
class ShippableOrder:
    order_id: str
    order: Order
    assets: List[Asset]
    asset_details: List[AssetDetail]
    metadata: Metadata


@dataclass
class FailedOrder:
    order_id: str
    order: Order
    stage: FailureStage
    reason: str
    assets: Optional[List[Asset]] = None
    asset_details: Optional[List[AssetDetail]] = None