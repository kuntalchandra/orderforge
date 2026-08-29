# Orderforge Code Review Drill — Tracker

## Purpose
This tracker records not only defects found, but how the review reasoning evolved: what was noticed first, what was missed, whether a comment belonged to the current PR, and what engineering principle should transfer to future reviews.

The goal is to build reviewer judgement across correctness, failure semantics, concurrency, API contracts, incremental architecture, observability, and operational recovery. Raw defect counts are secondary.

## Review lenses
For every PR, review across these lenses instead of reading only top-to-bottom:

1. **State-machine correctness** — can any path violate ordering or terminal-state rules?
2. **Failure semantics** — do business failure, transient failure, ambiguous outcome, and programming error remain distinct?
3. **Lifecycle / ownership** — who creates state, who owns it, and can callers inspect it after the function returns?
4. **Concurrency / atomicity** — what becomes shared, and which read-modify-write or claim operations must be atomic?
5. **Side-effect safety** — can a timeout hide a successful remote mutation and make retry unsafe?
6. **Contract precision** — names, counts, return values, exception types, and tests should say exactly what happened.
7. **Phase boundary judgement** — is a concern real, and separately, does it belong in this PR?
8. **Operability** — when something goes wrong, can an operator identify dependency, operation, order, and outcome?
9. **Test-as-contract** — which semantic decisions need executable protection, especially off-by-one and failure-path behaviour?

## Session log

### Session 1 — Phase 2 retry PR, live review + reverse shadow

**PR scope:** generic retry with exponential backoff across `OrderQueue`, both `GenerationService` instances, `ArtifactRepository`, and `ResultPublisher`.

#### Findings raised by reviewer

1. **Retry termination / exhaustion behaviour**
   - Reviewer noticed `retrying_operation()` terminated by re-raising once `max_attempts` was reached and questioned whether exhaustion should be handled more gracefully and visibly.
   - This was a strong failure-semantics observation: repeated transient failure is not the same as an ordinary business `FAILED` outcome.
   - Reverse-shadow refinement: do not convert exhaustion into `FailedOrder` and do not merely log it. Raise a distinct `RetryExhaustedError`; the worker records an unresolved order because the true downstream outcome may be unknown.

2. **Missing locking / future worker safety**
   - Reviewer noticed the worker architecture is intended to become multi-threaded and challenged the absence of locking, specifically the risk of duplicate processing of one order.
   - Strong architectural instinct: shared work claiming must eventually be atomic.
   - Refinement in wording: workers do not "interrupt" each other; the failure mode is concurrent claim/process of the same logical order or races in other shared state.
   - Phase-boundary correction: the concern is valid, but full worker-pool locking belongs to Phase 4 by plan. Phase 2 should not partially implement concurrency.

3. **Tests intentionally deprioritised during first review**
   - Reviewer chose to focus on implementation reasoning and assumed correct logic would imply acceptable tests.
   - Learning: tests here are not only implementation verification; they encode semantic contracts such as "3 attempts means initial call + 2 retries" and exact backoff progression.

#### Findings missed on first pass

1. **Unresolved registry lifecycle — blocking**
   - Initial revised worker accepted `unresolved: UnresolvedOrderRegistry | None = None` and created one internally when absent.
   - Locally the registry looked correct, but `run()` returned only an integer; internally-created unresolved state became unreachable after return.
   - This violated the explicit plan requirement that unresolved orders remain inspectable.
   - Fix: registry is now a required injected dependency.
   - Transferable lesson: follow important state beyond the class or function where it is written. A correct data structure can still be ineffective because of ownership/lifecycle composition.

2. **Ambiguous `OrderQueue.take()` is not solved by Phase 3's stated idempotency scope**
   - Uniform retry sounds clean at the abstraction level, but destructive `take()` can have an ambiguous result: server removes Order A, response is lost, retry returns Order B.
   - Generation-trigger and publisher idempotency do not repair the lost claim for Order A.
   - This exposed a roadmap assumption rather than a local code defect.
   - Carry-forward: Phase 3 must explicitly revisit queue claim semantics such as reserve/lease + ack or visibility timeout.
   - Transferable lesson: challenge whether two operations that share an interface wrapper actually share retry semantics.

