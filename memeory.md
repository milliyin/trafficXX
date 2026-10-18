# trafficXX work log

This is the running project memory. The filename `memeory.md` follows the user's requested spelling.

## How to maintain this log

- Add a dated entry after meaningful work, debugging, experiments, or design decisions.
- Record what changed, why, and how it was verified. Distinguish proposals from accepted decisions.
- For bugs, record the symptom, confirmed cause, fix, verification, and anything still unresolved.
- Record useful lessons and the next concrete step. Never store credentials or secrets here.
- Keep earlier entries as history; add corrections when understanding changes.

## 2026-10-18 - Initial review and project memory

### Work completed

- Read the full `README.md` and inspected the workspace.
- Created this work log at the user's request.
- Found only `README.md` before creating this file. The documented repository layout is a target, not implemented code.
- A `git status --short` check reported that this directory is not a Git repository. No Git configuration was changed.

### What we learned

- The README calls the project Azure TrafficForge; the user refers to it as trafficXX.
- The goal is an Azure ticketing-platform lab demonstrating correctness, load handling, scaling, release recovery, security, and disaster recovery.
- Proposed components include browse and booking APIs, a worker, PostgreSQL, Redis, Service Bus, AKS, Terraform, and GitOps tooling.
- PostgreSQL is intended to enforce inventory correctness; Redis is a cache.
- Throughput, latency, availability, and rollback numbers are aspirational targets with no implementation measurements yet.

### Bugs and verification

- No application bugs were investigated or fixed; no application exists in the inspected workspace yet.
- Verification was limited to reading the README and inspecting the workspace. No application tests or infrastructure commands were run.

### Suggested starting point - pending user discussion

1. Establish the learning priority, Azure budget, time available, and familiarity with the tools.
2. Define inventory rules and reservation transitions before choosing deployment details.
3. Build a small local flow: seed an event, reserve inventory atomically, confirm or expire a reservation, and read its status.
4. Verify concurrent reservations, duplicate requests, and confirmation-versus-expiry races. Use the README's experiment: 100 tickets and 1,000 concurrent booking attempts.
5. Add reliable asynchronous processing and verify worker crash/retry behavior.
6. Deploy the proven flow to a small Azure environment, then add scaling and resilience experiments.

This proposes changing the README's infrastructure-first sequence; the user has not accepted that change yet.

### Open system-design questions

- Is the main objective Azure/DevOps practice, backend correctness, or both? How much experience and weekly time are available?
- What Azure credits/budget and region constraints apply?
- Are tickets general admission quantities or individually assigned seats? Start with one event or several?
- Does a successful reservation immediately hold inventory? What is the hold duration, and which transitions release it?
- What happens if confirmation and expiry run concurrently? Which database operation determines the winner?
- Does the confirmation endpoint finalize the booking or request asynchronous processing? What does the client see while it waits?
- What should a retry with the same idempotency key return? What if its request body differs or the original request is still running?
- How does a committed booking reach Service Bus after a process crash? Should a transactional outbox be part of the first queue implementation?
- Can a worker repeat processing after a crash without duplicating confirmation or inventory changes?
- How stale may browse availability be, and how should the API respond when displayed tickets are already unavailable?
- What request mix and overload behavior define the load test? Sold-out responses should be tracked separately from infrastructure failures.
- Does the first milestone require separate browse/booking services, or could one API with separate routes reduce initial setup while preserving a path to split them?

### Next step

Discuss the user's goals and the reservation model, then document the agreed first milestone and its acceptance criteria before implementation.

## 2026-10-18 - Learning goals and initial design discussion

### User-confirmed direction

- Learn both Azure/DevOps and backend system design.
- The user reports Azure sponsorship for five years. Credit allowance, renewal terms, region, quotas, and practical spending limit remain unknown; sponsorship duration is not a verified spending budget.
- Support both general-admission quantities and assigned seats.
- Keep separate browse and booking services to practice system design.
- Include a mechanism to check processing backlogs.
- Reservation duration and confirmation-versus-expiry rules are undecided.

