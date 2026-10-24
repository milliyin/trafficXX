# Choose a learning path

You do not need to deploy the entire Azure lab to understand trafficXX.
Start with the question you want to answer.

## Understand the system

1. Read the [system design](architecture.md) and reservation lifecycle.
2. Follow the [messaging flow](messaging.md) from transaction to consumer.
3. Review [verified progress](progress.md) for evidence and limitations.

The central problem is simple: two customers must not both get the same ticket.
The surrounding work makes the result reliable when requests repeat, workers
stop, or messages arrive more than once.

## Run it locally

Follow [local development](local-development.md) for dependencies and commands.
The [CI guide](ci.md) explains the correctness and messaging checks.

Local execution is separate from the deployed Azure lab. Use the documented
environment examples and disposable local credentials; do not copy cloud secrets
into source control.

## Explore the deployed lab

Start with [AKS setup](aks.md), [private PostgreSQL](postgresql.md), and
[workload identity](workload-identity.md). Then follow the schema and service
guides in the sidebar. Commands name this project's particular lab resources;
review them before adapting them to your own subscription.

The Browse and Booking services are internal. The public documentation site
does not expose their application endpoints or provide an authenticated customer
interface. Grafana has its own authenticated HTTPS access.

## Study the experiments

Read the [bounded read baseline](read-load-baseline.md),
[booking write baseline](booking-write-baseline.md), and
[Browse user-load record](browse-user-load.md) in that order.

In the load record, a **user** pauses between requests. It is not a request per
second. A **passing test** means its explicit checks passed for that run; it is
not a promise of unlimited capacity.

## A few concepts in plain words

| Concept | Plain-language meaning |
| --- | --- |
| Idempotency | Repeating the same successful request returns the same result instead of creating another booking. |
| Database lock | A transaction takes its turn before another transaction can change the same inventory. |
| Connection pool | A small group of database connections is kept ready and shared between requests. |
| Waiting queue | Requests can wait briefly when every connection is busy. The line has a limit. |
| Transactional outbox | A business change and the record of its event are saved together. A worker sends the event afterward. |
| Consumer inbox | A receipt helps the consumer avoid applying a duplicate message twice. |
| Workload identity | A running service proves which Azure identity it is using instead of storing a database password. |