3. **`processed` / `count` terminology was too broad**
   - Worker incremented the count even when processing ended unresolved.
   - Returning `3` could mean one shippable, one business-failed, one unresolved.
   - Fix: internally call it `taken_count` and document the returned metric precisely.
   - Transferable lesson: metric and API names become architecture later. Imprecise names today turn into misleading observability tomorrow.

4. **Operation identity lacked dependency context**
   - `RetryExhaustedError(operation="queue")` could mean asset generation, metadata generation, or another dependency method with the same name.
   - Fix: record qualified names such as `asset_generation.queue` and `order_queue.take`.
   - Transferable lesson: "error is visible" is weaker than "error is diagnosable".

#### Reviewer strengths observed

- Strong sensitivity to concurrency and shared-state risks even before concurrency code arrived.
- Good instinct that retry exhaustion needs an explicit terminal handling path rather than an unexamined `raise`.
- Willingness to challenge the implementation rather than limit review to style or test success.

#### Growth areas exposed

- **Lifecycle tracing:** after seeing a registry/cache/store, trace who owns it and whether the useful state survives the call boundary.
- **Semantic equivalence of retries:** classify operations as read-only, idempotent mutation, non-idempotent mutation, destructive claim, or ambiguous remote mutation before accepting a generic retry abstraction.
- **Concern vs phase:** first identify the engineering risk, then independently decide whether the current PR should fix it.
- **Test contracts:** review tests for semantics, not just coverage count.
- **Operational identity:** ask whether logs/errors identify enough dimensions to diagnose the real failing dependency.

## Phase progress

| Phase | Review mode | Key reviewer findings | Key misses / reverse-shadow additions | Status |
|---|---|---|---|---|
| 1 Basic orchestration | Baseline | — | — | Done |
| 2 Retry/backoff | Live PR review + reverse shadow | Exhaustion handling; future locking concern | Registry lifecycle; ambiguous destructive take; metric naming; operation qualification; tests as contract | Ready to commit |
| 3 Idempotency | Planned | Focus: ambiguous side effects, dedup scope, queue claim semantics, check-then-act boundaries | — | Next |
| 4 Concurrency | Planned | Focus: atomic claim, shared mutable state, lock scope, deadlock/throughput trade-offs | — | Locked |
| 5 Caching | Planned | Focus: cache correctness, invalidation, stale data, stampede behaviour | — | Locked |
| 6 Circuit breaker | Planned | Focus: state transitions, interaction with retry, half-open concurrency | — | Locked |
| 7 Backpressure | Planned | Focus: bounded resources, reject/park semantics, fairness | — | Locked |
| 8 Observability | Planned | Focus: metric semantics, cardinality, diagnosability, silent failure | — | Locked |
| 9 Persistence/API | Planned | Focus: distributed failure, transaction boundaries, real remote semantics | — | Locked |

## Carry-forward checklist for Phase 3

Before commenting on implementation details, explicitly answer:

- Which calls have side effects?
- For each side effect, can "success happened but response was lost" occur?
- What key defines idempotency: order, stage, job, submission type, or something else?
- Is check-then-act atomic today? If not, is that intentionally deferred to Phase 4?
- Does dedup prevent duplicate action without suppressing a legitimate later action?
- What happens to `OrderQueue.take()` under an ambiguous response? The Phase 2 roadmap note requires an explicit answer.
- Can unresolved state be replayed/recovered, or only inspected?
- What exact tests encode those decisions?

## Trend to revisit after several phases

Do not score progress only by "issues found". Track whether findings move from visible structural defects toward hidden semantic and system-level defects. The target progression is:

**code shape → local correctness → state lifecycle → concurrency/atomicity → distributed ambiguity → operational recovery → roadmap/design assumption review.**
