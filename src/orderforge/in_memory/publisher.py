"""Thread-safe in-memory ResultPublisher with idempotency semantics.

A terminal submission is one atomic transaction:

    check idempotency
        -> append result
        -> record idempotency outcome

The helpers ending in _locked require their caller to already hold
self._lock. They deliberately do not reacquire it.

Result collections are kept private and returned as snapshots so external
readers cannot access or mutate the shared lists outside the locking
contract.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Dict, List

from ..exceptions import IdempotencyConflictError
from ..interfaces import ResultPublisher
from ..models import FailedOrder, ShippableOrder


@dataclass(frozen=True)
class _PublishedResult:
    kind: str
    payload: ShippableOrder | FailedOrder


class InMemoryResultPublisher(ResultPublisher):
    def __init__(self):
        self._shippable_orders: List[ShippableOrder] = []
        self._failed_orders: List[FailedOrder] = []
        self._idempotency_records: Dict[str, _PublishedResult] = {}
        self._lock = threading.Lock()

    @property
    def shippable_orders(self) -> List[ShippableOrder]:
        with self._lock:
            return list(self._shippable_orders)

    @property
    def failed_orders(self) -> List[FailedOrder]:
        with self._lock:
            return list(self._failed_orders)

    def _already_submitted_locked(
        self,
        idempotency_key: str | None,
        kind: str,
        payload: ShippableOrder | FailedOrder,
    ) -> bool:
        """Caller must hold self._lock."""

        if idempotency_key is None:
            return False

        existing = self._idempotency_records.get(idempotency_key)

        if existing is None:
            return False

        if existing.kind != kind or existing.payload != payload:
            raise IdempotencyConflictError(
                f"result key {idempotency_key!r} "
                "reused for a different terminal result"
            )

        return True

    def _record_submission_locked(
        self,
        idempotency_key: str | None,
        kind: str,
        payload: ShippableOrder | FailedOrder,
    ) -> None:
        """Caller must hold self._lock."""

        if idempotency_key is not None:
            self._idempotency_records[idempotency_key] = _PublishedResult(
                kind=kind,
                payload=payload,
            )

    def submit_shippable(
        self,
        order: ShippableOrder,
        idempotency_key: str | None = None,
    ) -> None:
        with self._lock:
            if self._already_submitted_locked(
                idempotency_key,
                "SHIPPABLE",
                order,
            ):
                return

            self._shippable_orders.append(order)

            self._record_submission_locked(
                idempotency_key,
                "SHIPPABLE",
                order,
            )

    def submit_failed(
        self,
        order: FailedOrder,
        idempotency_key: str | None = None,
    ) -> None:
        with self._lock:
            if self._already_submitted_locked(
                idempotency_key,
                "FAILED",
                order,
            ):
                return

            self._failed_orders.append(order)

            self._record_submission_locked(
                idempotency_key,
                "FAILED",
                order,
            )
