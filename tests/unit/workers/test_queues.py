import pytest
from celery import Celery

from covenantiq.core import task_names
from covenantiq.workers import queues


@pytest.fixture
def broker() -> Celery:
    """An in-memory broker with the real topology declared and every queue emptied."""
    app = Celery("test", broker="memory://")
    queues.declare_topology(app)
    with app.connection_for_write() as connection:
        for queue in (*queues.WORK_QUEUES, *(queues.dlq_name(q) for q in queues.WORK_QUEUES)):
            connection.default_channel.queue_purge(queue)
    return app


class TestTopology:
    def test_four_work_queues_on_one_direct_exchange(self) -> None:
        assert {q.name for q in queues.QUEUES} == {
            "document_processing",
            "embedding_generation",
            "contract_analysis",
            "vendor_analysis",
        }
        for queue in queues.QUEUES:
            assert queue.exchange.name == "covenantiq"
            assert queue.exchange.type == "direct"
            assert queue.routing_key == queue.name
            assert queue.durable

    def test_every_work_queue_dead_letters_to_its_own_dlq(self) -> None:
        for queue in queues.QUEUES:
            assert queue.queue_arguments == {
                "x-dead-letter-exchange": "covenantiq.dlx",
                "x-dead-letter-routing-key": f"{queue.name}.dlq",
            }

    def test_dead_letter_queues_are_durable_and_bound_to_the_dlx(self) -> None:
        assert {q.name for q in queues.DEAD_LETTER_QUEUES} == {
            f"{name}.dlq" for name in queues.WORK_QUEUES
        }
        for queue in queues.DEAD_LETTER_QUEUES:
            assert queue.exchange.name == "covenantiq.dlx"
            assert queue.routing_key == queue.name
            assert queue.durable
            assert not queue.queue_arguments  # a DLQ must not itself dead-letter

    def test_dlqs_are_not_consumed_by_workers_by_default(self) -> None:
        assert not {q.name for q in queues.QUEUES} & {q.name for q in queues.DEAD_LETTER_QUEUES}


EXPECTED = {
    task_names.PROCESS_DOCUMENT: "document_processing",
    task_names.PURGE_DOCUMENT: "document_processing",
    task_names.EMBED_DOCUMENT: "embedding_generation",
    task_names.RUN_CONTRACT_ANALYSIS: "contract_analysis",
    task_names.RUN_VENDOR_ASSESSMENT: "vendor_analysis",
}


class TestRouting:
    def test_each_task_goes_to_the_spec_queue(self) -> None:
        assert queues.QUEUE_FOR_TASK == EXPECTED
        for task, queue in EXPECTED.items():
            assert queues.ROUTES[task] == {
                "queue": queue,
                "exchange": "covenantiq",
                "routing_key": queue,
            }

    def test_every_route_targets_a_declared_queue(self) -> None:
        declared = {q.name for q in queues.QUEUES}
        assert {route["queue"] for route in queues.ROUTES.values()} <= declared

    def test_ingest_tasks_share_the_ingest_workers_queues(self) -> None:
        # worker-ingest consumes `-Q document_processing,embedding_generation` (compose)
        ingest = {q for t, q in EXPECTED.items() if t.startswith("ingest.")}
        assert ingest == {"document_processing", "embedding_generation"}


class TestDeclaration:
    def test_declare_creates_every_queue(self, broker: Celery) -> None:
        with broker.connection_for_read() as connection:
            channel = connection.default_channel
            for name in queues.WORK_QUEUES:
                assert channel.queue_declare(queue=name, passive=True).message_count == 0
                dlq = channel.queue_declare(queue=queues.dlq_name(name), passive=True)
                assert dlq.message_count == 0

    def test_declare_is_idempotent(self, broker: Celery) -> None:
        queues.declare_topology(broker)
        queues.declare_topology(broker)

    def test_dead_lettered_messages_are_routed_by_the_dlx(self, broker: Celery) -> None:
        from kombu import Producer

        with broker.connection_for_write() as connection:
            channel = connection.default_channel
            Producer(channel).publish(
                body={"x": 1},
                exchange="covenantiq.dlx",
                routing_key="document_processing.dlq",
                serializer="json",
            )
            assert (
                channel.queue_declare(queue="document_processing.dlq", passive=True).message_count
                == 1
            )
            assert (
                channel.queue_declare(queue="vendor_analysis.dlq", passive=True).message_count == 0
            )


def test_declaring_against_an_unreachable_broker_fails_quickly() -> None:
    import time

    dead = Celery("dead", broker="amqp://nobody:nope@127.0.0.1:1//")
    dead.conf.broker_connection_timeout = 1
    started = time.monotonic()
    with pytest.raises(Exception, match=r"."):
        queues.declare_topology(dead)
    assert time.monotonic() - started < 6
