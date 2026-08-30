# PLAN.md — orderforge (Artifact Generation Exercise)

## Scope

Build a client that ingests generic `Order`s from a queue, orchestrates two dependent asynchronous generation steps (Asset, then Metadata), and publishes one terminal result per order.

The first implementation is in-memory, while the interfaces are shaped so persistence and real remote APIs can replace those implementations later without rewriting the orchestration state machine.


## NFR targets

- Target throughput: 10,000 orders/min (~167/sec).
- With ~400ms end-to-end latency, Little's Law gives roughly 65-70 orders in flight to sustain that rate.
- Phase 4 introduces a bounded 150-thread worker pool to provide headroom for the current I/O-bound model.
- The current execution model remains single-process and thread-based until a real distributed requirement justifies additional coordination complexity.


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

The retry wrapper is client-side. For mutating calls, the stable idempotency key travels with the request and the downstream service owns the authoritative idempotency record.

That distinction matters for ambiguous outcomes: if the server commits the mutation but its response is lost, a retry with the same key must return or
suppress the already-committed operation rather than perform it again.

OrderQueue.take() is a different problem. A destructive work claim cannot be made recoverable by generation/result idempotency. The in-memory queue remains simple for now. Phase 4 makes its local take atomic across threads, while Phase 9 introduces real reserve/lease + acknowledgement or equivalent visibility semantics when the queue becomes remote/persistent.

**Architecture — decomposed for OCP**

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

The optional idempotency parameter expresses a downstream capability without forcing direct callers to supply one.

The worker composes idempotent wrappers for mutating operations before handing the services to the orchestrator. The orchestrator therefore stays focused on business rules 1-6 rather than idempotency-key creation.

CachingArtifactRepository is a transparent decorator over ArtifactRepository. It preserves the repository interface exactly rather
than requiring callers to adapt to caching-specific assumptions.

Failure model
- Programming / invariant errors

Fail fast and propagate.
- Examples include invalid state, unknown jobs where the contract says the job
must exist, or conflicting reuse of an idempotency key.

Generation FAILED
- This is an expected business outcome.
- Asset generation failure publishes FailedOrder(ASSET) without generated artifacts or metadata.
- Metadata generation failure publishes FailedOrder(METADATA) with generated assets/details but without metadata.

Transient interaction failure
- Retry using exponential backoff with jitter.
- Retry jitter is enabled by default so concurrent workers do not synchronise their retries into a thundering herd.
- max_delay_seconds is a hard upper bound even after jitter is applied.

Retry exhaustion
- If the order is already known, record it in the inspectable UnresolvedOrderRegistry. Do not manufacture a business failure for an
interaction failure.
- Queue-level retry exhaustion is different because no reliable order may have been obtained. Phase 4 coordinates worker shutdown and propagates that error.

Idempotency conflict
- Fail fast.
- Reusing the same idempotency key for a different logical mutation, terminal kind, order, or terminal payload indicates a correctness violation.

Ambiguous mutation response
- Safe only when the downstream mutation participates in idempotency using the stable key supplied by the client.

Concurrency model
- Single process with a bounded thread pool, default 150 workers.

Each worker loops:

queue.take()
    |
    v
process(order)

Shared in-memory state is synchronised at the smallest boundary required to preserve its invariant:

queue claiming
generation idempotency records
generation job/poll state
publisher idempotency records
publisher result collections
unresolved-order tracking
Phase 5 cache entries and in-flight same-key loads

- Locking the complete worker workflow would serialise the worker pool and defeat the concurrency model.
- Long-running dependency calls and complete order workflows therefore remain outside global locks.
- A physical queue entry also does not imply exclusive ownership of a logical order. Duplicate physical queue entries may contain the same logical order and therefore converge on the same idempotent downstream generation job.

Incremental roadmap — one phase, one reviewed PR
1. Basic implementation

Interfaces, in-memory implementations, orchestrator rules 1-6 and single-threaded worker.

