# Local development

Docker Desktop must be running with Linux containers. No host Python installation is required.

## Start and stop

```powershell
docker compose up --build -d db browse booking expiry-worker
docker compose ps
docker compose logs --tail 50 booking expiry-worker
```

- Browse API and Swagger UI: http://localhost:8001/docs
- Booking API and Swagger UI: http://localhost:8002/docs
- PostgreSQL is internal to the Compose network. Ports for the APIs bind only to localhost.
- Demo events: `demo-general` (100 tickets) and `demo-seated` (seats A1 through A100).

```powershell
$headers = @{ 'X-Customer-Id' = 'alice'; 'Idempotency-Key' = 'purchase-001' }
$body = @{ event_id = 'demo-seated'; seats = @('A1', 'A2') } | ConvertTo-Json
$hold = Invoke-RestMethod http://localhost:8002/reservations -Method Post -Headers $headers -ContentType 'application/json' -Body $body
$hold
Invoke-RestMethod "http://localhost:8002/reservations/$($hold.id)/confirm" -Method Post -Headers $headers
Invoke-RestMethod http://localhost:8001/events/demo-seated/availability
```

Use the same key and body to retry the same intended purchase. Use a new key for a new purchase. General admission uses `{"event_id":"demo-general","quantity":2}` instead of `seats`.

```powershell
docker compose stop
```

`stop` retains the database. `docker compose down` removes containers and the network but retains the named data volume. Adding `--volumes` deletes local inventory and reservations: do that only when deliberately resetting the lab.

Schema and seed scripts run only on an empty database volume. The `migrate` service applies checksummed additive migrations before application startup, including upgrades to an existing local database. Do not edit a migration after it has been applied; add a new file instead.

## Run the PostgreSQL integration tests

```powershell
docker compose --profile test run --build --rm tests
docker compose --profile test stop test-db
```

The tests use a separate `trafficxx_test` database backed by temporary container storage. Every test resets that database; a safety check rejects the normal lab database. Tests exercise HTTP behavior, two inventory types, concurrent duplicates, group atomicity, expiry, cancellation, ownership checks, and competing state transitions. The inventory experiment uses 1,000 total attempts with 32 concurrent workers; it is a correctness test, not a throughput benchmark or 1,000 simultaneous-client test.

## Current scope and Azure path

Both APIs use the same runtime image with different startup commands. The expiry worker uses that image too. The test image is a separate build target. Local Compose uses the default DATABASE_AUTH_MODE=url with DATABASE_URL. Azure uses explicit workload-identity authentication and verify-full TLS instead of a saved database password; see [Browse API on Azure](browse-api-azure.md). Compose itself does not provision Azure.

These are local lab services. `X-Customer-Id` simulates identity and is forgeable; it must be replaced by verified authentication before public exposure. Local database credentials are disposable examples, and local Compose uses the database owner account. Azure browse/migrator roles, TLS and the real internal Browse API deployment have been verified separately. Other runtime roles, booking/worker deployment, connection pooling and request telemetry remain later milestones.

Reservation changes write durable outbox records. Start the optional [messaging profile](messaging.md) to publish and consume them using Microsoft's Service Bus emulator. Without that profile, pending outbox records accumulate.

Inspect the pending outbox count and oldest pending age locally:

```powershell
docker compose exec db psql -U trafficxx -d trafficxx -c "SELECT count(*) AS pending, max(clock_timestamp() - created_at) AS oldest_pending_age FROM outbox WHERE published_at IS NULL;"
```

This is a manual inspection command. The operations service adds `/backlog` flags and `/metrics`; see the messaging guide for startup, interpretation, and recovery commands. External alert delivery is not configured yet.
