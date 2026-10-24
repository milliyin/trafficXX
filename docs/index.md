---
title: Overview
hide:
  - toc
---

<div class="tx-hero" markdown>

<p class="tx-eyebrow">BUILD. TEST. UNDERSTAND.</p>

# trafficXX

**A ticket reservation backend. A practical journey into distributed systems.**

Follow the design decisions, Azure deployments, failed experiments, and verified
results behind a system that holds tickets, moves events, and recovers from faults.

[Explore the architecture](architecture.md){ .md-button .md-button--primary }
[Start the learning path](getting-started.md){ .md-button }

</div>

## The latest measured milestone

<div class="tx-milestone" markdown>

**100 paced users. 2,734 successful reads. Zero errors.**

A one-minute in-cluster Browse test passed with four ready database connections
and 16 waiting slots per pod. 95% of requests completed within 313 ms.

[Read the experiment and its limits](browse-user-load.md#live-100-user-queue-16-comparison-passed)

</div>

This is a learning lab, not a production-readiness claim. Results are dated
observations. The load generator's memory use is the next investigation.

## Find your starting point

<div class="grid cards" markdown>

- **Understand the design**

    Why event locks, idempotency keys, and a transactional outbox keep a ticket
    reservation consistent.

    [System architecture](architecture.md)

- **Build the Azure lab**

    AKS, private PostgreSQL, workload identities, and the steps that connect them.

    [Azure foundation](aks.md)

- **Follow an event**

    From a committed reservation change to Service Bus, a consumer inbox, and a
    current-state projection.

    [Messaging design](messaging.md)

- **Learn from the failures**

    Duplicate messages, poison events, pod replacement, backlog recovery, and
    the path from a failed 100-user test to a passing run.

    [Load-test record](browse-user-load.md)

</div>

## What runs where

| Layer | Components | Responsibility |
| --- | --- | --- |
| Application | Browse and Booking APIs on AKS | Read availability and change reservations |
| Database | Private PostgreSQL | Inventory, transactions, outbox, and consumer records |
| Background work | Expiry, publisher, consumer | Release expired holds and deliver committed events |
| Broker | Azure Service Bus | Carry reservation events between workers |
| Observation | Operations API, Prometheus, Grafana | Expose state, store metrics, and display warnings |

These pages are the project's reference and experiment record. Use
[verified progress](progress.md) to distinguish completed checks from work that
is still pending. The APIs remain internal; this site is their documentation.
