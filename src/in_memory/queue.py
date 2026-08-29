"""
In-memory FIFO order queue.

No locking: Phase 1 is explicitly single-threaded (see PLAN.md). Locking
for concurrent take() is scoped to Phase 4 along with the rest of the
worker pool's thread-safety story, not added preemptively here.
"""

from __future__ import annotations

from collections import deque
from typing import Iterable, Optional

from ..interfaces import OrderQueue
from ..models import Order


class InMemoryOrderQueue(OrderQueue):
    def __init__(self, orders: Iterable[Order] = ()):
        self._queue = deque(orders)

    def take(self) -> Optional[Order]:
        if not self._queue:
            return None
        return self._queue.popleft()

    def __len__(self) -> int:
        return len(self._queue)