### Design proposals - not yet accepted or implemented

- Start with a configurable five-minute reservation hold, using database time as the deadline authority.
- Model reservation transitions as HELD -> CONFIRMED, EXPIRED, or CANCELLED. Confirmation must atomically check HELD status and an unpassed deadline. At or after the deadline, confirmation is rejected even if expiry cleanup has not run yet.
- Serialize confirmation, expiry, and cancellation against the same reservation so inventory is released once and a confirmed reservation cannot later expire.
- For general admission, atomically guard available quantity; for assigned seats, enforce one active claim per event seat. These have different storage rules but can share the reservation lifecycle.
- Give booking sole ownership of inventory mutations. Browse reads availability and can later cache it; cached availability is advisory.
- Save a reservation and an outbox event in the same PostgreSQL transaction. A publisher retries pending outbox events into Service Bus. Consumers handle duplicates because publication or acknowledgement can repeat after crashes.
- Monitor pending outbox count and oldest age, queue backlog and oldest message age, worker failures/retries, and dead-letter messages.
- Use a periodic reconciliation job to identify overdue holds and stalled processing; repair actions must obey the same transaction and idempotency rules as normal processing.

### Lesson

Queue backlog monitoring detects delayed messages that reached the queue. It cannot detect an event that was never published. A transactional outbox preserves the intent to publish; monitoring and reconciliation expose delays and incomplete work.

### Next discussion

- Establish current Python/API, SQL, Docker, Terraform, and Kubernetes experience to choose the first practical exercise.
- Establish usable sponsorship credits/spending limit and weekly time before sizing Azure resources.
- Proposed first milestone: separate local browse and booking APIs with PostgreSQL, a documented reservation state model, and concurrency tests; add queue delivery once reservation correctness is demonstrated.
- No application code, infrastructure, or bug fixes were added in this discussion.

## 2026-10-18 - Atomic seat groups and duplicate requests

### User-confirmed decision

- Reject the entire multi-seat reservation if any requested seat is unavailable. Never silently reserve a partial group.

### Explanation and proposed implementation

- The user recalled a Python mechanism for duplicate requests. The relevant API concept is idempotency; no particular Python library has been selected.
- The client generates one idempotency key per intended reservation and reuses it on network retries or repeated submissions for that same action.
- Scope the key to the authenticated customer and operation. PostgreSQL enforces uniqueness across API replicas; an in-memory Python set or process-local lock would not provide that guarantee.
- Store a normalized request fingerprint and the resulting reservation/outcome with the key. Same key and same request returns the original result; same key with a different request is rejected.
- Claim the key, claim all requested inventory, create the reservation, and store the successful outcome atomically. Concurrent duplicates must coordinate on the database key so only one reservation is created. Failure-response persistence and key retention remain to be specified.
- Lock assigned-seat records in a consistent order, validate every requested seat, and commit all seat claims together. Roll back all claims if any requested seat cannot be reserved.

### Next design question

- If browse shows a seat as available but another customer reserves it first, should booking immediately reject the stale selection? Proposed response: return a conflict and let the customer choose again; booking checks authoritative database state.
- No application implementation or tests were performed; this entry records discussion and proposed behavior.

## 2026-10-18 - Stale availability behavior confirmed

- User accepted: if browse shows a seat as available but another customer reserves it first, reject the stale booking selection and refresh availability so the customer can choose again.
- Booking checks authoritative PostgreSQL state when claiming inventory. Displayed availability does not guarantee a reservation.
- The previously accepted all-or-nothing rule still applies: one unavailable seat rejects the entire requested group.
- Proposed HTTP response: `409 Conflict` with a stable error code such as `SEAT_UNAVAILABLE`. The exact API contract remains to be written.
- No application code or tests were added.

## 2026-10-18 - Five-minute reservation hold accepted

