"""
In-memory ResultPublisher. Just records submissions for inspection (by
tests, or by the CLI demo runner). No locking — see queue.py's note;
same reasoning, deferred to Phase 4.
"""

from __future__ import annotations

from typing import List

from ..interfaces import ResultPublisher
from ..models import FailedOrder, ShippableOrder


class InMemoryResultPublisher(ResultPublisher):
    def __init__(self):
        self.shippable_orders: List[ShippableOrder] = []
        self.failed_orders: List[FailedOrder] = []

    def submit_shippable(self, order: ShippableOrder) -> None:
        self.shippable_orders.append(order)

    def submit_failed(self, order: FailedOrder) -> None:
        self.failed_orders.append(order)