Guarantee:

One order follows the correct generation and terminal-result state machine.

DONE — committed.

2. Robust retry

Generic retry/backoff across external-facing interfaces, explicit unresolved registry and dependency-qualified retry errors.

Guarantee:

Temporary interaction failures can be retried without incorrectly converting them into business failures.

DONE — committed.

3. Idempotency

Stable keys for generation triggers and terminal result submissions.

Downstream services own the authoritative idempotency record. Duplicate identical mutations are safe replays; conflicting reuse fails fast.

Mutation idempotency is explicitly separate from queue-claim recoverability.

Guarantee:

Retried mutating requests do not turn duplicate attempts into duplicate side effects.

DONE — reviewed and revised.

4. Concurrency

Bounded worker pool, default 150 via DEFAULT_WORKER_POOL_SIZE, plus explicit synchronisation at shared-state boundaries:

atomic queue take
generation idempotency state
generation job/poll state
publisher idempotency/result state
unresolved-order tracking

Duplicate physical queue entries may represent the same logical order, so downstream state cannot assume exclusive worker ownership.

get_status() therefore protects job lookup and poll-count mutation.

Publisher:

check idempotency
    ->
append terminal result
    ->
record idempotency

remains one atomic transaction.

Publisher result collections are exposed only as locked snapshots.

Retry jitter is enabled by default to avoid synchronised retries, and max_delay_seconds remains a hard cap after jitter.

Queue-level retry exhaustion triggers coordinated worker shutdown.

Durable queue reserve/ack semantics remain Phase 9.

Guarantee:

Many workers can execute the Phase 1-3 pipeline concurrently without queue duplication, shared-job poll races, idempotency races, duplicate terminal
publication or unsafe shared bookkeeping.

DONE — reviewed and corrected.

5. Caching

Shared thread-safe TTL read-through caching around ArtifactRepository reads only.

Queue take, generation operations and terminal publishing remain uncached because they represent ownership, mutation or live state rather than reusable
artifact reads.

Guarantee:

Repeated artifact reads can be served faster without changing the correctness or established freshness semantics of the order-processing pipeline.

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

A cache miss becomes one logical source load whose transient failures remain governed by the existing retry policy.

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

Concurrent misses for the same key therefore use single-flight/request coalescing.

One worker performs the repository load while other workers wait for the same in-flight result.

Different cache keys remain independently loadable and are not serialised behind a global source-load lock.

Stale-while-revalidate is deliberately not used because the Orderforge domain has not established that serving stale artifacts is safe.

Cache miss semantics

None can not safely represent both:

cache miss
and:
cached value is legitimately None

Cache lookup therefore represents hit/miss explicitly rather than using None as the miss sentinel.

This keeps an absent entry distinguishable from a cached None.

Cached-object ownership and immutability

Cached values represent snapshots, not shared mutable domain objects.

Returning the cache-owned object directly would create aliasing:

cache entry ----+
                |
Worker A -------+--> same mutable object
                |
Worker B -------+

If Worker A modified that object, Worker B could observe a value that was never written to the source repository.

CachingArtifactRepository therefore stores a defensive copy and returns a new defensive copy to each caller.

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

deepcopy() is an explicit Phase 5 correctness choice. If copying becomes a material performance cost, immutable domain DTOs would be preferable to
sharing mutable cached objects.

TTL semantics

TTL starts when a successful source load completes, and the value is inserted into the cache.

The default TTL is 30 seconds.

A zero TTL means the committed cache entry expires immediately. Callers already coalesced behind the same in-flight source load may still share the result of that load.

TTL bounds cache residence. It is not being used as a substitute for a domain invalidation protocol.

Freshness and event-based invalidation

No event-based invalidation is introduced in Phase 5.

The current Orderforge state machine waits for the corresponding generation job to reach SUCCESS before reading an artifact:

Asset generation SUCCESS
        |
        v
read Assets / AssetDetails

