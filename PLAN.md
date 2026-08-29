# PLAN.md — orderforge (Artifact Generation Exercise)

## Scope
An exercise: build a client that ingests generic "Orders" from a queue,
orchestrates two dependent async generation steps (Asset, Metadata) via a
set of JSON-style APIs, and publishes a terminal result per order. Starts
as an in-memory implementation. Designed so the scope can grow —
persistence/repository, a real remote API — without rewriting the
orchestration logic itself.

## NFR targets (assumed for this exercise, briefly justified)
- Target throughput: 10,000 orders/min (~167/sec).
- Little's Law: concurrency ≈ throughput × latency. With a short simulated
  per-phase generation delay (50-300ms) and two sequential phases per
  order (rule 2), end-to-end latency ≈ ~400ms, so sustaining 167/sec needs
  roughly ~65-70 orders concurrently in flight.
- Worker pool sized with headroom over that minimum (150 workers — see
  "concurrency model" below for exactly what a "worker" is).

## Architecture — decomposed for OCP
Rather than one large "TestServer" interface with every method on it,
split into small, focused, independently-swappable interfaces. The
orchestrator depends only on these abstractions (constructor-injected).
Extending scope later — DB-backed persistence, a real HTTP API — means
writing a new class that implements an interface; it does not mean
touching the orchestrator. That's the concrete mechanism for "open for
extension, closed for modification":

- `OrderQueue` — `take() -> Order | None`
- `GenerationService` — `queue(order) -> job_id`, `get_status(job_id)`.
  One generic interface reused for both Asset and Metadata generation —
  but only the *lifecycle* is identical (trigger a job, poll until
  SUCCESS/FAILED). The *result content* is not: asset generation yields
  N typed `Asset` records per order (1:many, per the data model),
  metadata generation yields exactly one `Metadata` record (1:1). That
  asymmetry is why result retrieval stays on a separate, non-generic
  interface below rather than being forced into the same abstraction.
- `ArtifactRepository` — read side: `get_assets`, `get_asset_detail`, `get_metadata`
- `ResultPublisher` — write side: `submit_shippable`, `submit_failed`

v1 implementations of all four are in-memory. Nothing about the
orchestrator, worker pool, or tests refers to "in-memory" — they refer
only to the interfaces.

## Out of scope for now (not permanent — see roadmap)
- Persistence across restarts — becomes its own phase once a DB/repository
  is introduced
- Distributed/multi-process workers — deferred, not excluded: at the
  current NFR target the work is I/O-bound and single-process threading
  covers the required concurrency with headroom (see NFR math above).
  Revisited explicitly in the concurrency phase rather than dropped
  silently.
- Metrics/observability beyond structured logging — its own phase

## Fail-fast vs. fail-safe (carried over from prior work)
- **Fail-fast**: order validation at ingestion/take time, and any
  orchestration invariant violation (e.g. something attempting to queue
  metadata generation for an order whose asset generation hasn't
  succeeded) raises immediately and loudly. These are programming errors,
  not business outcomes — they must never be caught and quietly ignored.
- **Fail-safe, never silently**: a generation job reporting `FAILED` is an
  expected business outcome, handled explicitly, logged with full context
  (order id, stage, reason) and turned into a `FailedOrder`. It is never
  swallowed and never mistaken for an error.
- **Transient failures**: retried with backoff, but retry exhaustion and
  any resulting "unresolved" order must stay visible — tracked in an
  inspectable registry, not just a log line that scrolls away. In
  production, a transient failure that keeps recurring against the same
  downstream dependency isn't "still transient" — it's a dependency that's
  actually down. Retry-with-backoff handles isolated blips; a circuit
  breaker is what handles the dependency-is-down case, so the client stops
  hammering it and fails fast for a cooldown window instead of queuing up
  retries that are all going to fail anyway.

## Concurrency model
Single process. A bounded thread pool where each thread is one worker,
looping `queue.take() -> process(order)` independently. Work is I/O-bound
(simulated delay now, real network calls later), so threads release the
GIL while waiting and scale without multiprocessing overhead at the
current NFR target. Every piece of shared in-memory state that multiple
worker threads touch concurrently — the queue, the idempotency dedup
cache, the result store — needs an explicit locking strategy; this is not
automatic just because it's "in-memory," and is scoped as part of Phase 4
rather than assumed away.

## Incremental roadmap — one phase, one reviewed PR
Each phase ships intentionally incomplete — gaps left for review, fixed in
the next phase based on your feedback, same reverse-shadow pattern as the
feature-flag library drills.

1. **Basic implementation** — the four interfaces + in-memory
   implementations + orchestrator (rules 1-6) + a single-threaded worker
   loop. No retry, no idempotency yet. **DONE — committed.**
2. **Robust retry mechanism** — backoff for transient errors, applied
   uniformly across all four interfaces via one wrapper, not per-call.
   Retry exhaustion after an order is known is recorded in an explicitly
   injected unresolved-order registry so the state remains inspectable.
   **DONE — reviewed and revised.**
3. **Idempotency** — dedup cache for generation triggers and result
   submissions, closing the "retry after an ambiguous timeout" gap.
4. **Concurrency** — multi-threaded worker pool sized per the NFR math,
   **plus an explicit locking strategy for every piece of shared in-memory
   state**: atomic take from `OrderQueue`, a lock around the
   idempotency dedup cache's check-then-act (queue-if-absent), and a lock
   around `ResultPublisher` appends. Also an explicit revisit of whether
   multi-process/distributed is justified yet (it isn't, at this NFR
   target — see concurrency model above).
5. **Caching** — a read-through cache in front of `ArtifactRepository`,
   revisited meaningfully once a real repository/API exists.
6. **Circuit breaker** — wraps `GenerationService` calls; trips on
   sustained failure, fails fast + cooldown instead of retrying into a
   known-down dependency.
7. **Backpressure / bounded intake** — 10K RPM is an assumed ceiling;
   decide what happens when intake exceeds worker capacity (bounded queue
   + explicit reject/park vs. unbounded growth).
8. **Observability** — structured metrics (taken / succeeded /
   failed-by-stage / unresolved counts) so "no error stays silent" is
   actually queryable, not just logged.
9. **Persistence + real API** (later, beyond this exercise's core) —
   DB-backed repository and a real HTTP client, slotted in behind the
   existing interfaces per the OCP design; the payoff of phases 1-8 being
   interface-first is that this phase touches no orchestration code.

## Testing strategy
`pytest`, matching the convention already established for the feature-flag
library. Each phase adds tests scoped to what it introduces; prior-phase
tests (rules 1-6, etc.) are never deleted, only built on. Rules 1-6 are a
natural fit for `@pytest.mark.parametrize` — one table-driven test instead
of near-duplicate methods per rule.

## Deviation log
- **Phase 2 review clarification — unresolved registry lifecycle:** the first
  retry design allowed `worker.run()` to create a registry internally. Review
  exposed that the registry then disappeared on return, violating the plan's
  inspectability requirement. The registry is now an explicit required
  dependency. This is a plan-implementation correction, not a scope change.
- **Roadmap risk discovered — ambiguous `OrderQueue.take()`:** Phase 2 retries
  `take()` uniformly as required, but a destructive remote take can succeed
  server-side and lose its response; retrying may then take a different order.
  Phase 3's generation/submission idempotency does not by itself close this
  gap. Revisit queue semantics in Phase 3: reserve/lease + acknowledgement,
  visibility timeout, or an equivalent recoverable claim protocol. No queue
  protocol is introduced in Phase 2 because the current queue is in-memory.
