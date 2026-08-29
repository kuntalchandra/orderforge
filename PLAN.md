# PLAN.md — orderforge (Artifact Generation Exercise)

## Scope
Build a client that ingests generic `Order`s from a queue, orchestrates two
dependent asynchronous generation steps (Asset, then Metadata), and publishes
one terminal result per order. The first implementation is in-memory, while the
interfaces are shaped so persistence and real remote APIs can replace those
implementations later without rewriting the orchestration state machine.

## NFR targets
- Target throughput: 10,000 orders/min (~167/sec).
- With ~400ms end-to-end latency, Little's Law gives roughly 65-70 orders in
  flight to sustain that rate.
- Phase 4 targets a bounded 150-thread worker pool to provide headroom for the
  current I/O-bound model.

## End-to-end target flow

```text
OrderQueue
   |
   | take / later reserve+ack
   v
Worker
   |
   | process one Order
   v
Asset Generation
   |  stable idempotency key: generation:asset:<order_id>
   |  retry transient interaction failures
   |  poll job until SUCCESS / FAILED
   |
   +-- FAILED -----------------------------> FailedOrder(ASSET)
   |
   v SUCCESS
ArtifactRepository
   |  get assets + asset details
   v
Metadata Generation
   |  stable idempotency key: generation:metadata:<order_id>
   |  retry transient interaction failures
   |  poll job until SUCCESS / FAILED
   |
   +-- FAILED -----------------------------> FailedOrder(METADATA)
   |
   v SUCCESS
ArtifactRepository
   |  get metadata
   v
ShippableOrder
   |
   | stable idempotency key: result:<order_id>
   v
ResultPublisher

The retry wrapper is client-side. For mutating calls, the stable idempotency
key travels with the request and the downstream service owns the authoritative
idempotency record. That distinction matters for ambiguous outcomes: if the
server commits the mutation but its response is lost, a retry with the same key
must return/suppress the already-committed operation rather than perform it
again.

OrderQueue.take() is a different problem. A destructive work claim cannot be
made recoverable by generation/result idempotency. The in-memory queue remains
simple for now; Phase 4 makes its local take atomic across threads, while Phase
9 introduces real reserve/lease + acknowledgement or equivalent visibility
semantics when the queue becomes remote/persistent.

Architecture — decomposed for OCP

The orchestrator depends on four focused abstractions:

OrderQueue — take() -> Order | None
GenerationService — queue(order, idempotency_key=None) -> job_id,
get_status(job_id). Asset and Metadata use separate instances.
ArtifactRepository — get_assets, get_asset_detail, get_metadata
ResultPublisher — submit_shippable(..., idempotency_key=None),
submit_failed(..., idempotency_key=None)

The optional idempotency parameter expresses a downstream capability without
forcing direct callers to supply one. The worker composes idempotent wrappers
for the mutating operations before handing the services to the orchestrator.
The orchestrator therefore stays focused on rules 1-6 rather than key creation.

Failure model
Programming/invariant errors: fail fast and propagate.
Generation FAILED: expected business outcome; publish the appropriate
FailedOrder according to rules 4/5.
Transient interaction errors: retry with exponential backoff.
Retry exhaustion after an order is known: record the order in the
inspectable unresolved registry; do not manufacture a business failure.
Idempotency conflict: fail fast. Reusing the same key for a different
logical mutation or terminal payload indicates a correctness violation.
Ambiguous mutation response: safe only when the downstream mutation
participates in idempotency with the stable key supplied by the client.
Concurrency model

Single process. Phase 4 introduces a bounded thread pool where each thread loops
queue.take() -> process(order). Shared in-memory state then requires explicit
locking: queue claiming, generation idempotency records, result idempotency
records/result lists, and the unresolved registry. Locking is not pulled into
Phase 3 because the worker is still single-threaded.

Incremental roadmap — one phase, one reviewed PR
Basic implementation — interfaces, in-memory implementations,
orchestrator rules 1-6, single-threaded worker. DONE — committed.
Robust retry — generic retry/backoff across external-facing interfaces;
explicit unresolved registry and dependency-qualified retry errors.
DONE — committed.
Idempotency — stable keys for generation triggers and terminal result
submissions; downstream services own the authoritative idempotency record;
duplicate identical mutations are safe, conflicting reuse fails fast.
Explicitly separate mutation idempotency from queue-claim recoverability.
DONE — reviewed and revised.
Concurrency — bounded worker pool (default 150 via `DEFAULT_WORKER_POOL_SIZE`) 
plus explicit synchronization at shared-state boundaries: atomic queue take, 
generation idempotency/job-poll state,
publisher idempotency/result state, and unresolved-order tracking. Duplicate 
physical queue entries may represent the same logical order, so downstream state 
cannot assume exclusive worker ownership. `get_status()` therefore protects job 
lookup and poll-count mutation. Publisher check → append → idempotency-record 
remains one atomic transaction, while result collections are exposed only as locked
snapshots. Retry jitter is enabled by default to avoid synchronized retries and 
`max_delay_seconds` remains a hard cap after jitter. Queue-level retry exhaustion 
triggers coordinated worker shutdown. Durable queue reserve/ack semantics remain Phase 9.
DONE — reviewed and corrected.
Circuit breaker — stop retrying into a known-down dependency; define
closed/open/half-open transitions and interaction with retry.
Backpressure — bound intake/work queues and define reject/park/fairness
behaviour when offered load exceeds worker capacity.
Observability — structured metrics for taken, succeeded, failed by stage,
retry exhaustion, idempotency conflicts, latency, and unresolved orders.
Persistence + real API/queue — replace in-memory adapters with persistent
repository/remote clients. Add durable queue reserve/lease + ack/visibility
semantics and durable downstream idempotency records where required.
Testing strategy

pytest. Each phase adds behavioural contracts and retains earlier tests.
Failure-path tests must model ambiguous outcomes explicitly, not only ordinary
pre-side-effect exceptions.

Deviation / decision log
Phase 2 — unresolved registry lifecycle: an internally-created registry
became unreachable after run() returned. It is now an explicit dependency.
Phase 2 — ambiguous OrderQueue.take(): retrying a destructive remote
take could lose Order A and then return Order B. Carried into Phase 3 for an
architectural decision rather than hidden by generic retry.
Phase 3 — client cache rejected as strong idempotency: the first design
wrote a local dedup entry only after the downstream call returned. If the
server committed and the response was lost, retry saw no local entry and
repeated the mutation. The fix moves authoritative idempotency participation
to the downstream mutation contract and sends stable keys with retries.
Phase 3 — duplicate vs conflict: identical replay with the same key is a
no-op/reuse; the same key with a different order, terminal kind, or payload is
an IdempotencyConflictError and fails fast.
Phase 3 — queue claim decision: mutation idempotency does not solve work
claiming. Phase 4 provides thread-safe local claiming; durable reserve/ack or
visibility-timeout semantics belong to Phase 9 when the queue becomes real.