Metadata generation SUCCESS
        |
        v
read Metadata

The Phase 5 domain assumption is that the corresponding artifact is final once that generation stage reaches SUCCESS.

Under that contract, a valid post-success artifact does not become stale because of another expected state transition.

If future requirements allow assets, asset details or metadata to change after generation SUCCESS, this assumption expires.

That change must introduce an explicit freshness mechanism such as:

event-based invalidation
versioned cache keys
another domain-defined consistency protocol

before mutable post-success artifacts can safely use this cache.

An invalidate() API is deliberately not added speculatively. Invalidation during an in-flight load introduces its own race: the old load could complete
after invalidation and repopulate the supposedly invalidated value. That atomicity should be designed only when the domain actually requires
invalidation.

Decorator transparency

CachingArtifactRepository preserves the ArtifactRepository contract exactly:

get_assets(order_id)
get_asset_detail(asset_id, order_id)
get_metadata(order_id)

Cache-key construction adapts to that contract.

Caching must not force new domain-object assumptions into existing callers.

Cache scope

Cache state is scoped to one run() invocation and shared by the workers in that run.

There is no process-global or distributed cache state in Phase 5.

Distributed cache consistency belongs with later persistence/distributed boundary work.

DONE — reviewed and corrected.

6. Circuit breaker

Protect external-facing operations from repeated calls into dependency
operations already known to be unhealthy.

Guarantee:

A dependency operation that repeatedly exhausts its retry budget is temporarily
removed from the request path. Subsequent workers fail fast rather than
continuing to spend retry capacity on a predictably unhealthy integration
point.

Library decision — build vs reuse

Phase 6 uses PyBreaker rather than implementing the CLOSED / OPEN / HALF_OPEN
state machine inside Orderforge.

Circuit state transitions, failure counters, recovery timing, half-open probing
and thread-safety are commodity resilience mechanisms. Reimplementing them would
add concurrency and lifecycle correctness risk without adding Orderforge domain
value.

Orderforge still owns the decisions specific to this system:

breaker placement relative to retry and cache
breaker granularity / health boundaries
which exceptions represent dependency-health failures
what an open circuit means for an Order
configuration
future metrics and operational visibility

The earlier custom implementation review was still useful because it exposed
the complexity hidden behind a circuit breaker: concurrent state transitions,
half-open probe ownership and stale in-flight completions.

Understanding those mechanisms is useful. Owning another implementation of
them is not required.

Breaker outside retry — logical failure vs physical attempt

Composition:

CircuitBreakingProxy
        |
        v
RetryingProxy
        |
        v
Dependency

The breaker therefore observes the result of one logical dependency operation
after RetryingProxy consumes its retry budget.

For example:

retry max_attempts = 3
breaker failure_threshold = 5

One logical call may make three physical dependency attempts.

If all three attempts fail, RetryingProxy raises one RetryExhaustedError and
PyBreaker records one circuit failure.

The opposite composition:

RetryingProxy
        |
        v
CircuitBreaker
        |
        v
Dependency

would allow individual physical retry attempts to advance circuit health state.

One caller could therefore consume several breaker failures during its own retry
sequence.

Phase 6 intentionally measures dependency health at logical-operation
granularity.

Failure classification — not every exception means dependency failure

Only RetryExhaustedError contributes to circuit health.

RetryExhaustedError represents a transient dependency interaction that remained
unavailable after consuming its retry budget.

That is meaningful evidence of dependency-health degradation.

Validation errors, business errors, idempotency conflicts and invariant /
programming errors propagate normally but do not advance the breaker.

Conceptually:

retry-exhausted interaction failure
    ->
dependency-health signal

business / validation / invariant failure
    ->
propagate
    ->
not a dependency-health signal

Opening an availability circuit because of invalid application input would
misclassify correctness failure as dependency failure.

CircuitOpenError — availability control signal, not business outcome

PyBreaker's CircuitBreakerError is translated at the Orderforge boundary into
CircuitOpenError.