- User accepted a five-minute hold for the simulated checkout.
- Confirmation must succeed before the hold deadline; otherwise the reservation expires and its inventory becomes available again.
- Retrying the reservation request keeps the original expiry time and does not extend the hold.
- Proposed implementation remains: use database time, atomically coordinate confirmation and expiry, and release inventory exactly once. Cleanup may run after the deadline, but late confirmation must still be rejected.
- Next unresolved behavior: whether customers can cancel confirmed bookings, or only cancel active holds in the initial version.
- No application code or tests were added.

## 2026-10-18 - Initial cancellation scope accepted

- User accepted cancellation of active holds, releasing their inventory immediately upon a successful database transaction.
- Cancellation of confirmed bookings and refund rules are deferred beyond the first version.
- The agreed initial lifecycle is HELD -> CONFIRMED, CANCELLED, or EXPIRED. Holds last five minutes; retries do not extend them.
- Inventory release and state changes must remain atomic so concurrent cancellation, confirmation, and expiry cannot release inventory twice or cancel a confirmed reservation.
- Core behavior now agreed: both inventory types, all-or-nothing seat groups, rejection of stale seat selections, five-minute holds, and cancellation of active holds only.
- Proposed next deliverable: a database model and API contract covering those rules, followed by local implementation and concurrent reservation tests. Technology selection is still open; FastAPI is only an example in the README.
- No application code or tests were added.

## 2026-10-18 - First Docker backend implementation

### User direction

- User asked to begin implementation and explicitly requested Docker for everything to make a later Azure move easier.
- FastAPI was selected from the README's example stack for this first implementation.

### Work completed

- Added a multi-stage Dockerfile and Compose services for separate browse and booking APIs, PostgreSQL, an expiry worker, and an isolated PostgreSQL test environment.
- Added schema and demo inventory for general-admission and assigned-seat events.
- Implemented all-or-nothing reservations, five-minute holds, idempotent creation, owner-scoped reads, simulated confirmation, cancellation of active holds, and expiry cleanup.
- Added durable outbox records in the same transaction as reservation changes. Publishing and consuming Service Bus messages remain future work.
- Added local run instructions, API/design documentation, and a README quick start.

### Design choices and limits

- Every inventory writer locks its event row first. This favors a clear correctness baseline; one popular event serializes booking writes and needs contention measurements before scaling claims.
- General admission uses anonymous ticket-unit rows; assigned seating uses labelled rows. Each event currently uses one inventory mode. Mixed seating sections within one event are deferred.
- Successful creation keys are retained indefinitely for now. Retries return the same reservation and deadline with current status. Failed creation attempts do not persist a response/key.
- Customer identity is simulated using `X-Customer-Id`; this is not authentication. The local database uses disposable credentials and an owner account. Public deployment requires proper authentication and separate database roles.
- API ports bind to localhost, PostgreSQL stays on the Compose network, and application containers run as a non-root user.
- Docker packages the workloads for reuse; Azure infrastructure, secret injection, TLS, migrations, image registry/deployment configuration, and connection pooling still need implementation.

### Verification results

- Compose configuration validation passed.
- Initial Docker/PostgreSQL integration run: 19 tests passed in 7.14 seconds, with one dependency deprecation warning.
- The consistency test made 1,000 total booking attempts with 32 concurrent workers for 100 tickets; exactly 100 succeeded. This does not claim 1,000 simultaneous clients or any throughput target.
- Tests also cover competing seat claims, duplicate requests, group rejection, an outbox-write failure rolling back inventory, repeated cancellation, ownership checks, overdue holds, and confirmation/cancellation/expiry races.
- Checked current package versions from PyPI inside Docker and updated the initial pins to FastAPI 0.143.5, Uvicorn 0.54.0, Psycopg 3.3.6, pytest 9.1.1, and HTTPX 0.28.1. Switched PostgreSQL from the initial 17.4 image to the maintained 17 tag; the final runtime reports PostgreSQL 17.11.
- Final Docker/PostgreSQL integration run: **19 passed in 5.68 seconds**. The test client emits one non-blocking deprecation warning about migrating from HTTPX to HTTPX2; tests pass and this migration is deferred.
- Live HTTP smoke check on the published localhost ports: both APIs reported ready; a two-seat hold succeeded; retry returned the same reservation ID and deadline; cancellation succeeded; browse reported all 100 seats available again.
- The smoke check left one cancelled sample reservation and two pending outbox records (HELD and CANCELLED). Verified the pending-count/oldest-age query. These records are expected because the publisher is not implemented.
- Browse, booking, the expiry worker, and the healthy PostgreSQL container are running. Stopped the separate test database after verification.

