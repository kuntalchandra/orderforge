from __future__ import annotations

import threading
from unittest import TestCase

from orderforge.cache import CacheConfig, TTLCache
from orderforge.caching import CachingArtifactRepository


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeOrder:
    def __init__(self, order_id: str):
        self.order_id = order_id


class BlockingRepository:
    def __init__(self):
        self.asset_reads = 0
        self._count_lock = threading.Lock()

        self.load_started = threading.Event()
        self.release_load = threading.Event()

    def get_assets(self, order):
        with self._count_lock:
            self.asset_reads += 1

        self.load_started.set()

        if not self.release_load.wait(timeout=2):
            raise RuntimeError("test timed out waiting to release repository load")

        return [
            {
                "asset_id": "a1",
                "name": "original",
            }
        ]

    def get_asset_detail(self, order, asset):
        raise NotImplementedError

    def get_metadata(self, order):
        raise NotImplementedError


class MutableRepository:
    def __init__(self):
        self.asset_reads = 0

    def get_assets(self, order):
        self.asset_reads += 1

        return [
            {
                "asset_id": "a1",
                "name": "original",
            }
        ]

    def get_asset_detail(self, order, asset):
        raise NotImplementedError

    def get_metadata(self, order):
        raise NotImplementedError


class TestTTLCache(TestCase):
    def test_cached_none_is_distinct_from_cache_miss(self):
        cache = TTLCache()

        missing = cache.get("missing")

        cache.put("present", None)
        present = cache.get("present")

        self.assertFalse(missing.found)

        self.assertTrue(present.found)
        self.assertIsNone(present.value)

    def test_expired_entry_is_a_cache_miss(self):
        clock = FakeClock()

        cache = TTLCache(
            CacheConfig(ttl_seconds=10),
            clock=clock,
        )

        cache.put("key", "value")

        clock.advance(11)

        result = cache.get("key")

        self.assertFalse(result.found)

    def test_zero_ttl_expires_immediately(self):
        clock = FakeClock()

        cache = TTLCache(
            CacheConfig(ttl_seconds=0),
            clock=clock,
        )

        cache.put("key", "value")

        result = cache.get("key")

        self.assertFalse(result.found)


class TestCachingArtifactRepository(TestCase):
    def test_concurrent_same_key_misses_are_coalesced(self):
        target = BlockingRepository()

        repository = CachingArtifactRepository(
            target,
            TTLCache(),
        )

        order = FakeOrder("o1")

        barrier = threading.Barrier(20)
        results = []
        result_lock = threading.Lock()

        def read_assets():
            barrier.wait()

            value = repository.get_assets(order)

            with result_lock:
                results.append(value)

        threads = [
            threading.Thread(target=read_assets)
            for _ in range(20)
        ]

        for thread in threads:
            thread.start()

        self.assertTrue(
            target.load_started.wait(timeout=2),
            "repository load did not start",
        )

        target.release_load.set()

        for thread in threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

        self.assertEqual(20, len(results))
        self.assertEqual(1, target.asset_reads)

    def test_caller_cannot_mutate_cached_snapshot(self):
        target = MutableRepository()

        repository = CachingArtifactRepository(
            target,
            TTLCache(),
        )

        order = FakeOrder("o1")

        first = repository.get_assets(order)

        first[0]["name"] = "corrupted"
        first.append(
            {
                "asset_id": "a2",
                "name": "added-by-caller",
            }
        )

        second = repository.get_assets(order)

        self.assertEqual(
            [
                {
                    "asset_id": "a1",
                    "name": "original",
                }
            ],
            second,
        )

        self.assertEqual(1, target.asset_reads)
