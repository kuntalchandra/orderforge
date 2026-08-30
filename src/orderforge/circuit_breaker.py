"""Circuit-breaker integration backed by PyBreaker."""

from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
from typing import Any, Iterable

import pybreaker

from .exceptions import (
    CircuitOpenError,
    RetryExhaustedError,
)


@dataclass(frozen=True)
class CircuitBreakerConfig:
    failure_threshold: int = 5
    recovery_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError(
                "failure_threshold must be >= 1"
            )

        if self.recovery_timeout_seconds < 0:
            raise ValueError(
                "recovery_timeout_seconds must be >= 0"
            )


def _exclude_from_failure_count(
    error: BaseException,
) -> bool:
    """Only retry-exhausted dependency failures affect circuit health.

    The breaker sits outside RetryingProxy, so RetryExhaustedError represents
    one logical dependency operation that failed after consuming its retry
    budget.

    Domain, validation and invariant errors propagate normally but do not
    indicate that the dependency itself is unhealthy.
    """

    return not isinstance(
        error,
        RetryExhaustedError,
    )


def build_operation_breakers(
    operations: Iterable[str],
    config: CircuitBreakerConfig,
) -> dict[str, pybreaker.CircuitBreaker]:
    return {
        operation: pybreaker.CircuitBreaker(
            fail_max=config.failure_threshold,
            reset_timeout=config.recovery_timeout_seconds,
            exclude=[_exclude_from_failure_count],

            # The operation that reaches the threshold should still surface
            # its RetryExhaustedError. Later calls rejected by the OPEN
            # circuit surface CircuitBreakerError and are translated below.
            throw_new_error_on_trip=False,
        )
        for operation in operations
    }


class CircuitBreakingProxy:
    """Apply independent PyBreaker instances to selected target operations."""

    def __init__(
        self,
        target: Any,
        dependency_name: str,
        breakers: dict[str, pybreaker.CircuitBreaker],
    ):
        self._target = target
        self._dependency_name = dependency_name
        self._breakers = breakers

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(
            self._target,
            name,
        )

        breaker = self._breakers.get(name)

        if breaker is None or not callable(attribute):
            return attribute

        @wraps(attribute)
        def guarded(*args, **kwargs):
            try:
                return breaker.call(
                    attribute,
                    *args,
                    **kwargs,
                )
            except pybreaker.CircuitBreakerError as error:
                raise CircuitOpenError(
                    "circuit open: "
                    f"dependency={self._dependency_name} "
                    f"operation={name}"
                ) from error

        return guarded