Orderforge therefore does not leak a third-party library exception through its
worker/orchestration contracts.

For an Order already acquired from the queue:

RetryExhaustedError
CircuitOpenError
        |
        v
UnresolvedOrderRegistry

Neither condition creates FailedOrder.

A circuit being open means the dependency operation was deliberately not
attempted. It does not prove that Asset or Metadata generation reached its
FAILED business state.

For OrderQueue.take(), no reliable Order has yet been acquired. An open queue
circuit therefore follows the existing queue-level failure path and triggers
coordinated worker shutdown.

Breaker granularity — shared across workers, isolated by operation

Breaker instances are shared by all workers but isolated by logical dependency
operation.

Current operation boundaries:

order_queue.take

asset_generation.queue
asset_generation.get_status

metadata_generation.queue
metadata_generation.get_status

artifact_repository.get_assets
artifact_repository.get_asset_detail
artifact_repository.get_metadata

result_publisher.submit_shippable
result_publisher.submit_failed

Conceptually:

all workers
    |
    v
same dependency operation
    |
    v
same breaker

A breaker per worker or per Order would be ineffective because each worker
would independently rediscover the same outage.

A single breaker for an entire interface can create unnecessary blast radius.

For example:

artifact_repository.get_metadata fails
        |
        X
should not automatically imply
        |
artifact_repository.get_assets is unavailable

Phase 6 therefore starts with operation-level breakers.

Health boundaries can later be consolidated if production behaviour proves
several operations always fail and recover together.

Cache interaction

Repository composition becomes:

CachingArtifactRepository
          |
          | cache miss
          v
CircuitBreakingProxy
          |
          v
RetryingProxy
          |
          v
ArtifactRepository

A cache hit therefore avoids:

repository access
retry
circuit-breaker health accounting

Only a real dependency interaction participates in circuit-health decisions.

This preserves the Phase 5 principle that cached reads should not consume
resilience capacity intended for remote dependency interactions.

Idempotency interaction

The existing Phase 3 mutation idempotency remains unchanged.

The current composition is:

CircuitBreakingProxy
        |
        v
RetryingProxy
        |
        v
IdempotentGenerationService / IdempotentResultPublisher
        |
        v
Dependency

Retry continues to reuse the stable idempotency key established in Phase 3.

Circuit breaking decides whether an interaction should currently be attempted.
It does not solve ambiguous mutation outcomes and does not replace downstream
authoritative idempotency.

Shared-state scope

Circuit breaker instances are created once inside run() and shared by the
worker pool for that run.

They are not created per worker or per Order.

Phase 6 remains single-process. Circuit health is therefore process-local.

Distributed breaker state is deliberately not introduced. Whether breaker
health should be shared between processes is a separate operational decision
rather than an automatic improvement.

Recovery semantics

CLOSED

Calls flow normally.

Once the configured logical failure threshold is reached, PyBreaker opens the
circuit.

OPEN

Calls fail fast without invoking the protected operation.

After the configured recovery timeout, PyBreaker manages the HALF_OPEN recovery
behaviour.

HALF_OPEN

Controlled recovery traffic determines whether the dependency has recovered.

Successful recovery closes the circuit.

Failed recovery opens it again.

Orderforge delegates these concurrency-sensitive state transitions to
PyBreaker.

DONE — implemented using PyBreaker.

7. Backpressure

Bound intake/work queues and define reject/park/fairness behaviour when offered load exceeds worker capacity.

Guarantee:

Offered load above processing capacity does not create unbounded resource growth.

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

The system's correctness, reliability and performance guarantees can be observed rather than inferred from logs after failure.

9. Persistence + real API/queue

Replace in-memory adapters with persistent repository/remote clients.

Add durable queue reserve/lease + ack/visibility semantics and durable downstream idempotency records where required.

Guarantee:

The guarantees established in earlier phases survive real network ambiguity, process failure and restart boundaries.

