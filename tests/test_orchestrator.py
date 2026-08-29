import pytest

from orderforge.exceptions import PollExhaustedError, UnknownJobError
from orderforge.in_memory.generation import InMemoryGenerationService
from orderforge.in_memory.publisher import InMemoryResultPublisher
from orderforge.in_memory.queue import InMemoryOrderQueue
from orderforge.in_memory.repository import InMemoryArtifactRepository
from orderforge.models import FailureStage, Order
from orderforge.orchestrator import PollConfig, process_order
from orderforge.worker import UnresolvedOrderRegistry, run


def make_services(asset_should_fail=lambda order: False, metadata_should_fail=lambda order: False):
    asset_svc = InMemoryGenerationService(should_fail=asset_should_fail)
    metadata_svc = InMemoryGenerationService(should_fail=metadata_should_fail)
    repo = InMemoryArtifactRepository()
    pub = InMemoryResultPublisher()
    return asset_svc, metadata_svc, repo, pub


def test_success_produces_shippable_order_with_full_data():
    order = Order(order_id="o1")
    asset_svc, metadata_svc, repo, pub = make_services()

    process_order(order, asset_svc, metadata_svc, repo, pub)

    assert len(pub.shippable_orders) == 1
    assert len(pub.failed_orders) == 0
    so = pub.shippable_orders[0]
    assert so.order_id == "o1"
    assert len(so.assets) == 2
    assert len(so.asset_details) == 2
    assert so.metadata.xml


def test_asset_failure_produces_failed_order_with_no_asset_or_metadata_data():
    order = Order(order_id="o2")
    asset_svc, metadata_svc, repo, pub = make_services(asset_should_fail=lambda o: True)

    process_order(order, asset_svc, metadata_svc, repo, pub)

    assert len(pub.failed_orders) == 1
    assert len(pub.shippable_orders) == 0
    fo = pub.failed_orders[0]
    assert fo.stage == FailureStage.ASSET_GENERATION
    assert fo.assets is None
    assert fo.asset_details is None


def test_metadata_generation_never_triggered_when_asset_generation_fails():
    order = Order(order_id="o3")
    asset_svc, metadata_svc, repo, pub = make_services(asset_should_fail=lambda o: True)

    calls = []
    original_queue = metadata_svc.queue
    metadata_svc.queue = lambda o: calls.append(o.order_id) or original_queue(o)

    process_order(order, asset_svc, metadata_svc, repo, pub)

    assert calls == []


def test_metadata_failure_produces_failed_order_with_asset_data_but_no_metadata():
    order = Order(order_id="o4")
    asset_svc, metadata_svc, repo, pub = make_services(metadata_should_fail=lambda o: True)

    process_order(order, asset_svc, metadata_svc, repo, pub)

    assert len(pub.failed_orders) == 1
    assert len(pub.shippable_orders) == 0
    fo = pub.failed_orders[0]
    assert fo.stage == FailureStage.METADATA_GENERATION
    assert fo.assets is not None
    assert len(fo.assets) == 2
    assert fo.asset_details is not None
    assert len(fo.asset_details) == 2


@pytest.mark.parametrize(
    "asset_fails,metadata_fails,expected_shippable,expected_stage",
    [
        (False, False, True, None),
        (True, False, False, FailureStage.ASSET_GENERATION),
        (False, True, False, FailureStage.METADATA_GENERATION),
    ],
)
def test_rules_1_through_6_table_driven(asset_fails, metadata_fails, expected_shippable, expected_stage):
    order = Order(order_id="o-table")
    asset_svc, metadata_svc, repo, pub = make_services(
        asset_should_fail=lambda o: asset_fails,
        metadata_should_fail=lambda o: metadata_fails,
    )

    process_order(order, asset_svc, metadata_svc, repo, pub)

    if expected_shippable:
        assert len(pub.shippable_orders) == 1
        assert len(pub.failed_orders) == 0
    else:
        assert len(pub.shippable_orders) == 0
        assert len(pub.failed_orders) == 1
        assert pub.failed_orders[0].stage == expected_stage


def test_unknown_job_id_raises_fail_fast():
    asset_svc, _, _, _ = make_services()

    with pytest.raises(UnknownJobError):
        asset_svc.get_status("no-such-job")


def test_poll_exhausted_propagates_rather_than_becoming_a_failed_order():
    order = Order(order_id="o5")
    asset_svc = InMemoryGenerationService(pending_polls_before_terminal=999)
    metadata_svc = InMemoryGenerationService()
    repo = InMemoryArtifactRepository()
    pub = InMemoryResultPublisher()

    with pytest.raises(PollExhaustedError):
        process_order(
            order,
            asset_svc,
            metadata_svc,
            repo,
            pub,
            poll=PollConfig(max_polls=3),
        )

    assert len(pub.failed_orders) == 0
    assert len(pub.shippable_orders) == 0


def test_worker_drains_queue_with_mixed_outcomes_and_isolates_orders():
    orders = [Order(order_id=f"o{i}") for i in range(5)]
    queue = InMemoryOrderQueue(orders)
    asset_svc, metadata_svc, repo, pub = make_services(
        asset_should_fail=lambda o: o.order_id == "o2",
        metadata_should_fail=lambda o: o.order_id == "o4",
    )

    taken_count = run(
        queue,
        asset_svc,
        metadata_svc,
        repo,
        pub,
        UnresolvedOrderRegistry(),
    )

    assert taken_count == 5
    assert len(queue) == 0
    assert len(pub.shippable_orders) == 3
    assert len(pub.failed_orders) == 2
    stages = {fo.order_id: fo.stage for fo in pub.failed_orders}
    assert stages["o2"] == FailureStage.ASSET_GENERATION
    assert stages["o4"] == FailureStage.METADATA_GENERATION
