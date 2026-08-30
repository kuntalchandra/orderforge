# Orderforge Code Review Drill — Tracker

## Purpose
Track how review judgement evolves, not just how many defects are found. Each
session records the first-pass reasoning, reverse-shadow additions, why misses
happened, and the review lens to carry into the next phase.

## Review lenses
1. State-machine correctness
2. Failure semantics
3. State lifecycle / ownership
4. Concurrency / atomicity
5. Side-effect and retry safety
6. Contract precision
7. Phase-boundary judgement
8. Operability / diagnosability
9. Tests as executable contracts
10. End-to-end guarantee: what outcome does this component actually prove?

## Session 1 — Phase 2 retry

### Reviewer findings
- Challenged retry termination and whether exhausted transient errors should be
  handled explicitly rather than simply raised.
- Flagged future worker locking / duplicate-processing risk.
- Deliberately deprioritised tests while reviewing logic.

### Reverse-shadow additions
- Retry exhaustion is neither a business `FAILED` result nor something to log
  and forget. It becomes `RetryExhaustedError` and an inspectable unresolved
  order once an order identity is known.
- The first unresolved registry had a lifecycle bug: if `run()` created it
  internally, its contents disappeared to the caller. It became required
  injected state.
- `processed` was misleading because unresolved orders were counted; renamed
  conceptually to `taken_count`.
- Operation names such as `queue` were not diagnosable; qualified dependency
  names were added.
- `OrderQueue.take()` exposed an ambiguous destructive-claim problem that a
  generic retry wrapper cannot solve.

### Learning signal
Strong instinct around failure handling and concurrency. The biggest gap was
following state and guarantees across composition boundaries rather than only
inside the function being reviewed.

## Session 2 — Phase 3 idempotency

### Reviewer findings
- Challenged `contains()` + `get()` as redundant state access.
- Noted `get()` should safely represent absence instead of blindly raising a
  missing-key exception.
- Correctly accepted stage/order idempotency key structure.

### Reverse-shadow additions
- **Core blocker:** client-only dedup did not solve the ambiguous-timeout case it
  claimed to solve. The local record was written only after `target.queue()` or
  `submit_*()` returned. A server-side success followed by a lost response left
  no local record, so retry repeated the side effect.
- Strong idempotency therefore requires the stable key to travel with the
  mutating request and the downstream service to own the authoritative mapping.
- Duplicate and conflicting terminal submissions were incorrectly treated the
  same. `SHIPPABLE -> SHIPPABLE` with the same payload is replay; `SHIPPABLE ->
  FAILED`, `FAILED -> SHIPPABLE`, or changed payload under the same key is an
  invariant violation and now fails fast.
- A cache/dedup entry must be reviewed by asking what fact it proves. The first
  design proved only "the client observed a successful response", not "the
  downstream mutation happened exactly once".
- Queue work claiming is separate from mutation idempotency. Local thread-safe
  take belongs to Phase 4; durable reserve/ack semantics belong to Phase 9.

### Reviewer self-observation
After Phases 2-3, reviewer explicitly recognised a need to understand the
end-to-end design outcome of each phase before going line-by-line. This becomes
an intentional review step from Phase 4 onward.

### Learning signal
The line-level API observations were correct. The next growth step is to start a
review by tracing the complete operation across client, retry wrapper,
downstream side effect, response, state write, and replay. Ask what guarantee
survives every interruption point.

## Phase progress

| Phase | Main review focus | Reviewer findings | Reverse-shadow / deeper additions | Status |
|---|---|---|---|---|
| 1 Basic orchestration | State machine | Baseline | Baseline | Done |
| 2 Retry | Failure semantics | Exhaustion handling; future locking | Registry lifecycle; queue ambiguity; metric naming; diagnostics | Committed |
| 3 Idempotency | Replay safety | Store API simplification; key shape | Client-cache semantic failure; downstream participation; conflicts; proof of state | Ready to commit |
| 4 Concurrency | Atomicity | Next | Worker pool, lock scope, races, throughput | Next |
| 5 Caching | Freshness | Later | Invalidation, stampede, ownership | Locked |
| 6 Circuit breaker | Dependency failure | Later | Retry interaction, half-open concurrency | Locked |
| 7 Backpressure | Capacity | Later | Bounds, fairness, reject/park | Locked |
| 8 Observability | Operational truth | Later | Metric semantics/cardinality | Locked |
| 9 Persistence/API | Distributed recovery | Later | Durable claims, transactions, remote idempotency | Locked |

