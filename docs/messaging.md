# Reliable reservation events

The local pipeline is PostgreSQL outbox -> Service Bus -> consumer inbox and reservation read model. Confirmation remains an explicit customer action; the consumer records delivery of state changes and does not automatically confirm holds.

## Run in Docker

The emulator uses SQL Server Developer as a local storage dependency. This is separate from the application's PostgreSQL database. Neither broker nor SQL Server ports are published to the host.

Before first use, read the [emulator license](https://github.com/Azure/azure-service-bus-emulator-installer/blob/main/EMULATOR_EULA.txt) and [SQL Server license](https://go.microsoft.com/fwlink/?LinkId=746388). If you accept both, add `SERVICEBUS_ACCEPT_EULA=Y` to your ignored `.env` file. `.env.example` defaults to `N`. Do not replace an existing `.env` containing other settings. The user accepted both licenses for this workspace during implementation.

```powershell
docker compose --profile messaging up --build -d
docker compose --profile messaging ps
docker compose logs --tail 30 publisher consumer
```

The migration service upgrades the existing database before application services start. Do not delete the database volume to install this milestone. Migrations are transactional, serialized with an advisory lock, and checked for changes to previously applied SQL files. The original bootstrap schema remains in `database/001-schema.sql`; additive changes live in `database/migrations/`.

- Browse: http://localhost:8001/docs
- Booking: http://localhost:8002/docs
- Operations: http://localhost:8003/docs
- Backlog report: http://localhost:8003/backlog
- Prometheus exposition: http://localhost:8003/metrics

SQL Server and emulator first startup can take several minutes. Publishers and consumers retry connections while the broker initializes. This setup creates no Azure resources.

## Delivery guarantees

1. Booking commits reservation changes and an outbox row in one PostgreSQL transaction.
2. Each outbox row receives a durable UUID used as the Service Bus message ID. Existing outbox rows receive UUIDs during migration.
3. Publishers claim due rows with `FOR UPDATE SKIP LOCKED`. A send succeeds before the row is marked published. A crash between these steps can resend the same UUID.
4. Failed sends retain the row and retry with exponential delay from two to sixty seconds. Successful publication clears the current error; failure counters remain available.
5. The consumer receives in Peek-Lock mode and commits its inbox entry and downstream database effect together. It completes the broker message only after commit. A repeated message does not repeat the effect.
6. Each event includes its outbox sequence. The read model accepts a newer sequence but never regresses when earlier messages arrive later.

Publishing is at least once. The inbox protects the database effect; it does not make arbitrary external effects, such as sending real email, exactly once. Broker duplicate detection is deliberately disabled so the application deduplication path is exercised.

The publisher holds one database row lock during its network send. This is a simple baseline with bounded send time, not a high-throughput publisher design. Measure database connection use and lock contention before increasing worker counts.

## Retry and dead-letter behavior

Malformed JSON, unknown schema versions, message-ID mismatches, or reusing a processed ID with different content are dead-lettered immediately. Database processing failures are abandoned with a short backoff; the worker dead-letters after five deliveries. The queue also has a five-delivery limit for failures outside application handling. The consumer observes the first 100 dead-letter messages non-destructively every fifteen seconds, including messages dead-lettered by the broker itself.

The downstream effect is intentionally small: `reservation_updates` stores the latest delivered reservation status. `consumer_inbox` records processed event identities. These support failure experiments without introducing real payments or notifications.

## Backlog interpretation

| Signal | Meaning |
|---|---|
| `outbox_pending` and `outbox_oldest_seconds` | Booking changes still waiting for publication |
| `processing_outstanding` and `processing_oldest_seconds` | Published events without a committed consumer receipt; age starts at event creation |
| `processed_total` | Unique events with committed consumer effects |
| `publish_failures_total` | Recorded failed send attempts |
| `processing_failures_total` | Recorded database-processing failures; a database outage can prevent recording its own failures |
| `observed_dead_letters_total` | Distinct observed dead-letter entries, including direct worker dead-letter actions |
| Worker heartbeat and error | Last publisher/consumer loop observation and sanitized error class/code |

The outstanding count covers this application's published records. It includes in-flight work, dead-lettered work, and events lost by restarting the local emulator. It is **not the native queue depth**. Dead-letter observations are a lower bound because passive inspection samples the first 100 entries; broker administration metrics for real Azure are a later integration.

`/backlog` flags unpublished or unprocessed work older than thirty seconds, observed dead letters, and stale/erroring recorded workers. Set `BACKLOG_ALERT_SECONDS` on the operations container to change that threshold. These are API flags and Prometheus metrics; an external alert delivery service is not configured yet. A worker that has never successfully written a heartbeat cannot produce a stale-heartbeat record; backlog-age flags still expose stalled work.

## Inspect and replay

```powershell
docker compose exec db psql -U trafficxx -d trafficxx -c "SELECT reservation_id, status, last_outbox_id FROM reservation_updates;"
docker compose exec publisher python -m trafficxx.reconcile
```

Replay defaults to a dry run. After investigating an aged backlog, apply it explicitly:

```powershell
docker compose exec publisher python -m trafficxx.reconcile --older-than-seconds 60 --apply
```

Only published events older than the grace period, with no inbox receipt and no observed dead-letter entry, become pending again. Their UUIDs remain unchanged, so an original in-flight copy can still arrive safely. The minimum grace period is ten seconds. This command does not redrive known poison messages; investigate them before designing an explicit dead-letter recovery action.

The local emulator is a development dependency. Its message storage must not be treated as durable across emulator restarts. The PostgreSQL outbox, inbox, and outstanding-work monitor remain the evidence for recovery experiments. See Microsoft's [emulator overview](https://learn.microsoft.com/en-us/azure/service-bus-messaging/overview-emulator) for limitations.

The emulator caps message lifetime at one hour, so both local queues use `PT1H`. Azure queue retention must be chosen separately when provisioning the real service. First-start broker readiness checks allow up to five minutes for database initialization.

## Tests

```powershell
# PostgreSQL correctness tests; emulator tests are skipped in this run.
docker compose --profile test run --build --rm tests

# Real AMQP tests, using an isolated database and dedicated test queue.
docker compose --profile broker-test run --build --rm broker-tests
docker compose --profile test stop test-db
```

Run these suites sequentially: both use `trafficxx_test`. Broker tests drain only `reservation-test-events`, never the application queue. Coverage includes duplicate delivery, send/mark and commit/ack failure windows, out-of-order delivery, transient retries, poison messages, native delivery-limit dead-lettering, backlog signals, safe replay, and repeated migration application.

For a repeatable live backlog experiment, start with the pipeline drained and the default thirty-second alert threshold, then run:

```powershell
powershell -NoProfile -File scripts/backlog-drill.ps1
```

The script stops the consumer, creates and cancels a one-ticket demo hold, waits for the backlog age flag, and restarts the consumer in a `finally` block. It verifies that the backlog drains and writes before/during/after evidence to ignored `test-results/backlog-drill.json`. It leaves a cancelled synthetic reservation in the lab database and restores its inventory.

## Azure configuration

The same application image supports a real Service Bus connection string via `SERVICEBUS_CONNECTION_STRING`, or `SERVICEBUS_FULLY_QUALIFIED_NAMESPACE` with `DefaultAzureCredential`. For AKS, supply a workload identity with appropriate send or receive permissions; configure the queue and dead-letter policy through infrastructure code. Omit the emulator connection string when using namespace identity. Do not use `UseDevelopmentEmulator=true` in Azure. The provided Compose environment is specifically for the local emulator and must be replaced in cloud deployment configuration.

References: [Microsoft emulator setup](https://learn.microsoft.com/en-us/azure/service-bus-messaging/test-locally-with-service-bus-emulator), [Python Service Bus client](https://learn.microsoft.com/en-us/python/api/azure-servicebus/azure.servicebus.servicebusclient?view=azure-python), [dead-letter queues](https://learn.microsoft.com/en-us/azure/service-bus-messaging/service-bus-dead-letter-queues).

## Verified local results - 2026-10-18

- Database suite: 37 passed; the five optional broker tests were skipped in that run.
- Separate real-AMQP suite: all five passed, for 42 passing tests across the two suites.
- Live consumer-stop drill: two published events accumulated; at 36.21 seconds oldest age, backlog-age and stale-heartbeat flags appeared. Restarting the consumer drained outstanding work to zero and cleared the flags.
- Both demo inventories were restored to 100 available units, and the pre-existing reservation survived the schema migration.

The generated drill report is `test-results/backlog-drill.json`. These results establish local correctness and recovery behavior; they are not an Azure throughput or availability benchmark.
