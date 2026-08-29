"""
Phase 1 keeps this minimal on purpose: no Transient/Permanent split yet
(that arrives in Phase 2 once there's a retry mechanism to justify it —
introducing it now with nothing to distinguish it from would be dead
abstraction).

What Phase 1 does need: a way to fail fast on structurally invalid input,
per the fail-fast principle from the plan. GenerationFailedError, by
contrast, is not a fail-fast case — it's an expected business outcome the
orchestrator handles explicitly (see orchestrator.py).
"""


class OrderValidationError(Exception):
    """Raised immediately for a structurally invalid Order. Never caught
    and converted into a FailedOrder — an invalid order was never
    correctly taken into the pipeline in the first place."""


class PollExhaustedError(Exception):
    """A generation job never reached a terminal status within the
    configured poll budget. This is NOT the same thing as the job
    reporting FAILED — that's an expected business outcome, represented
    directly as JobStatus.FAILED and branched on in the orchestrator, not
    raised as an exception at all. This is the genuinely exceptional case
    (the service appears stuck), and it propagates rather than being
    converted into a FailedOrder — we don't actually know the order's true
    outcome, so we don't guess."""


class UnknownJobError(Exception):
    """Raised by a GenerationService when asked about a job id it has no
    record of. A programming error (wrong id passed somewhere), not a
    business outcome — fails fast rather than being interpreted as any
    particular JobStatus."""