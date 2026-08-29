"""
In-memory FIFO order queue.

take() protects the whole check-then-pop sequence. Correctness therefore
does not depend on deque.popleft() or other individual operations happening
to be atomic in one Python interpreter.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Iterable, Optional

from ..interfaces import OrderQueue
from ..models import Order


class InMemoryOrderQueue(OrderQueue):
    def __init__(self, orders: Iterable[Order] = ()):
        self._queue = deque(orders)
        self._lock = threading.Lock()

    def take(self) -> Optional[Order]:
        with self._lock:
            if not self._queue:
                return None

            return self._queue.popleft()

    def __len__(self) -> int:
        with self._lock:
            return len(self._queue)