### Environment issues and lessons

- Docker Desktop was initially unavailable; launched it in the background. Sandbox access to its engine pipe was denied, so Docker access was requested through the escalation mechanism and approved.
- A host `.venv` was created before the Docker-only preference arrived. It is ignored and is not used by the application or test workflow.
- Reservation deadlines must use database wall time after waiting for a lock. A test specifically checks a confirmation delayed past expiry while waiting for the event lock.
- Expiry changes must commit before returning a late-confirmation conflict, or inventory release would roll back with the error.

### Next milestone

- Add reliable outbox publishing, Service Bus consumers with duplicate protection, and backlog/oldest-age monitoring after the local baseline is verified.

## 2026-10-18 - Start requested

- Ran `docker compose up -d db browse booking expiry-worker`; all four services were already running and PostgreSQL was healthy.
- Verified both published API readiness endpoints returned `ready`.
- Browse documentation: http://localhost:8001/docs; booking documentation: http://localhost:8002/docs.
- Retained existing local data; no schema reset or inventory changes were performed.

## 2026-10-18 - Reliable messaging and backlog monitoring

### User authorization

- User asked to implement the next milestone.
- User explicitly accepted the Microsoft Service Bus emulator and SQL Server license terms for this local setup. Recorded acceptance in ignored `.env`; the distributable example defaults to non-acceptance.

### Implementation

- Added the Docker messaging profile with Microsoft's Service Bus emulator, its separate SQL Server Developer dependency, an outbox publisher, and an event consumer.
- Added transactional, checksummed additive migrations and a one-shot migration container that gates application startup. Existing lab data is retained.
- Outbox rows have stable UUID message IDs, send-attempt/failure counters, and bounded exponential retry delays. Parallel publishers coordinate through row locks with SKIP LOCKED.
- Consumer commits its inbox receipt and downstream reservation status read model before broker completion. Duplicates do not repeat the database effect; out-of-order events cannot regress the latest delivered state.
- Confirmation remains an explicit API action. The consumer does not automatically confirm reservations or send external notifications.
- Malformed or conflicting event envelopes are dead-lettered. Transient database failures retry with a five-delivery limit. Passive observation of the first 100 dead-letter entries also catches broker-directed dead letters.
- Added a localhost operations API on port 8003 with backlog-age flags, Prometheus metrics, worker heartbeats, and observed dead-letter counts.
- Added a dry-run-first reconciliation command for aged published events without inbox receipts, preserving their UUIDs and excluding observed dead letters.
- The application image supports either a Service Bus connection string or a namespace with DefaultAzureCredential for a future Azure workload identity. Real Azure deployment and permissions remain untested.
- Added `docs/messaging.md` and updated existing setup and architecture documentation.

### Verification results

- Docker Compose configuration validation passed.
- PostgreSQL suite: **37 passed, 5 broker tests skipped**, in 14.97 seconds. Includes the original reservation tests and new crash-window, duplicate-consumer, retry, out-of-order, backlog, replay, and migration tests.
- Corrected real-AMQP suite: **5 passed in 177.50 seconds**, including emulator readiness time, against `reservation-test-events` and isolated `trafficxx_test`. Verified actual duplicate delivery, poison-message dead-lettering, transient abandon/redelivery, redelivery after a committed effect, and the native broker delivery limit. Total across both successful suites: **42 passed**.
- Before and after upgrading the normal lab database, verified the existing CANCELLED reservation and all 200 ticket-unit rows were preserved.

### Lessons and limits

