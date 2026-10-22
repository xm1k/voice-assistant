# RabbitMQ delivery

All four queues share the consumer in `shared/backend/chat/broker.py`. ACK follows
successful processing or confirmed mandatory publication to the existing retry/dead
queue. At attempt five, dead publication precedes the core exhausted notification
and source ACK. HTTP 409 keeps the attempt unchanged. Failed publication or core
notification causes NACK/requeue, connection closure and a five-second backoff.
Cancellation closes the connection without an extra retry copy. Failed ACK also
reconnects, allowing repeat delivery of a committed result.

Publishers require positive confirms and raise on mandatory returns. Retry copies
preserve the body and properties, changing only the attempt header. Outbox rows are
marked published after confirmation. A crash before their commit can republish.

The total asynchronous handler deadline is 600 seconds; job leases remain 720
seconds. RabbitMQ consumer_timeout is explicitly 900000 ms. Connection, publish,
exhausted notification and settlement operations are bounded by 30 seconds.
An awaited thread cannot be forcibly stopped by asyncio cancellation; existing
lease and version fences remain necessary. Synchronous blocking code cannot be
interrupted by an asynchronous deadline.

Core locks the chat row, checks terminal status and lease fences, and commits
records, messages, events and outbox in one transaction. Transcription creates its
successor job in that transaction. Indexing locks and rechecks the record version
after embedding and updates the existing embedding row.

## Verification

No paid LLM calls are needed:

```sh
python -m pytest shared/tests_v2 -q
```

Real fault tests require existing isolated test endpoints:

```sh
TEST_POSTGRES_URL=postgresql+psycopg://user@127.0.0.1/test_database \
TEST_RABBITMQ_URL=amqp://user:password@127.0.0.1/test_vhost \
python -m pytest shared/tests_v2 -q
```

Use a dedicated empty vhost, never the production/default vhost. Tests use the
existing logical queue names there and kill only their child consumer process.
PostgreSQL tests create/drop uniquely named schemas; the user needs schema creation
rights. Death before handling, during handling and after commit before ACK must
preserve delivery and produce one mutation. Additional tests cover concurrent
results, transcription deduplication, stale fences and outbox confirmation rollback.
Without endpoint variables these integration tests explicitly skip.

Index version guards use SQLite with vector serialization and a stub embedder;
they do not verify PostgreSQL pgvector operations or model inference. The available
local PostgreSQL has no pgvector extension. Local RabbitMQ/PostgreSQL fault tests
do not stop either service.

## Deployment order

1. Apply `services/rabbitmq/deployment.yaml` first. Preserve PVC `rabbitmq-v2`;
   never delete it or purge queues. This restarts the existing single broker, so
   schedule the rollout separately from fault tests.
2. Wait for readiness and check inside the pod:
   `rabbitmqctl eval 'application:get_env(rabbit, consumer_timeout).'`
   Expected: `{ok,900000}`.
3. Build/deploy updated existing core, assistant-worker and transcription-worker
   images. Core contains the index and result consumers.
4. Check readiness and job completion; monitor retry/dead queues, unacknowledged
   deliveries and expired leases. Queue names and arguments are unchanged.

No production rollout was performed during local verification. Roll back consumers
before lowering broker timeout. Existing retry TTL/dead-letter behavior, malformed
JSON handling, recovery attempt counting and single-node durability limits remain
unchanged. This does not add survival of broker disk/node loss.