Testing strategy

Use pytest.

Each phase adds behavioural contracts while retaining all earlier tests.

Tests should prove semantics rather than merely execute lines of code.

Failure-path tests must model ambiguous outcomes explicitly, including cases where the downstream side effect succeeds but the response is lost.

Concurrency tests should force the relevant internal race window rather than assuming that starting threads at approximately the same time proves
atomicity.

A green suite does not by itself prove interface conformance if the relevant path or contract is insufficiently exercised. Interface definitions remain an
independent source of truth during decorator/adapter review.

Deviation/decision log
Phase 2 — unresolved registry lifecycle

An internally-created registry became unreachable after run() returned.

It is now an explicit dependency so unresolved orders remain inspectable by the caller.

Phase 2 — ambiguous OrderQueue.take()

Retrying a destructive remote take() could consume Order A, lose its response, retry, and then return Order B.

Generation/result idempotency cannot repair this work-claim ambiguity.

The in-memory implementation remains simple. Durable reserve/lease + ack or equivalent visibility semantics are deferred until Phase 9.

Phase 3 — client cache rejected as strong idempotency

The first design wrote a local dedup entry only after the downstream mutation returned.

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

The fix moves authoritative idempotency participation to the downstream mutation contract and sends a stable key with every retry.

Phase 3 — duplicate vs conflict

Identical replay with the same idempotency key is a safe no-op/reuse.

The same key with a different order, terminal kind or payload is an IdempotencyConflictError and fails fast.

Phase 3 — queue claim decision

Mutation idempotency does not solve work claiming.

Phase 4 provides thread-safe local claiming.

Durable reserve/ack or visibility-timeout semantics belong to Phase 9 when the queue becomes real.

Phase 4 — physical vs logical ownership

One worker owning a physical queue entry does not guarantee exclusive ownership of the logical order or its downstream state.

Duplicate physical entries can represent the same logical order and therefore converge on the same idempotent generation job.

Shared job polling state must consequently remain synchronised.

Phase 4 — lock scope

Synchronise the smallest shared-state transition required to preserve the invariant rather than the complete workflow.

Locking _worker_loop() would make the 150-thread pool effectively serial.

For publisher state:

check idempotency
    ->
append result
    ->
record idempotency

is one atomic unit.

Helper methods invoked while that ordinary Lock is held must not reacquire the same lock or they would deadlock.

Phase 4 — generation polling

get_status() originally relied on the assumption that one worker exclusively owned a generation job.

Idempotency invalidates that assumption because duplicate logical orders can map multiple workers onto the same job ID.

Job lookup and polls_seen mutation are therefore protected together.

Phase 4 — retry jitter

Jitter initially defaulted to zero, which defeated its purpose under concurrent retry load.

Jitter is now enabled by default.

The final jittered delay is also capped by max_delay_seconds; the configured maximum represents the actual maximum sleep rather than only the pre-jitter
base delay.

Phase 4 — deterministic concurrency testing

Placing a barrier immediately before a production method call does not prove that threads collide inside the actual check/mutate race window.

Concurrency tests therefore coordinate the observed internal state transition where necessary so an unlocked implementation would deterministically expose
the race.

Phase 4 — publisher snapshots

Public mutable result lists allowed callers to inspect or mutate shared state outside the publisher lock.

The publisher now keeps result collections private and exposes locked snapshots.

Phase 5 — cache stampede

Thread-safe cache get() and put() do not make the compound:

miss -> source load -> put

operation safe from duplicate loading.

Same-key misses therefore use single-flight/request coalescing while unrelated keys remain concurrent.

Phase 5 — stale-while-revalidate decision

Stale-while-revalidate was considered but rejected for the current phase.

It intentionally serves stale data while refreshing in the background, but Orderforge has no domain contract permitting stale artifact reads.

Request coalescing provides duplicate-load protection without weakening freshness semantics.

Phase 5 — freshness contract

