from __future__ import annotations

from unittest import TestCase

from orderforge.circuit_breaker import (
    CircuitBreakerConfig,
    CircuitBreakingProxy,
    build_operation_breakers,
)
from orderforge.exceptions import (
    CircuitOpenError,
    RetryExhaustedError,
    TransientError,
)


class AlwaysUnavailable:
    def __init__(self):
        self.calls = 0

    def read(self):
        self.calls += 1

        raise RetryExhaustedError(
            operation="dependency.read",
            attempts=3,
            cause=TransientError("dependency unavailable"),
        )


class BusinessFailure:
    def __init__(self):
        self.calls = 0

    def call(self):
        self.calls += 1
        raise ValueError("invalid request")


class PartiallyAvailableDependency:
    def __init__(self):
        self.read_calls = 0
        self.write_calls = 0

    def read(self):
        self.read_calls += 1

        raise RetryExhaustedError(
            operation="dependency.read",
            attempts=3,
            cause=TransientError("read unavailable"),
        )

    def write(self):
        self.write_calls += 1
        return "ok"


class HealthyDependency:
    def __init__(self):
        self.calls = 0
        self.label = "healthy"

    def read(self):
        self.calls += 1
        return "ok"


class TestCircuitBreakingProxy(TestCase):
    def test_retry_exhaustion_opens_breaker_at_threshold(self):
        target = AlwaysUnavailable()

        proxy = CircuitBreakingProxy(
            target,
            dependency_name="dependency",
            breakers=build_operation_breakers(
                ["read"],
                CircuitBreakerConfig(
                    failure_threshold=2,
                    recovery_timeout_seconds=30,
                ),
            ),
        )

        with self.assertRaises(RetryExhaustedError):
            proxy.read()

        with self.assertRaises(RetryExhaustedError):
            proxy.read()

        self.assertEqual(
            2,
            target.calls,
        )

        with self.assertRaises(CircuitOpenError):
            proxy.read()

        # OPEN means fail fast. The protected dependency is not called again.
        self.assertEqual(
            2,
            target.calls,
        )

    def test_business_error_does_not_open_breaker(self):
        target = BusinessFailure()

        breakers = build_operation_breakers(
            ["call"],
            CircuitBreakerConfig(
                failure_threshold=1,
            ),
        )

        proxy = CircuitBreakingProxy(
            target,
            dependency_name="dependency",
            breakers=breakers,
        )

        with self.assertRaises(ValueError):
            proxy.call()

        self.assertEqual(
            "closed",
            breakers["call"].current_state,
        )

        with self.assertRaises(ValueError):
            proxy.call()

        self.assertEqual(
            2,
            target.calls,
        )

    def test_operations_have_independent_health_state(self):
        target = PartiallyAvailableDependency()

        breakers = build_operation_breakers(
            [
                "read",
                "write",
            ],
            CircuitBreakerConfig(
                failure_threshold=1,
            ),
        )

        proxy = CircuitBreakingProxy(
            target,
            dependency_name="dependency",
            breakers=breakers,
        )

        with self.assertRaises(RetryExhaustedError):
            proxy.read()

        with self.assertRaises(CircuitOpenError):
            proxy.read()

        # read is OPEN, but write has an independent breaker.
        self.assertEqual(
            "ok",
            proxy.write(),
        )

        self.assertEqual(
            1,
            target.read_calls,
        )
        self.assertEqual(
            1,
            target.write_calls,
        )

    def test_unprotected_method_delegates_to_target(self):
        target = HealthyDependency()

        proxy = CircuitBreakingProxy(
            target,
            dependency_name="dependency",
            breakers={},
        )

        self.assertEqual(
            "ok",
            proxy.read(),
        )
        self.assertEqual(
            1,
            target.calls,
        )

    def test_unprotected_non_callable_attribute_delegates_to_target(self):
        target = HealthyDependency()

        proxy = CircuitBreakingProxy(
            target,
            dependency_name="dependency",
            breakers={},
        )

        self.assertEqual(
            "healthy",
            proxy.label,
        )
