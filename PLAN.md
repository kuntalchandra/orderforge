# PLAN.md — orderforge (Artifact Generation Exercise)

## Scope

Build a client that ingests generic `Order`s from a queue, orchestrates two
dependent asynchronous generation steps (Asset, then Metadata), and publishes
one terminal result per order.

The first implementation is in-memory, while the interfaces are shaped so
persistence and real remote APIs can replace those implementations later
without rewriting the orchestration state machine.


## NFR targets

- Target throughput: 10,000 orders/min (~167/sec).
- With ~400ms end-to-end latency, Little's Law gives roughly 65-70 orders in
  flight to sustain that rate.
- Phase 4 introduces a bounded 150-thread worker pool to provide headroom for
  the current I/O-bound model.
- The current execution model remains single-process and thread-based until a
  real distributed requirement justifies additional coordination complexity.


## End-to-end target flow

```text
OrderQueue
   |
   | take / later reserve+ack
   v
Worker Pool
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
CachingArtifactRepository
   |
   |  cache hit -> return defensive snapshot
   |
   |  cache miss
   v
Retrying ArtifactRepository
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
CachingArtifactRepository
   |
   |  cache hit -> return defensive snapshot
   |
   |  cache miss
   v
Retrying ArtifactRepository
   |  get metadata
   v
ShippableOrder
   |
   | stable idempotency key: result:<order_id>
   v
ResultPublisher

The retry wrapper is client-side. For mutating calls, the stable idempotency
key travels with the request and the downstream service owns the authoritative
idempotency record.

That distinction matters for ambiguous outcomes: if the server commits the
mutation but its response is lost, a retry with the same key must return or
suppress the already-committed operation rather than perform it again.

OrderQueue.take() is a different problem. A destructive work claim cannot be
made recoverable by generation/result idempotency. The in-memory queue remains
simple for now. Phase 4 makes its local take atomic across threads, while Phase
9 introduces real reserve/lease + acknowledgement or equivalent visibility
semantics when the queue becomes remote/persistent.

Architecture — decomposed for OCP

The orchestrator depends on four focused abstractions:

OrderQueue
    take() -> Order | None

GenerationService
    queue(order, idempotency_key=None) -> job_id
    get_status(job_id)

    Asset and Metadata use separate instances.

ArtifactRepository
    get_assets(order_id)
    get_asset_detail(asset_id, order_id)
    get_metadata(order_id)

ResultPublisher
    submit_shippable(..., idempotency_key=None)
    submit_failed(..., idempotency_key=None)

The optional idempotency parameter expresses a downstream capability without
forcing direct callers to supply one.

The worker composes idempotent wrappers for mutating operations before handing
the services to the orchestrator. The orchestrator therefore stays focused on
business rules 1-6 rather than idempotency-key creation.

CachingArtifactRepository is a transparent decorator over
ArtifactRepository. It preserves the repository interface exactly rather
than requiring callers to adapt to caching-specific assumptions.

Failure model
Programming / invariant errors

Fail fast and propagate.

Examples include invalid state, unknown jobs where the contract says the job
must exist, or conflicting reuse of an idempotency key.

Generation FAILED

This is an expected business outcome.

Asset generation failure publishes FailedOrder(ASSET) without generated
artifacts or metadata.
Metadata generation failure publishes FailedOrder(METADATA) with generated
assets/details but without metadata.
Transient interaction failure

Retry using exponential backoff with jitter.

Retry jitter is enabled by default so concurrent workers do not synchronize
their retries into a thundering herd.

max_delay_seconds is a hard upper bound even after jitter is applied.

Retry exhaustion

If the order is already known, record it in the inspectable
UnresolvedOrderRegistry. Do not manufacture a business failure for an
interaction failure.

Queue-level retry exhaustion is different because no reliable order may have
been obtained. Phase 4 coordinates worker shutdown and propagates that error.

Idempotency conflict

Fail fast.

Reusing the same idempotency key for a different logical mutation, terminal
kind, order or terminal payload indicates a correctness violation.

Ambiguous mutation response

Safe only when the downstream mutation participates in idempotency using the
stable key supplied by the client.

Concurrency model

Single process with a bounded thread pool, default 150 workers.

Each worker loops:

queue.take()
    |
    v
process(order)

Shared in-memory state is synchronized at the smallest boundary required to
preserve its invariant:

queue claiming
generation idempotency records
generation job/poll state
publisher idempotency records
publisher result collections
unresolved-order tracking
Phase 5 cache entries and in-flight same-key loads

Locking the complete worker workflow would serialize the worker pool and defeat
the concurrency model.

Long-running dependency calls and complete order workflows therefore remain
outside global locks.

A physical queue entry also does not imply exclusive ownership of a logical
order. Duplicate physical queue entries may contain the same logical order and
therefore converge on the same idempotent downstream generation job.

Incremental roadmap — one phase, one reviewed PR
1. Basic implementation

Interfaces, in-memory implementations, orchestrator rules 1-6 and
single-threaded worker.

Guarantee:

One order follows the correct generation and terminal-result state machine.

DONE — committed.

2. Robust retry

Generic retry/backoff across external-facing interfaces, explicit unresolved
registry and dependency-qualified retry errors.

Guarantee:

Temporary interaction failures can be retried without incorrectly converting
them into business failures.

DONE — committed.

3. Idempotency

Stable keys for generation triggers and terminal result submissions.

Downstream services own the authoritative idempotency record. Duplicate
identical mutations are safe replays; conflicting reuse fails fast.

Mutation idempotency is explicitly separate from queue-claim recoverability.

Guarantee:

Retried mutating requests do not turn duplicate attempts into duplicate
side effects.

DONE — reviewed and revised.

4. Concurrency

Bounded worker pool, default 150 via DEFAULT_WORKER_POOL_SIZE, plus explicit
synchronization at shared-state boundaries:

atomic queue take
generation idempotency state
generation job/poll state
publisher idempotency/result state
unresolved-order tracking

Duplicate physical queue entries may represent the same logical order, so
downstream state cannot assume exclusive worker ownership.

get_status() therefore protects job lookup and poll-count mutation.

Publisher:

check idempotency
    ->
append terminal result
    ->
record idempotency

remains one atomic transaction.

Publisher result collections are exposed only as locked snapshots.

Retry jitter is enabled by default to avoid synchronized retries and
max_delay_seconds remains a hard cap after jitter.

Queue-level retry exhaustion triggers coordinated worker shutdown.

Durable queue reserve/ack semantics remain Phase 9.

Guarantee:

Many workers can execute the Phase 1-3 pipeline concurrently without queue
duplication, shared-job poll races, idempotency races, duplicate terminal
publication or unsafe shared bookkeeping.

DONE — reviewed and corrected.

5. Caching

Shared thread-safe TTL read-through caching around ArtifactRepository reads
only.

Queue take, generation operations and terminal publishing remain uncached
because they represent ownership, mutation or live state rather than reusable
artifact reads.

Guarantee:

Repeated artifact reads can be served faster without changing the correctness
or established freshness semantics of the order-processing pipeline.

Wrapper ordering

The cache wraps the repository RetryingProxy:

CachingArtifactRepository
          |
          | cache miss
          v
     RetryingProxy
          |
          v
ArtifactRepository

A cache hit therefore bypasses both the dependency call and retry machinery.

A cache miss becomes one logical source load whose transient failures remain
governed by the existing retry policy.

Failed source loads are not cached.

Concurrent cache misses

Thread-safe get() and put() individually are not sufficient because:

get -> miss
load from source
put

is itself a compound operation.

Without additional coordination:

Worker A -> miss -> repository
Worker B -> miss -> repository
Worker C -> miss -> repository

would create a cache stampede.

Concurrent misses for the same key therefore use single-flight/request
coalescing.

One worker performs the repository load while other workers wait for the same
in-flight result.

Different cache keys remain independently loadable and are not serialized
behind a global source-load lock.

Stale-while-revalidate is deliberately not used because the Orderforge domain
has not established that serving stale artifacts is safe.

Cache miss semantics

None cannot safely represent both:

cache miss

and:

cached value is legitimately None

Cache lookup therefore represents hit/miss explicitly rather than using
None as the miss sentinel.

This keeps an absent entry distinguishable from a cached None.

Cached-object ownership and immutability

Cached values represent snapshots, not shared mutable domain objects.

Returning the cache-owned object directly would create aliasing:

cache entry ----+
                |
Worker A -------+--> same mutable object
                |
Worker B -------+

If Worker A modified that object, Worker B could observe a value that was never
written to the source repository.

CachingArtifactRepository therefore stores a defensive copy and returns a
new defensive copy to each caller.

Conceptually:

repository value
      |
      | defensive copy
      v
cache-owned snapshot
      |
      | defensive copy
      v
caller-owned value

deepcopy() is an explicit Phase 5 correctness choice. If copying becomes a
material performance cost, immutable domain DTOs would be preferable to
sharing mutable cached objects.

TTL semantics

TTL starts when a successful source load completes and the value is inserted
into the cache.

The default TTL is 30 seconds.

A zero TTL means the committed cache entry expires immediately. Callers already
coalesced behind the same in-flight source load may still share the result of
that load.

TTL bounds cache residence. It is not being used as a substitute for a domain
invalidation protocol.

Freshness and event-based invalidation

No event-based invalidation is introduced in Phase 5.

The current Orderforge state machine waits for the corresponding generation
job to reach SUCCESS before reading an artifact:

Asset generation SUCCESS
        |
        v
read Assets / AssetDetails

Metadata generation SUCCESS
        |
        v
read Metadata

The Phase 5 domain assumption is that the corresponding artifact is final once
that generation stage reaches SUCCESS.

Under that contract, a valid post-success artifact does not become stale
because of another expected state transition.

If future requirements allow assets, asset details or metadata to change after
generation SUCCESS, this assumption expires.

That change must introduce an explicit freshness mechanism such as:

event-based invalidation
versioned cache keys
another domain-defined consistency protocol

before mutable post-success artifacts can safely use this cache.

An invalidate() API is deliberately not added speculatively. Invalidation
during an in-flight load introduces its own race: the old load could complete
after invalidation and repopulate the supposedly invalidated value. That
atomicity should be designed only when the domain actually requires
invalidation.

Decorator transparency

CachingArtifactRepository preserves the ArtifactRepository contract
exactly:

get_assets(order_id)
get_asset_detail(asset_id, order_id)
get_metadata(order_id)

Cache-key construction adapts to that contract.

Caching must not force new domain-object assumptions into existing callers.

Cache scope

Cache state is scoped to one run() invocation and shared by the workers in
that run.

There is no process-global or distributed cache state in Phase 5.

Distributed cache consistency belongs with later persistence/distributed
boundary work.

DONE — reviewed and corrected.

6. Circuit breaker

Stop retrying into a known-down dependency.

Define closed/open/half-open transitions and interaction with retry.

Guarantee:

A known-unhealthy dependency does not cause every worker to continue spending
resources on predictably failing calls.

7. Backpressure

Bound intake/work queues and define reject/park/fairness behaviour when offered
load exceeds worker capacity.

Guarantee:

Offered load above processing capacity does not create unbounded resource
growth.

8. Observability

Structured metrics for:

taken
succeeded
failed by stage
retry exhaustion
idempotency conflicts
latency
unresolved orders
relevant concurrency/cache behaviour

Guarantee:

The system's correctness, reliability and performance guarantees can be
observed rather than inferred from logs after failure.

9. Persistence + real API/queue

Replace in-memory adapters with persistent repository/remote clients.

Add durable queue reserve/lease + ack/visibility semantics and durable
downstream idempotency records where required.

Guarantee:

The guarantees established in earlier phases survive real network ambiguity,
process failure and restart boundaries.

Testing strategy

Use pytest.

Each phase adds behavioural contracts while retaining all earlier tests.

Tests should prove semantics rather than merely execute lines of code.

Failure-path tests must model ambiguous outcomes explicitly, including cases
where the downstream side effect succeeds but the response is lost.

Concurrency tests should force the relevant internal race window rather than
assuming that starting threads at approximately the same time proves
atomicity.

A green suite does not by itself prove interface conformance if the relevant
path or contract is insufficiently exercised. Interface definitions remain an
independent source of truth during decorator/adapter review.

Deviation / decision log
Phase 2 — unresolved registry lifecycle

An internally-created registry became unreachable after run() returned.

It is now an explicit dependency so unresolved orders remain inspectable by the
caller.

Phase 2 — ambiguous OrderQueue.take()

Retrying a destructive remote take() could consume Order A, lose its
response, retry, and then return Order B.

Generation/result idempotency cannot repair this work-claim ambiguity.

The in-memory implementation remains simple. Durable reserve/lease + ack or
equivalent visibility semantics are deferred until Phase 9.

Phase 3 — client cache rejected as strong idempotency

The first design wrote a local dedup entry only after the downstream mutation
returned.

If the server committed and its response was lost:

local idempotency miss
    ->
server commits mutation
    ->
response lost
    ->
local record never written
    ->
retry
    ->
duplicate server mutation

The fix moves authoritative idempotency participation to the downstream
mutation contract and sends a stable key with every retry.

Phase 3 — duplicate vs conflict

Identical replay with the same idempotency key is a safe no-op/reuse.

The same key with a different order, terminal kind or payload is an
IdempotencyConflictError and fails fast.

Phase 3 — queue claim decision

Mutation idempotency does not solve work claiming.

Phase 4 provides thread-safe local claiming.

Durable reserve/ack or visibility-timeout semantics belong to Phase 9 when the
queue becomes real.

Phase 4 — physical vs logical ownership

One worker owning a physical queue entry does not guarantee exclusive ownership
of the logical order or its downstream state.

Duplicate physical entries can represent the same logical order and therefore
converge on the same idempotent generation job.

Shared job polling state must consequently remain synchronized.

Phase 4 — lock scope

Synchronize the smallest shared-state transition required to preserve the
invariant rather than the complete workflow.

Locking _worker_loop() would make the 150-thread pool effectively serial.

For publisher state:

check idempotency
    ->
append result
    ->
record idempotency

is one atomic unit.

Helper methods invoked while that ordinary Lock is held must not reacquire
the same lock or they would deadlock.

Phase 4 — generation polling

get_status() originally relied on the assumption that one worker exclusively
owned a generation job.

Idempotency invalidates that assumption because duplicate logical orders can
map multiple workers onto the same job ID.

Job lookup and polls_seen mutation are therefore protected together.

Phase 4 — retry jitter

Jitter initially defaulted to zero, which defeated its purpose under concurrent
retry load.

Jitter is now enabled by default.

The final jittered delay is also capped by max_delay_seconds; the configured
maximum represents the actual maximum sleep rather than only the pre-jitter
base delay.

Phase 4 — deterministic concurrency testing

Placing a barrier immediately before a production method call does not prove
that threads collide inside the actual check/mutate race window.

Concurrency tests therefore coordinate the observed internal state transition
where necessary so an unlocked implementation would deterministically expose
the race.

Phase 4 — publisher snapshots

Public mutable result lists allowed callers to inspect or mutate shared state
outside the publisher lock.

The publisher now keeps result collections private and exposes locked
snapshots.

Phase 5 — cache stampede

Thread-safe cache get() and put() do not make the compound:

miss -> source load -> put

operation safe from duplicate loading.

Same-key misses therefore use single-flight/request coalescing while unrelated
keys remain concurrent.

Phase 5 — stale-while-revalidate decision

Stale-while-revalidate was considered but rejected for the current phase.

It intentionally serves stale data while refreshing in the background, but
Orderforge has no domain contract permitting stale artifact reads.

Request coalescing provides duplicate-load protection without weakening
freshness semantics.

Phase 5 — freshness contract

Event-based invalidation was considered but deliberately not added.

The current domain assumption is that artifacts become final once their
corresponding generation stage reaches SUCCESS.

TTL bounds cache residence rather than repairing post-success mutations.

If that assumption changes, explicit invalidation, versioned keys or another
defined consistency mechanism becomes required.

Phase 5 — cached-object ownership

Returning the cache-owned mutable object would allow one caller to change what
later callers observe without any source/cache write.

Cached artifacts are therefore defensive snapshots.

Immutable domain objects are a possible later optimisation if defensive copying
becomes materially expensive.

Phase 5 — cache miss semantics

None cannot safely mean both:

not cached

and:

cached None

Cache lookup therefore carries explicit hit/miss state.

Phase 5 — retry/cache ordering

Caching wraps retry rather than retry wrapping caching.

A hit avoids the dependency/retry path completely.

A miss represents one coalesced logical load, while RetryingProxy owns
transient dependency retries underneath it.

Only a successful source result enters the cache.

Phase 5 — decorator transparency

The first caching wrapper incorrectly assumed repository arguments were domain
objects exposing order_id / asset_id.

The actual ArtifactRepository contract accepts string IDs:

get_assets(order_id)
get_asset_detail(asset_id, order_id)
get_metadata(order_id)

The decorator was corrected to preserve that interface exactly rather than
forcing caching-specific assumptions onto existing callers.

The test suite was green before the final signature mismatch was noticed. This
reinforced that passing tests do not independently prove interface conformance
when a relevant path is insufficiently exercised.