## Mandatory review sequence from Phase 4 onward
Before reading implementation details, write down:

1. **Phase outcome:** what new guarantee should this PR add?
2. **End-to-end path:** which components participate from input to terminal
   result?
3. **State owners:** where is each piece of mutable state stored and who owns its
   lifecycle?
4. **Interruption points:** what happens if execution stops before/after each
   side effect?
5. **Concurrency points:** which check-then-act sequences become races?
6. **Phase boundary:** which real risks are intentionally deferred, and to which
   phase?
7. **Tests:** which failure sequence would disprove the claimed guarantee?

Only then do the normal line-by-line review.

## Phase 4 carry-forward checklist
- Is `OrderQueue.take()` atomic across workers?
- Can two workers both pass an idempotency-record absence check and perform the
  same mutation?
- Which lock owns generation idempotency records?
- Which lock owns publisher idempotency records and result lists?
- Is the unresolved registry thread-safe?
- Are locks held during simulated/remote I/O, unnecessarily serialising work?
- Can lock ordering deadlock?
- Does the bounded pool actually provide backpressure, or can another queue
  still grow without bound?
- Do concurrency tests coordinate threads deterministically enough to expose
  races rather than merely "run things in parallel"?

## Trend to revisit
Target progression:

**code shape -> local correctness -> lifecycle/ownership -> end-to-end guarantee
-> concurrency/atomicity -> distributed ambiguity -> operational recovery ->
roadmap/design-assumption review**

### Orderforge Phase 4 — concurrency / atomicity

**Reviewer findings**
- Challenged `jitter_ratio=0.0` because deterministic retries undermine the
  Phase 4 thundering-herd protection.
- Challenged optional idempotency keys on mutating APIs as an unsafe escape
  hatch when idempotency is intended as a system invariant.
- Correctly challenged the claim that `get_status()` never accesses shared
  mutable state and therefore needs no lock.
- Challenged publisher helper thread-safety and worker-loop locking.

**Reverse shadow**
- The jitter implementation itself was reachable when configured non-zero;
  the real defect was the operational default.
- `max_delay_seconds` also needed to remain a hard cap after applying
  jitter.
- Physical queue-entry ownership is not the same as logical-order
  ownership. Duplicate entries for one order deliberately converge on the
  same idempotent job, so multiple workers can poll the same `_JobRecord`.
- Publisher `_already_submitted_locked()` and
  `_record_submission_locked()` must NOT independently acquire the same
  ordinary Lock. Their caller owns the lock across the complete
  check → append → record transaction.
- Locking `_worker_loop()` would serialize the worker pool. Protect the
  smallest shared-state invariant rather than the whole concurrent
  workflow.
- Public mutable publisher lists weakened the thread-safety contract even
  though writes were locked; expose locked snapshots instead.
- `DEFAULT_WORKER_POOL_SIZE=150`, PLAN documentation, and
  `run(num_workers=1)` contradicted one another. The runtime default now
  matches the declared Phase 4 default.
- A Barrier immediately before a method call does not deterministically
  expose an internal check-then-act race. Race tests now coordinate inside
  the observed read so every unlocked caller captures the same pre-mutation
  state.

**Reasoning progression**
The next concurrency-review question is no longer simply "does this need a
lock?" Use:

1. What exact state is shared?
2. What invariant spans more than one operation?
3. What is the smallest sequence that must be atomic?
4. Which code can execute outside that critical section?
5. Does physical ownership actually imply logical/state ownership?
6. Can the test deterministically force the race window it claims to test?

The key Phase 4 lesson: locking too broadly can be as architecturally wrong
as failing to lock at all.

## Phase 6-7 — Resilience and production-boundary review