Event-based invalidation was considered but deliberately not added.

The current domain assumption is that artifacts become final once their corresponding generation stage reaches SUCCESS.

TTL bounds cache residence rather than repairing post-success mutations.

If that assumption changes, explicit invalidation, versioned keys or another defined consistency mechanism becomes required.

Phase 5 — cached-object ownership

Returning the cache-owned mutable object would allow one caller to change what later callers observe without any source/cache write.

Cached artifacts are therefore defensive snapshots.

Immutable domain objects are a possible later optimisation if defensive copying becomes materially expensive.

Phase 5 — cache miss semantics

None cannot safely mean both:

not cached

and:

cached None

Cache lookup therefore carries explicit hit/miss state.

Phase 5 — retry/cache ordering

Caching wraps retry rather than retry wrapping caching.

A hit avoids the dependency/retry path completely.

A miss represents one coalesced logical load, while RetryingProxy owns transient dependency retries underneath it.

Only a successful source result enters the cache.

Phase 5 — decorator transparency

The first caching wrapper incorrectly assumed repository arguments were domain objects exposing order_id / asset_id.

The actual ArtifactRepository contract accepts string IDs:

get_assets(order_id)
get_asset_detail(asset_id, order_id)
get_metadata(order_id)

The decorator was corrected to preserve that interface exactly rather than forcing caching-specific assumptions onto existing callers.
reinforced that passing tests do not independently prove interface conformance
when a relevant path is insufficiently exercised.

Phase 6 — build vs reuse

The first Phase 6 design explored implementing the circuit-breaker state machine
inside Orderforge.

Reviewing that approach exposed significant concurrency and lifecycle
complexity, including CLOSED / OPEN / HALF_OPEN transitions, recovery-probe
ownership and stale in-flight completions.

That implementation direction was deliberately discarded before commit.

Phase 6 instead uses PyBreaker and limits Orderforge code to integration policy.

The design lesson is to distinguish understanding an infrastructure mechanism
from needing to own its implementation.

Build vs reuse should be an explicit engineering decision.


Phase 6 — retry vs circuit-breaker ordering

CircuitBreakingProxy wraps RetryingProxy.

The breaker therefore sees one RetryExhaustedError after a complete retry
sequence as one failed logical operation.

It does not count every physical retry attempt as an independent breaker
failure.

This prevents one caller's retry loop from consuming several breaker failures.


Phase 6 — exception classification

Not every exception means that a dependency is unhealthy.

Only RetryExhaustedError contributes to circuit health.

Validation, business, idempotency and invariant errors propagate without
advancing the circuit.

This avoids opening an availability circuit because of application-level
correctness failures.


Phase 6 — breaker granularity

Breaker state is shared across workers but isolated by logical dependency
operation.

A per-worker breaker would allow every worker to independently rediscover the
same outage.

A single breaker across unrelated operations can create unnecessary blast
radius by allowing one failing endpoint to block another healthy endpoint.

Phase 6 therefore starts with operation-level breakers.

Those health boundaries can be consolidated later if production evidence shows
that several operations always fail and recover together.


Phase 6 — open circuit is unresolved, not FAILED

CircuitOpenError means Orderforge deliberately did not invoke a dependency
because recent interaction history indicates that operation is unhealthy.

It does not prove Asset or Metadata generation reached its FAILED domain state.

For an already-known Order, the condition is recorded in
UnresolvedOrderRegistry.

For OrderQueue.take(), where no Order has safely been acquired, the condition
is treated as an intake-level worker failure.


Phase 6 — circuit breaker does not provide backpressure

Fail-fast protection prevents workers from repeatedly spending retry capacity
on a known-unhealthy operation.

It does not control how quickly new work is acquired.

If workers continue taking Orders while a downstream circuit is open, many
Orders may rapidly become unresolved.

Controlling intake when offered load or downstream capacity is insufficient is
a separate concern and remains Phase 7 backpressure.

