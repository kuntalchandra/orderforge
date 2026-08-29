"""In-memory ResultPublisher with downstream idempotency semantics."""

from __future__ import annotations

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
        self.shippable_orders: List[ShippableOrder] = []
        self.failed_orders: List[FailedOrder] = []
        self._idempotency_records: Dict[str, _PublishedResult] = {}

    def _already_submitted(
        self,
        idempotency_key: str | None,
        kind: str,
        payload: ShippableOrder | FailedOrder,
    ) -> bool:
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

    def _record_submission(
        self,
        idempotency_key: str | None,
        kind: str,
        payload: ShippableOrder | FailedOrder,
    ) -> None:
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
        if self._already_submitted(idempotency_key, "SHIPPABLE", order):
            return

        self.shippable_orders.append(order)
        self._record_submission(idempotency_key, "SHIPPABLE", order)

    def submit_failed(
        self,
        order: FailedOrder,
        idempotency_key: str | None = None,
    ) -> None:
        if self._already_submitted(idempotency_key, "FAILED", order):
            return

        self.failed_orders.append(order)
        self._record_submission(idempotency_key, "FAILED", order)