### Build vs reuse became part of the review

Circuit-breaker review initially moved into implementing CLOSED / OPEN /
HALF_OPEN state management directly.

That exposed real concurrency questions:

- transition ownership
- half-open probe ownership
- stale in-flight completions
- concurrent failure accounting
- recovery timing

The implementation was deliberately replaced with PyBreaker.

Review lesson:

Understanding infrastructure internals is useful, but implementation ownership
must still be justified.

A mature library can own commodity state-machine mechanics while Orderforge
owns integration policy.


### Logical failure vs physical retry attempt

Circuit breaking is placed outside retry.

One caller may make several physical attempts, but the breaker sees one logical
failure only after the retry budget is exhausted.

Review lesson:

Before counting failures, identify what the counter represents.

Physical attempts and logical operations are different units.


### Exception classification is part of resilience design

Only retry-exhausted dependency interaction failures contribute to circuit
health.

Business, validation, idempotency and invariant failures do not imply that a
dependency is unavailable.

Review lesson:

Exception handling is not only control flow.

The exception taxonomy determines how resilience mechanisms interpret system
health.


### Breaker scope is an architectural boundary

Per-worker breakers would make every worker independently rediscover an outage.

One breaker for all operations would allow one failing operation to disable
unrelated healthy operations.

Phase 6 therefore starts with shared operation-level breakers.

Review lesson:

Shared-state scope determines failure isolation and blast radius.


### Circuit breaker does not replace idempotency

Circuit breaking decides whether an operation should currently be attempted.

Idempotency determines whether repeated mutation attempts can safely represent
one logical mutation.

Review lesson:

Resilience mechanisms solve different failure dimensions and should not be
treated as interchangeable safeguards.


### Cache hits should not affect dependency health

Caching remains outside circuit breaking and retry.

A cache hit performs no dependency interaction and therefore should not consume
retry capacity or influence circuit state.

Review lesson:

Decorator ordering changes semantics, not merely code organisation.


### Backpressure belongs at the ownership boundary

An application semaphore was considered and rejected as the generic production
backpressure mechanism.

For a pull-based durable queue, consumer concurrency, prefetch / outstanding
delivery limits and acknowledgement behaviour should normally keep excess work
in the upstream queue.

Review lesson:

Do not duplicate infrastructure responsibility inside the application without
first identifying where work ownership actually transfers.


### Rate limiting and backpressure are different

A configured rate controls how quickly work may be offered or consumed.

Backpressure is the dynamic propagation of reduced downstream capacity toward
upstream work acquisition.

Review lesson:

A fixed rate may participate in a backpressure design but does not by itself
define one.


### Observability does not require building an observability framework

The production system should expose signals for:

- terminal success
- business failure by stage
- unresolved orders
- retry exhaustion
- circuit behaviour
- dependency latency
- end-to-end latency
- queue backlog / lag

Orderforge does not need custom metrics infrastructure to demonstrate that
design.

Review lesson:

Identify what must be observable separately from deciding which production
platform records it.


### Persistence must follow ownership

Local persistence cannot make an ambiguous remote mutation idempotent if the
actual side-effect owner does not participate in the idempotency protocol.

Likewise, locally simulating queue lease / acknowledgement behaviour does not
prove correctness against a real queue.

Review lesson:

Durability is useful only when it is placed at the boundary that owns the
state or side effect being recovered.


### Do not invent external contracts

The exercise establishes JSON API interactions but the repository does not
contain the concrete testserver endpoint and payload contract.

HTTP adapters were therefore not fabricated.

Review lesson:

An abstraction can be ready for integration without guessing the contract of
the external system.

The correct implementation point is when that contract becomes known.


### Final review progression

The review drill evolved through:

code shape
    ->
local correctness
    ->
state-machine correctness
    ->
failure semantics
    ->
ownership
    ->
ambiguous outcomes
    ->
concurrency / atomicity
    ->
resilience composition
    ->
build vs reuse
    ->
system boundaries

The final question is no longer only:

"Is this implementation correct?"

It also includes:

"Should this responsibility be implemented here at all?"