- A publisher can send successfully and crash before marking publication; retry must reuse its persisted message identity.
- A consumer can commit successfully and lose its broker acknowledgement; inbox deduplication must commit with the effect.
- Published events without receipts are an application-wide outstanding-work measure, not native queue depth. They can include in-flight, dead-lettered, or emulator-lost messages.
- Observed dead letters are a lower bound; the passive broker sample is limited to its first 100 entries. External alert notification and real Azure administration metrics are not configured.
- Local emulator startup initializes SQL Server system databases and broker databases and can take several minutes. Emulator restarts can discard broker messages; PostgreSQL records remain necessary for identifying and replaying missing work.
- FastAPI/Starlette still emits the previously noted HTTPX test-client deprecation warning. It does not fail the tests.

### Bug found during emulator integration

- Symptom: the emulator never became ready, the first AMQP test hit its startup timeout, and both workers reported missing queue entities. The backlog API retained and flagged the two unpublished events.
- Confirmed cause: the application queue was configured with a fourteen-day message lifetime, but the emulator supports at most one hour. The broker log explicitly rejected the configuration before creating the queues.
- Fix: changed the application queue to `PT1H`, matching the test queue; restarted only the emulator. Increased the first-start test readiness allowance to five minutes and made the broker suite fail fast on its first failure.
- The initial AMQP run was stopped after the startup error. All five real-AMQP tests passed after the fix; no passing result is claimed for the initial attempt.
- Verified that the additive migration retained the existing CANCELLED reservation and all 200 ticket units. The lab database volume was not reset.

### Live backlog experiment and final state

- Added and ran `scripts/backlog-drill.ps1`, a reproducible experiment with a cleanup block that restarts the consumer on normal assertion or HTTP failures.
- Baseline: zero pending publications, zero outstanding processed receipts, two processed events, and no alert flags.
- Stopped the consumer, created and cancelled a one-ticket demo hold, and waited 35 seconds. Publication continued: zero pending publications, two outstanding events, oldest age 36.21 seconds. Both PROCESSING_BACKLOG_AGING and CONSUMER_HEARTBEAT_STALE flags appeared.
- Restarted the consumer. The backlog drained to zero, the processed-event count rose from two to four, and alert flags cleared. Both worker heartbeats reported no error.
- Saved before/during/after evidence to ignored `test-results/backlog-drill.json`, recorded at 2026-10-18 11:25:22 UTC.
- Final HTTP checks: browse and booking reported ready, Prometheus metrics returned HTTP 200, and both demo events had all 100 units available. The new drill reservation remains CANCELLED.
- Nine runtime containers are running: browse, booking, operations, expiry, publisher, consumer, PostgreSQL, Service Bus emulator, and its SQL Server dependency. The migration container completed successfully; the separate test database was stopped after verification.
- No Azure resources were provisioned. This demonstrates local messaging correctness and backlog recovery, not cloud capacity, production availability, or native Azure queue administration metrics.

### Proposed next milestone

- Add CI for container builds, both test suites, and image/dependency scanning; then prepare the Azure Terraform foundation once region and usable sponsorship credit limits are established.

## 2026-10-18 - Container CI and repository setup

### User direction and repository

- User asked to implement the next milestone and supplied `https://github.com/milliyin/trafficXX.git` as the project repository.
- Verified that the repository was public and empty. Initialized this workspace's Git history on `main` and added that origin; no existing remote history was overwritten.
- Local `.env`, `.venv`, and generated test evidence remain ignored. Added `.env.*` protection while keeping `.env.example` trackable.

### CI implementation

