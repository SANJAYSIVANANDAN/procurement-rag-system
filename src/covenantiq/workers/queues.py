"""Queue topology (spec 25.2 and 25.6): one direct exchange, four work queues, and a dead-letter
exchange routing each queue's rejected messages to `{queue}.dlq`."""

from typing import Any

from kombu.entity import Exchange, Queue

from covenantiq.core import task_names

EXCHANGE = "covenantiq"
DEAD_LETTER_EXCHANGE = "covenantiq.dlx"

DOCUMENT_PROCESSING = "document_processing"
EMBEDDING_GENERATION = "embedding_generation"
CONTRACT_ANALYSIS = "contract_analysis"
VENDOR_ANALYSIS = "vendor_analysis"

WORK_QUEUES = (DOCUMENT_PROCESSING, EMBEDDING_GENERATION, CONTRACT_ANALYSIS, VENDOR_ANALYSIS)

QUEUE_FOR_TASK = {
    task_names.PROCESS_DOCUMENT: DOCUMENT_PROCESSING,
    task_names.PURGE_DOCUMENT: DOCUMENT_PROCESSING,
    task_names.EMBED_DOCUMENT: EMBEDDING_GENERATION,
    task_names.RUN_CONTRACT_ANALYSIS: CONTRACT_ANALYSIS,
    task_names.RUN_VENDOR_ASSESSMENT: VENDOR_ANALYSIS,
    task_names.RUN_VENDOR_COMPARISON: VENDOR_ANALYSIS,
}


def dlq_name(queue: str) -> str:
    return f"{queue}.dlq"


_exchange = Exchange(EXCHANGE, type="direct", durable=True)
_dead_letter_exchange = Exchange(DEAD_LETTER_EXCHANGE, type="direct", durable=True)

QUEUES = [
    Queue(
        name,
        _exchange,
        routing_key=name,
        durable=True,
        queue_arguments={
            "x-dead-letter-exchange": DEAD_LETTER_EXCHANGE,
            "x-dead-letter-routing-key": dlq_name(name),
        },
    )
    for name in WORK_QUEUES
]

DEAD_LETTER_QUEUES = [
    Queue(dlq_name(name), _dead_letter_exchange, routing_key=dlq_name(name), durable=True)
    for name in WORK_QUEUES
]

ROUTES: dict[str, dict[str, Any]] = {
    task: {"queue": queue, "exchange": EXCHANGE, "routing_key": queue}
    for task, queue in QUEUE_FOR_TASK.items()
}


def declare_topology(app: Any) -> None:
    """Declare the exchanges, work queues and dead-letter queues (idempotent).

    Messages dead-lettered to an exchange with no bound queue are silently dropped, so the DLQs
    must exist before the first failure; workers and the API both call this at startup.
    """
    with app.connection_for_write() as connection:
        # Bounded: an unreachable broker must fail startup checks in seconds, not retry for long.
        connection.ensure_connection(max_retries=1, interval_start=0, interval_step=0.5)
        channel = connection.default_channel
        for queue in (*QUEUES, *DEAD_LETTER_QUEUES):
            queue(channel).declare()
