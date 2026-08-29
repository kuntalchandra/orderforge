"""Generic retry wrapper for transient interaction failures."""

from __future__ import annotations

import functools
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Callable

from .exceptions import RetryExhaustedError, TransientError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryConfig:
    """Retry policy. max_attempts includes the initial call.

    jitter_ratio adds randomness to each computed delay to avoid many
    concurrent callers retrying a struggling dependency in lockstep.

    max_delay_seconds is a hard upper bound on the final sleep duration,
    including jitter.

    Tests that need deterministic exact delays should explicitly use
    jitter_ratio=0.0.
    """

    max_attempts: int = 3
    initial_delay_seconds: float = 0.1
    backoff_multiplier: float = 2.0
    max_delay_seconds: float = 2.0
    jitter_ratio: float = 0.2

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.initial_delay_seconds < 0:
            raise ValueError("initial_delay_seconds must be >= 0")
        if self.backoff_multiplier < 1:
            raise ValueError("backoff_multiplier must be >= 1")
        if self.max_delay_seconds < 0:
            raise ValueError("max_delay_seconds must be >= 0")
        if not 0.0 <= self.jitter_ratio <= 1.0:
            raise ValueError("jitter_ratio must be between 0.0 and 1.0")


class RetryingProxy:
    """Apply one retry policy uniformly to callable operations on a target."""

    def __init__(
        self,
        target: Any,
        config: RetryConfig = RetryConfig(),
        sleeper: Callable[[float], None] = time.sleep,
        dependency_name: str | None = None,
        rand: Callable[[], float] = random.random,
    ):
        self._target = target
        self._config = config
        self._sleeper = sleeper
        self._dependency_name = dependency_name or type(target).__name__
        self._rand = rand

    def _delay_for_attempt(self, attempt: int) -> float:
        base = min(
            self._config.initial_delay_seconds
            * self._config.backoff_multiplier ** (attempt - 1),
            self._config.max_delay_seconds,
        )

        if self._config.jitter_ratio == 0.0:
            return base

        low = base * (1 - self._config.jitter_ratio)
        high = min(
            base * (1 + self._config.jitter_ratio),
            self._config.max_delay_seconds,
        )

        return low + self._rand() * (high - low)

    def __getattr__(self, name: str) -> Any:
        operation = getattr(self._target, name)

        if not callable(operation):
            return operation

        operation_name = f"{self._dependency_name}.{name}"

        @functools.wraps(operation)
        def retrying_operation(*args: Any, **kwargs: Any) -> Any:
            for attempt in range(1, self._config.max_attempts + 1):
                try:
                    return operation(*args, **kwargs)
                except TransientError as exc:
                    if attempt == self._config.max_attempts:
                        logger.error(
                            "retry exhausted: operation=%s attempts=%d error=%s",
                            operation_name,
                            attempt,
                            exc,
                        )
                        raise RetryExhaustedError(
                            operation_name,
                            attempt,
                            exc,
                        ) from exc

                    delay = self._delay_for_attempt(attempt)

                    logger.warning(
                        "transient failure: operation=%s attempt=%d/%d "
                        "retry_in=%.3fs error=%s",
                        operation_name,
                        attempt,
                        self._config.max_attempts,
                        delay,
                        exc,
                    )

                    self._sleeper(delay)

        return retrying_operation