- Added GitHub Actions for workflow/shell validation, PostgreSQL tests, optional real-AMQP emulator tests, runtime image scanning, and SBOM generation.
- Actions are pinned to verified full commit SHAs. Trivy 0.76.0 and actionlint 1.7.12 images are pinned by digest in `ci/tools.env`.
- Jobs have read-only repository permissions; checkout does not persist credentials. No Azure credentials, container publishing, or deployment is configured.
- Test scripts use reserved `trafficxx-ci-*` Compose projects, capture JUnit/log evidence, and clean up their own containers and volumes. They reject the normal `trafficxx` project name.
- CI report mounts use the Linux runner's UID/GID so the non-root test process can write evidence. Scripts run application/test workloads through Docker.
- Scanning reads a saved runtime image archive rather than giving Trivy Docker engine access. It scans OS and installed Python dependencies, retains all severities, and gates on fixable HIGH/CRITICAL findings. No ignore list was added.
- GitHub security summaries explicitly show unfixed findings. JSON, text, Markdown summaries, and test evidence are retained for seven days; large image archives are excluded from uploads.
- Broker tests on GitHub require repository variable `SERVICEBUS_ACCEPT_EULA=Y` after acceptance for those CI runners. The earlier local approval was not automatically extended to GitHub runners. When disabled, the workflow reports skipped coverage explicitly.
- Work-log-only pushes skip repeated jobs; all pull requests still run CI. Added `docs/ci.md` with policy and setup instructions.

### Local verification so far

- actionlint and ShellCheck passed after fixing initial shell warnings about literal assignment, export/assignment, and following the tool-version source file.
- Ran the exact database CI script in a Linux Docker helper: **37 passed in 8.61 seconds**. JUnit XML, Compose logs, and cleanup logs were saved under `test-results/ci/trafficxx-ci-db-local/`; its disposable project was removed.
- Runtime image scan and the chosen gate passed. CycloneDX SBOM contains **119 components**.
- Full report findings: 0 CRITICAL, 44 HIGH, 63 MEDIUM, 62 LOW, and 3 UNKNOWN package/advisory occurrences. The HIGH occurrences cover **8 distinct advisories**, with no fixes listed by Trivy. These remain unresolved; exploitability was not assessed. A passing gate does not mean the image is vulnerability-free.
- Security artifacts are under `test-results/ci/security/`. The summary renderer was verified against the actual scanner JSON and SBOM.

### Cold-start bug exposed by CI isolation

- The first isolated broker CI run stopped before pytest: SQL Server was still upgrading system databases when the inherited lab health-check budget expired.
- Failure logs and cleanup evidence were retained; the failed CI project was removed without affecting the demo.
- Added a CI-only health-check override with a three-minute startup grace period and one hundred subsequent checks. It still requires a successful SQL query; readiness is not bypassed. The corrected isolated broker run passed all five tests.

### Publication and hosted verification

- Committed the initial project and CI setup as `2cdc10a8815a7fd5bcd19c4e05ac7e8b5bd75670` and pushed `main` to the user-supplied origin. No force push was used.
- First hosted run completed with **success**: https://github.com/milliyin/trafficXX/actions/runs/39125451031.
- Hosted workflow/shell validation, PostgreSQL correctness tests, runtime image/security/SBOM, and broker-coverage reporting all passed. Hosted broker tests were explicitly skipped because CI license acceptance is not configured.
- Verified successful upload of `database-test-evidence` and `runtime-security-evidence`, with seven-day retention. The image archive was not uploaded.
- Confirmed `.env`, `.venv`, and generated test results were excluded from the initial commit. The live demo remained running throughout.
- Corrected local broker CI rerun: **5 passed in 211.97 seconds**, including emulator readiness time. Its JUnit report records five tests and zero failures; together with the database suite, **42 local tests passed**.
- Verified both local CI scripts exited successfully and removed their isolated containers and networks. Reports and cleanup logs remain under ignored `test-results/ci/`; the ordinary demo was not stopped or reset.
- Reusing the same local CI project name replaced the earlier failure log files on rerun; the confirmed diagnosis and fix remain recorded above. Hosted runs use unique run/attempt project names and separate uploaded artifacts.
- Saved the verified local vulnerability summary as `test-results/ci/security/security-summary.md`. Unfixed HIGH findings remain visible and unresolved.
- This verification closeout changes only the work log. The hosted success above applies to the implementation commit; work-log-only pushes intentionally skip redundant CI runs.
- No Azure resources, image registry publication, branch protection, or deployment settings were changed.

### Next milestone

- Prepare the Azure Terraform foundation and deployment identity after establishing the desired Azure region and usable sponsorship credit limits. The current CI checks can validate container changes before any cloud deployment.
