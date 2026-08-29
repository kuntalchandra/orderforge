"""Domain, interaction, and idempotency exceptions."""


class OrderValidationError(Exception):
    """Raised immediately for a structurally invalid Order."""


class PollExhaustedError(Exception):
    """A generation job did not reach a terminal state within its poll budget."""


class UnknownJobError(Exception):
    """Raised when a generation service receives an unknown job id."""


class TransientError(Exception):
    """A temporary interaction failure that may succeed if attempted again."""


class RetryExhaustedError(Exception):
    """A transient operation still failed after all configured attempts."""

    def __init__(self, operation: str, attempts: int, cause: TransientError):
        self.operation = operation
        self.attempts = attempts
        self.cause = cause
        super().__init__(
            f"retry exhausted for {operation!r} after {attempts} attempts: {cause}"
        )


class IdempotencyConflictError(Exception):
    """An idempotency key was reused for a different logical operation."""
