"""Thread-safe TTL cache with same-key request coalescing."""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class CacheConfig:
    ttl_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.ttl_seconds < 0:
            raise ValueError("ttl_seconds must be >= 0")


@dataclass(frozen=True)
class CacheLookup:
    """Distinguishes a cache miss from a cached value of None."""

    found: bool
    value: Any = None


@dataclass
class _CacheEntry:
    value: Any
    expires_at: float


class TTLCache:
    """In-memory TTL cache shared by workers within one process run.

    get_or_load() provides single-flight behaviour per cache key:
    concurrent misses for the same key share one source load, while
    different keys can load concurrently.
    """

    def __init__(
        self,
        config: CacheConfig | None = None,
        clock: Callable[[], float] | None = None,
    ):
        self._config = config if config is not None else CacheConfig()
        self._clock = clock if clock is not None else time.monotonic

        self._entries: dict[str, _CacheEntry] = {}
        self._in_flight: dict[str, Future[Any]] = {}

        self._lock = threading.Lock()

    def get(self, key: str) -> CacheLookup:
        now = self._clock()

        with self._lock:
            return self._lookup_locked(key, now)

    def put(self, key: str, value: Any) -> None:
        expires_at = self._clock() + self._config.ttl_seconds

        with self._lock:
            self._entries[key] = _CacheEntry(
                value=value,
                expires_at=expires_at,
            )

    def get_or_load(
        self,
        key: str,
        loader: Callable[[], Any],
    ) -> Any:
        """Return a cached value or load it once for concurrent same-key misses.

        Source failures are not cached. All callers waiting for the same
        in-flight load observe the result of that same attempt.
        """

        now = self._clock()

        with self._lock:
            cached = self._lookup_locked(key, now)

            if cached.found:
                return cached.value

            future = self._in_flight.get(key)

            if future is None:
                future = Future()
                self._in_flight[key] = future
                is_loader = True
            else:
                is_loader = False

        if not is_loader:
            return future.result()

        try:
            value = loader()
        except Exception as error:
            future.set_exception(error)

            with self._lock:
                if self._in_flight.get(key) is future:
                    del self._in_flight[key]

            raise

        expires_at = self._clock() + self._config.ttl_seconds

        with self._lock:
            self._entries[key] = _CacheEntry(
                value=value,
                expires_at=expires_at,
            )

        future.set_result(value)

        with self._lock:
            if self._in_flight.get(key) is future:
                del self._in_flight[key]

        return value

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        now = self._clock()

        with self._lock:
            expired_keys = [
                key
                for key, entry in self._entries.items()
                if now >= entry.expires_at
            ]

            for key in expired_keys:
                del self._entries[key]

            return len(self._entries)

    def _lookup_locked(
        self,
        key: str,
        now: float,
    ) -> CacheLookup:
        entry = self._entries.get(key)

        if entry is None:
            return CacheLookup(found=False)

        if now >= entry.expires_at:
            del self._entries[key]
            return CacheLookup(found=False)

        return CacheLookup(
            found=True,
            value=entry.value,
        )
