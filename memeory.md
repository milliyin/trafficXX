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

### Local shutdown requested

- User requested stopping the Docker containers. Stopped this project's Compose services, including PostgreSQL, the messaging workers, Service Bus emulator, and SQL Server.
- Verified that `docker compose ps --status running` returned no running project containers. Kept containers and data volumes for later restart.

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

### Hosted broker coverage enabled and verified

- User configured repository Actions variable `SERVICEBUS_ACCEPT_EULA=Y` and supplied a screenshot, enabling the emulator and SQL Server license opt-in for GitHub CI runners.
- Manually dispatched the existing CI workflow on `main` at commit `5aeb98b45055c8652c9472953466181859e32a6e`. No source change was needed to enable the job.
- Full hosted run completed with **success**: https://github.com/milliyin/trafficXX/actions/runs/39126094347.
- All five jobs passed: workflow/shell validation, PostgreSQL correctness tests, Service Bus emulator tests, runtime image/security/SBOM, and broker-coverage reporting. The broker job ran successfully rather than being skipped.
- Verified uploads of `broker-test-evidence`, `database-test-evidence`, and `runtime-security-evidence`. The configured retention is seven days.
- The security gate still allows findings without available fixes; the earlier documented unfixed HIGH advisories remain unresolved. A green workflow does not establish that the image has no vulnerabilities.
- Recorded this result as a work-log-only change so its push does not start a redundant CI run. No Azure deployment or infrastructure provisioning was performed.

### Next milestone

- Prepare the Azure Terraform foundation and deployment identity. The user has confirmed continuous operation is acceptable and asked not to block on sponsorship credit or region questions; keep the region configurable.

## 2026-10-18 - Terraform learning and bootstrap validation

- User wants to understand each part while building. Explained locking, pooling, idempotency, outbox/inbox, backlogs, dead letters, Docker, CI, and Terraform using plain-language examples. Continue in small teaching steps.
- Drafted `infrastructure/bootstrap/` for state storage and `infrastructure/foundation/` for networking, ACR, and a logging workspace. These are preparations for the README's AKS design; no Azure resources have been provisioned.
- Docker Desktop is off. Used the downloaded Terraform 1.16.6 tool in the ignored local tools folder without restarting project containers.
- Applied Terraform formatting. Initialized bootstrap with `-backend=false`, installed the signed AzureRM 5.10.0 provider, and generated its dependency lock file.
- `terraform -chdir=infrastructure/bootstrap validate` passed. This checks configuration and provider schema; it does not verify subscription permissions, resource availability, or deployment success. Foundation is still unvalidated.
- The bootstrap initially keeps its own state locally. It must be protected and backed up; it is separate from the later foundation state held in Azure Blob Storage. No plan or apply has been run.
- User supplied an Azure portal screenshot showing the sponsorship subscription and East US. Added ignored `infrastructure/bootstrap/terraform.tfvars` with the supplied subscription ID, region `eastus`, and proposed state account name `trafficxxtfstatee8708a85`. Global name availability and subscription access have not been checked. No Azure resources were created.
- User prefers executing Azure commands personally in browser Cloud Shell and declined local account access. Cloud Shell confirmed the intended sponsorship subscription and Terraform 1.16.5 on Linux. Adjusted both configurations to accept Terraform >=1.16.5 and <1.17.0; AzureRM remains pinned to 5.10.0. Continue with user-run Azure commands rather than accessing their account locally.
- User ran bootstrap validation successfully in Cloud Shell, then supplied the saved-plan output: **3 to add, 0 to change, 0 to destroy**. Proposed resources are the East US state resource group, `trafficxxtfstatee8708a85` storage account, and private `tfstate` container. State account shared-key access is disabled, versioning and 30-day deletion retention are enabled; its network endpoint remains public with authentication required. Plan saved in Cloud Shell as `bootstrap.tfplan`; apply has not yet been reported.
- User subsequently supplied the bootstrap `backend` outputs after the apply instruction, indicating apply completion: resource group `trafficxx-tfstate-rg`, account `trafficxxtfstatee8708a85`, container `tfstate`, foundation key `lab/foundation.tfstate`, Entra authentication enabled. These outputs were user-reported from Cloud Shell; no local Azure access was used. Next verify blob data access before initializing the foundation backend. Bootstrap's own local state must be kept safely; it has not been migrated into Azure.
- Blob listing with `--auth-mode login` failed with a data-permission error. Management access did not provide the required blob data access. Next remediation: user-run assignment of Storage Blob Data Contributor scoped to the `tfstate` container, followed by access verification after propagation. Keep shared-key authentication disabled. Role assignment completion has not yet been reported; capture it in infrastructure code later to avoid an undocumented manual dependency.
- After the role-assignment instructions, user reported blob listing returned `0`: authenticated list access now succeeds and the state container is empty. This verifies listing, not a write operation; the subsequent backend initialization will exercise remote state access.
- Prepared ignored foundation inputs for the same subscription and `eastus`, proposed registry `trafficxxacre8708a85` (availability unchecked), and Entra-authenticated backend `lab/foundation.tfstate` in the existing state container.
- Foundation formatting and provider-schema validation passed locally without Azure account access. Reused the previously verified bootstrap provider and full registry checksum lock file; replaced the temporary Windows-only lock produced by offline initialization so Linux Cloud Shell can install its provider.
- Packaged the six foundation configuration/input/lock files into ignored `test-results/trafficxx-foundation.zip` for user upload. No `.terraform` directory, state files, or saved plans are included. Next user-run steps: initialize the real Azure backend and validate in a separate Cloud Shell foundation folder. No foundation plan/apply has been run.
- User reported successful foundation validation, then supplied the Cloud Shell plan: **7 to add, 0 to change, 0 to destroy**, matching the intended resource group, VNet, three subnets, Basic ACR with admin authentication disabled, and 30-day Log Analytics workspace in East US. Remote backend lock acquisition and release appeared in the output. Saved plan is `foundation.tfplan`; foundation apply completion has not yet been reported. ACR uses a public endpoint with authentication; subnet security controls and telemetry collection are later steps.
- User supplied foundation outputs after applying the saved plan, indicating successful creation of `trafficxx-lab-rg`, the AKS/PostgreSQL/private-endpoint subnets in `trafficxx-lab-vnet`, logging workspace `trafficxx-lab-logs`, and registry `trafficxxacre8708a85.azurecr.io`. These are user-reported Cloud Shell results; no local Azure account access was used.
- Next verification: run a fresh foundation `terraform plan` in Cloud Shell and expect no changes. Next implementation milestone is AKS and its identities/role assignments; no cluster, database server, queue, application deployment, or telemetry collection has been created by the foundation configuration.
- User confirmed the follow-up foundation plan reported no changes. Next teaching/build step is AKS, using separate system and application node pools. Obtain user-run East US AKS version and compute-quota output before choosing the Kubernetes version and node sizing; subscription credit is not the same as a compute quota.

## 2026-10-18 - AKS draft after version and quota checks

- User supplied East US Kubernetes versions and quotas from Cloud Shell. The list includes 1.36.4 with KubernetesOfficial support. Total regional vCPU quota is 705; Standard DSv5 family quota is 65; both showed zero usage.
- Added `infrastructure/foundation/aks.tf` to extend the existing foundation state: AKS Standard at 1.36.4; two dedicated system nodes; application pool starts at two and autoscales between two and four. Both use Standard_D4s_v5, four vCPUs each. Initial pools use 16 vCPUs, rising to 24 at configured maximum before upgrade surge; actual SKU capacity remains unchecked.
- Added explicit control-plane and kubelet managed identities, subnet-scoped Network Contributor, kubelet-scoped Managed Identity Operator, registry-scoped AcrPull, and cluster-scoped Azure Kubernetes Service RBAC Cluster Admin for an explicitly supplied human Entra object ID. No local account access was used; all Azure operations remain user-run.
- AKS uses Azure CNI Overlay/Cilium, distinct pod/service CIDRs, Standard Load Balancer outbound connectivity, no node public IPs, Entra/Azure RBAC, disabled local cluster accounts, and OIDC/workload identity readiness. API endpoint is public for Cloud Shell access; source-IP restrictions, application network policies, zone placement, workload federation, collection of telemetry, and workloads are not implemented in this step.
- Terraform controls node-pool definitions and ignores the application pool's current node_count so it does not override autoscaler decisions. Kubernetes upgrades are explicit; node-image updates use NodeImage.
- Local validation initially caught a missing Entra tenant parameter. Added `azurerm_client_config.current.tenant_id` to the Entra block. Formatting, provider-schema validation, and whitespace checks then passed. A providers-schema attempt in the uninitialized foundation backend failed without changing Azure; obtained the same pinned provider's schema from bootstrap instead.
- Added `docs/aks.md` covering design, limits, teaching notes, Cloud Shell upload, SKU preflight, plan, and later apply. Updated README status and excluded infrastructure from application Docker build context to avoid sending provider binaries or private Terraform files to Docker.
- Created ignored `test-results/trafficxx-aks-update.zip` containing only aks.tf, variables.tf, and outputs.tf. It preserves the user's existing backend, input values, and state. Expected plan is eight additions and no changes/deletions to the seven existing foundation resources. No AKS plan/apply or real cluster verification has been performed yet.
- User confirmed Cloud Shell validation passed and Standard_D4s_v5 SKU restrictions were empty in East US. This is a subscription restriction check, not a runtime capacity guarantee.
- Reviewed user-supplied `aks.tfplan` output: **8 to add, 0 to change, 0 to destroy**, matching two identities, four scoped roles, one AKS cluster including its two-node system pool, and one autoscaling two-to-four-node application pool. Version 1.36.4, Standard tier, Entra/Azure RBAC, local accounts disabled, expected subnet, Cilium overlay, explicit outbound IP, and registry pull scope all match the draft. Human admin principal is supplied in the plan. No existing foundation resources are changed. AKS API is public with identity authentication. Plan saved successfully; apply completion and cluster readiness have not yet been reported.
- User supplied outputs following AKS apply: cluster `trafficxx-lab-aks`, kubelet client ID `84f24a2d-b552-4c80-b4eb-61c6bc556e87`, and an East US OIDC issuer URL, alongside the existing foundation outputs. These indicate user-run apply completion; node readiness and Kubernetes access still require verification. Next use Cloud Shell's existing Azure sign-in with normal `az aks get-credentials`, kubelogin's azurecli mode, and `kubectl get nodes`; do not use local account/admin credentials or access Azure from the local workspace.
- User successfully obtained cluster connection settings and used Azure CLI authentication. `kubectl get nodes -L agentpool` showed **four Ready nodes**, all v1.36.4: two `system` and two `apps`. This verifies Kubernetes API access and current node readiness, not application functionality or autoscaling.
- User also opened the OIDC issuer root in a browser and received WebContentNotFound (404). Microsoft documents discovery at the issuer's `/.well-known/openid-configuration` path; root 404 is not evidence of cluster failure. Provided the discovery URL for verification; successful discovery and workload federation have not yet been reported. Updated README and AKS guide to reflect deployed cluster status. Next inspect system pods with `kubectl get pods -A`.
- User supplied `kubectl get pods -A`: all listed kube-system pods were Running with all containers ready. Both workload-identity webhook replicas had one prior restart each; cause is unknown from this snapshot. No CrashLoopBackOff, Pending, or failed pods appeared. Next inspect warning events and check that restart counts do not keep increasing.
- User supplied successful OIDC discovery JSON: issuer matches the cluster URL, public signing-key metadata is advertised at `openid/v1/jwks`, and RS256 signing is supported. The earlier browser 404 was at the issuer root; the discovery path works. This verifies discovery metadata availability, not successful application token exchange or Azure resource authorization.
- Reviewed kube-system warning events: initial FailedScheduling entries occurred while only one node was available (untolerated taints and one host-port conflict); startup/readiness probes later reported connection refusals or HTTP 500/503 during component initialization. The most recent supplied events were around two minutes old. Combined with the earlier all-Ready pod snapshot, these suggest transient startup issues, but a fresh readiness/restart check is needed to confirm current stability. The taint names and causes of the two webhook restarts are not established; readiness probe failures alone do not explain container restarts. No corrective cluster configuration changes were made.
- Follow-up user-run `kubectl wait --for=condition=Ready pod --all -n kube-system --timeout=60s` succeeded for every listed pod. Fresh pod listing showed all containers ready and Running; both webhook replicas remained at one restart, with last restart about seven minutes earlier. This supports recovery from the observed startup warnings; no corrective configuration change is warranted from these snapshots. Long-term stability and the original restart cause remain unproven. Basic cluster readiness checks are complete; next planned component is managed Azure PostgreSQL and its private connectivity to AKS.

## 2026-10-18 - Private PostgreSQL draft and learning step

- User agreed to proceed after the plain-language explanation. Added `infrastructure/foundation/postgres.tf` for PostgreSQL Flexible Server 17, initial 32 GiB with growth, General Purpose `GP_Standard_D2ds_v5`, 14-day non-geo-redundant backups, private VNet integration, and required TLS. This initial server has no HA standby; throughput, restore, and availability targets remain unverified.
- Private DNS zone `trafficxx.postgres.database.azure.com` is linked to the lab VNet before server provisioning. Public database access is disabled. The application should use the server output FQDN rather than a fixed address. This uses delegated-subnet VNet integration, not a private endpoint.
- Selected Entra-only authentication with password access disabled. The human PostgreSQL administrator uses the explicitly chosen AKS human object's ID plus a supplied matching Entra principal name. No password is generated or placed in Terraform by this step. Server/database `prevent_destroy` rules reduce accidental Terraform deletion while retained in configuration; these do not replace backups.
- Added Microsoft.Storage service endpoint to the existing PostgreSQL subnet: Microsoft documents that the service adds it automatically for WAL archival, so tracking it prevents later Terraform drift/removal. Expected next plan is **six additions, one in-place subnet change, zero destructions**.
- Added server name/SKU/admin-name inputs and non-secret server FQDN/database outputs. Database creation depends on Entra administrator setup. No workload identities, SQL grants, schema migrations, or application connections are created yet; the Python DATABASE_URL path still needs Entra token refresh support.
- Formatting, local AzureRM 5.10.0 validation, and whitespace checks passed. No Azure account access was requested or used locally, and no Docker services were restarted. Actual database SKU availability, name availability, and Azure permissions are still unverified.
- Added `docs/postgresql.md` with architecture, learning notes, limits, user-run provider registration check, plan/apply sequence, and later AKS-based private connection verification. Updated README to link it.
- Packaged postgres.tf/main.tf/variables.tf/outputs.tf as ignored `test-results/trafficxx-postgres-update.zip`. This preserves the user's AKS file, private inputs, backend, and state. Next user-run steps are upload, validation, and `Microsoft.DBforPostgreSQL` registration check. No PostgreSQL plan/apply has been run.

## 2026-10-19 - PostgreSQL plan reviewed

- User resumed the PostgreSQL step, uploaded the update, and confirmed Cloud Shell validation succeeded and `Microsoft.DBforPostgreSQL` is Registered.
- Reviewed the user-supplied saved plan: **6 to add, 1 to change, 0 to destroy**. The sole existing-resource change adds Microsoft.Storage to the PostgreSQL subnet; no AKS or other existing resources are changed.
- Plan matches the intended PostgreSQL 17 server `trafficxx-pg-e8708a85`, GP_Standard_D2ds_v5, 32 GiB storage with growth, 14-day non-geo-redundant backups, private access, Entra-only authentication, human administrator, empty trafficxx database, private DNS zone/VNet link, and required TLS configuration.
- Plan saved as `postgres.tfplan` in the user's Cloud Shell. Next user-run action is to apply that reviewed plan; successful creation, database connections, migrations, and application access have not yet been reported or verified.
- PostgreSQL apply failed during server creation with HTTP 400 `ParameterOutOfRange: The value of the 'Version' should be in: []`. The empty list does not establish that version 17 alone is unsupported; subscription/region capabilities and SKU availability need investigation. DNS/link/subnet work may already have committed before failure; preserve state and inspect `terraform state list`. Requested user-run `az postgres flexible-server list-skus --location eastus --output json` before changing configuration. No blind version downgrade or infrastructure deletion was performed; generate a fresh plan after diagnosing and correcting the cause.
- User supplied `terraform state list`: PostgreSQL private DNS zone and VNet link are tracked along with the existing AKS/foundation resources; PostgreSQL server, administrator, database, and TLS configuration are absent. Preserve that partial progress.
- User's East US `list-skus` output confirms the blocker: `reason` explicitly says provisioning is restricted in the region; normal `supportedServerEditions` and `supportedServerVersions` are empty, and OfferRestricted is Enabled. Fast-provisioning template entries are not evidence that normal server creation is permitted. The misleading version error reflects unavailable subscription/region capabilities, not evidence that PostgreSQL 17 must be downgraded.
- Microsoft capacity-error guidance documents regional access restrictions for program offers and region-access support requests. Next read-only checks: East US 2 and Central US capabilities. No region or version has been changed. Keeping the existing single-region private design requires East US access; moving only the database to another region would need a regional VNet/subnet and deliberate private routing/DNS design. Do not change the shared location input and recreate existing AKS/network resources merely to bypass this restriction.
- User supplied alternate-region results: East US 2 is restricted with an empty version list; Central US has no restriction reason and advertises versions 11-18, including PostgreSQL 17. Specific SKU capacity and server creation remain unverified.
- Prepared a Central US recovery draft without changing the shared East US location: new database VNet `10.50.0.0/16`, delegated PostgreSQL subnet `10.50.0.0/24` with storage endpoint, two directional peering resources, and an additional link to the existing private DNS zone. PostgreSQL uses `postgres_location` (default centralus) and the new regional subnet. Existing AKS/network/DNS resources and the unused original PostgreSQL subnet are preserved; postgres_subnet_id output now refers to the regional subnet.
- Serialized the two Azure peering operations. Private database access, Entra-only authentication, TLS, version 17, backup settings, and deletion guards remain as drafted. Cross-region latency/data-transfer charges need measurement; this design is not multi-region database HA or DR.
- Local Terraform formatting/schema validation and non-overlapping VNet/pod/service CIDR checks passed; no local Azure access was used. Expected fresh plan against the supplied partial state: **9 to add, 0 to change, 0 to destroy**, excluding output-value changes. No Central US deployment has occurred.
- Updated PostgreSQL guide and packaged four files as ignored `test-results/trafficxx-postgres-centralus-update.zip` (postgres-network.tf, postgres.tf, variables.tf, outputs.tf). User should upload, validate, and create a fresh postgres.tfplan for review; do not reuse the failed saved plan.
- Reviewed the user-supplied Central US recovery plan: **9 to add, 0 to change, 0 to destroy**, exactly as expected. It adds the regional VNet/subnet/DNS link, both private peerings, and the four pending PostgreSQL resources. Version 17, Central US, private-only server access, Entra-only authentication, required TLS, storage, and backup settings match the draft. Existing AKS and network resources are preserved. postgres_subnet_id changes only as an output to identify the new regional subnet; the original subnet is not deleted. New plan saved as postgres.tfplan; Central US apply completion remains unverified.
- User supplied outputs after the Central US apply: PostgreSQL server `trafficxx-pg-e8708a85`, database `trafficxx`, FQDN `trafficxx-pg-e8708a85.postgres.database.azure.com`, location centralus, and regional subnet under `trafficxx-lab-postgres-vnet`. This indicates the recovery apply completed. Existing AKS, ACR, and foundation outputs remain present. All Azure work was user-run; no local account access was used.
- Next verify private DNS resolution and PostgreSQL readiness using a temporary postgres:17 client pod on the AKS apps pool, with service-account token automount disabled and automatic cleanup. Expected DNS is in 10.50.0.0/24 and pg_isready should report accepting connections. This is not an authenticated SQL query or TLS certificate verification; Entra token exchange, database permissions, schema migrations, and application connectivity remain untested.
- User ran the network probe from AKS: DNS resolved to **10.50.0.4**, and pg_isready reported **accepting connections** on port 5432. Kubectl's attach attempt timed out and it fell back to log streaming, which captured the successful result. This verifies private DNS and PostgreSQL readiness across regions, not database login or TLS certificate verification. No token or password was supplied in that probe.
- Added `scripts/check-postgres-auth.sh` for the next user-run Cloud Shell check. It guards subscription/context, creates a unique non-root client pod on apps nodes with no service-account token mount, waits for it to start, pipes a temporary Entra token over stdin into one exec process, and runs a read-only SQL query with verify-full TLS and the CA trust bundle. Token contents are not included in pod settings, command arguments, files, or printed output. The script cleans up its client pod on ordinary exit; no application roles or schema are changed.
- Git Bash syntax and whitespace checks passed locally; no local Azure account access or token acquisition was attempted. Documented the readiness evidence and next auth/TLS test. Authenticated SQL and certificate verification remain pending user-run results.
- First user-run auth check failed before database login: postgres:17 lacked the assumed CA bundle path. The exec process exited before reading stdin, followed by a BrokenPipeError from the token-producing command. This was a diagnostic client prerequisite bug; it does not demonstrate an Azure database authentication failure.
- Fixed the script to locate Cloud Shell's trusted public PEM CA bundle (or explicit TRAFFICXX_CA_BUNDLE), copy it into the non-root pod at /tmp/trafficxx-ca.crt, and verify bundle/client prerequisites before requesting a token. Kept verify-full TLS; no certificates were accepted without verification. No local Azure access or infrastructure changes were made.
- Git Bash syntax and four stubbed-flow checks passed: normal success, client preflight failure (no token requested), SQL failure, and missing local bundle (no pod/token). Checked cleanup on ordinary success/failure and that a fake token was absent from captured output. Harness/evidence remain ignored under test-results/ci/. These tests verify script control flow only; real Entra login and TLS verification await the user's rerun with the updated script.
- User reran the updated script successfully from Cloud Shell. Read-only SQL from an AKS apps-node client returned database **trafficxx**, the configured human Entra identity, **ssl=t**, and **TLSv1.3**. The client used verify-full with the copied trusted CA bundle, verifying authenticated SQL and certificate/hostname-verified TLS across the private regional peering. The script reported success and invoked temporary-pod cleanup. No schema or application data was changed by this query.
- Private PostgreSQL now has user-run evidence for DNS, readiness, Entra administrator login, and verified TLS. Next create separate application/migration identities and database permissions, add application token refresh, and run migrations; do not deploy APIs using the human administrator identity. Workload federation, least-privilege SQL access, restore behavior, and application performance remain untested.

## 2026-10-19 - Workload identity preparation

- User agreed to continue after the administrator SQL/TLS milestone. Prepared `infrastructure/foundation/workload-identities.tf`: distinct managed identities for browse, booking, expiry, publisher, consumer, operations, migrator, and manual reconciler.
- Each has a separate federated credential scoped to the existing AKS issuer, exact `system:serviceaccount:trafficxx:<name>` subject, and AzureADTokenExchange audience. Uses AzureRM 5.10.0's user_assigned_identity_id field. Expected plan: **16 additions, 0 changes, 0 destructions**. No SQL, Service Bus, or Kubernetes admin permissions are granted by this draft.
- Added non-secret identity metadata outputs and a JSON Kubernetes manifest output for the trafficxx namespace/eight service accounts, with matching client/tenant annotations and default Kubernetes API-token automount disabled. AzureRM will not install those objects; export/review/kubectl apply follows Azure identity creation. Future pod templates must use the matching account plus azure.workload.identity/use=true for the projected Azure exchange token.
- Added `docs/workload-identity.md` explaining identity versus trust versus authorization, planned responsibilities, namespace access implications, and staged user-run plan/apply/service-account steps. Linked from README. No application deployment or workload federation test has run.
- Local formatting, AzureRM validation, and whitespace checks passed. Packaged only the new Terraform file as ignored `test-results/trafficxx-workload-identities-update.zip`. No local Azure account access was used and no infrastructure was applied.
- Code review also confirmed the local migration runner requires an existing outbox table. The empty Azure database needs an ordered schema bootstrap before additive migrations; address this alongside application token refresh and SQL grants in the next stage.
- Reviewed the first user-run workload identity plan. All eight issuer/audience/exact subject bindings and managed identities match, but the plan is **16 additions, 1 change, 0 destructions**: it unexpectedly wants to clear PostgreSQL's Azure-assigned zone 1 to null. This unrelated database update should be removed from the identity rollout before apply.
- Added `ignore_changes = [zone]` only to the PostgreSQL server's existing lifecycle block, retaining prevent_destroy. Azure owns placement because no zone was explicitly requested; other server settings remain managed. This follows the pinned provider's documented zone handling. If intentional zone control is later needed, revise that rule explicitly.
- Formatting and local provider validation passed. Packaged updated postgres.tf alone as ignored `test-results/trafficxx-postgres-zone-update.zip`, preserving the user's workload identity draft and backend/state. No real infrastructure change was executed; user must generate and share a fresh workload-identities.tfplan expected to contain 16 additions and zero existing-resource changes.
- Reviewed the user's fresh saved workload identity plan after the zone fix: **16 to add, 0 to change, 0 to destroy**. Confirmed eight managed identities and eight exact issuer/audience/service-account trust bindings. PostgreSQL is refreshed but has no planned update; the unintended zone change is gone. Saved plan is workload-identities.tfplan. Identity creation and Kubernetes service-account installation remain pending user-run apply/results.
- User supplied outputs following identity apply: all eight workload identity records now contain concrete client/principal/resource IDs and the expected trafficxx service-account subjects, alongside the generated Kubernetes manifest. This indicates user-run identity/federation creation completed. No local Azure account access was used.
- Parsed the actual manifest from the user's output and verified one trafficxx namespace, eight distinct expected service accounts, client-ID mappings matching the corresponding identity outputs, and disabled default token automount. Namespace/service-account installation is the next user-run step, via Terraform's raw manifest output and kubectl apply. SQL authorization, actual token exchange, and application workloads remain pending.
- User applied workload-serviceaccounts.json successfully: trafficxx namespace plus booking, browse, consumer, expiry, migrator, operations, publisher, and reconciler accounts were created. Listing also showed the normal Kubernetes default account. This verifies installation, not federation or SQL authorization.
- Prepared `kubernetes/checks/workload-identity.yaml`, a temporary non-root apps-node Job using the browse account and workload-identity webhook label. It checks injected client/tenant IDs against the provided browse outputs, reads the projected token, and requests an Entra PostgreSQL-scoped token over verified HTTPS. Output includes only result/subject/client ID/expiry; no bearer tokens or raw error responses. It performs no SQL or data mutation. Active deadline is 120 seconds and finished-job TTL is 600 seconds.
- Verified the official Python image Dockerfile includes CA certificates, avoiding the earlier PostgreSQL-image assumption. The diagnostic uses standard-library OAuth exchange solely for this check; application SDK credentials/refresh remain a later change.
- Local embedded-Python syntax and four mocked checks passed: success, identity mismatch, subject mismatch, and Entra HTTP error. Mismatch cases request no token; no fake secret appeared in captured output. Harness is ignored under test-results/ci/. Updated workload-identity guide and README. Real browse token exchange and all other workload federation/SQL permissions remain pending user-run output; no local Azure account access was used.
- User supplied real Job logs: **TOKEN_EXCHANGE_OK** for `system:serviceaccount:trafficxx:browse`, matching client ID `851cbe64-04d0-416a-8ebb-7c43b9e18aa5`, with expires_in_seconds 86399. This verifies browse service-account token projection and Entra exchange for the PostgreSQL scope without the human CLI login. Token contents were not shared. It does not verify SQL authorization or the other seven identities; cleanup is configured by Job TTL but not independently confirmed.
- Next stage is explicit PostgreSQL identity-to-role mapping and separate permissions for browse, booking, workers, operations, reconciler, and migrator, followed by schema bootstrap/migrations and positive/negative access tests. Do not use the human administrator identity for application runtime. No SQL grants or application deployments were performed during this verification.

## 2026-10-19 - Pause checkpoint for GitHub

- User requested pausing work until tomorrow and pushing the code to the existing GitHub repository. Resume on 2026-10-20 (Asia/Karachi) when the user returns; no scheduled automation was requested.
- Completed cloud evidence: AKS in East US with four Ready nodes; PostgreSQL 17 in Central US after East US restrictions; private DNS/peering; human Entra SQL login with verified TLS 1.3; eight workload identities/trust records and eight Kubernetes service accounts; real browse token exchange succeeded.
- Resume at PostgreSQL identity-to-role mapping and SQL permissions, then ordered schema bootstrap/additive migrations and application token refresh. The existing local migration runner assumes an outbox table already exists; do not blindly run it against the empty Azure schema. Test allowed and denied operations before deploying APIs/workers. Azure Service Bus and application deployment remain ahead.
- Azure commands must continue to be executed personally by the user in browser Cloud Shell, from ~/trafficxx/foundation. Remote foundation state is in the existing Azure storage backend; bootstrap's own state is still in the user's Cloud Shell bootstrap folder. Never request state files or tokens in chat.
- Azure resources remain running under the user's earlier continuous-operation preference. Pausing work does not stop or destroy them. Local Docker services were previously stopped and have not been restarted by these infrastructure checks.
- Publication includes Terraform sources/provider lock files, examples, diagnostic script/Job, documentation, and this work log. Real .env, private tfvars/backend inputs, Terraform state/plans/provider binaries, generated ZIPs, and test evidence remain ignored.
- Pre-push checks: formatting and validation passed for both Terraform roots; Bash syntax and four auth-script flow checks passed; four workload-probe mocked checks passed, including mismatch rejection and no test tokens in output. These are local checks plus the earlier user-run Azure evidence, not a new hosted CI result. An initial check command referenced the auth harness with the wrong .py extension; reran the actual .sh harness successfully.
- User additionally requested keeping sensitive local settings in .env. Inspected only its key names in output: it previously contained the license acceptance setting. Added the existing disposable emulator SQL password as SERVICEBUS_SQL_PASSWORD in ignored .env, preserving the current Compose value and all existing settings; no credential rotation or container restart was performed. Actual Azure authentication is Entra/workload identity, so there is no cloud database password to save and temporary tokens must not be persisted. Confirmed no .env/private tfvars/backend/state/plan files are tracked. .env.example remains a documented disposable example; real private overrides belong only in .env. Work remains paused.
- User invited a README update. Reworked it around verified progress as of 2026-10-19, the actual East US AKS/Central US PostgreSQL layout, application/cloud limits, safe local configuration, and the exact SQL-permissions/schema/token-refresh resume point. Marked locally implemented functional items and replaced the hypothetical directory tree with the real layout. Preserved the broader roadmap as explicitly future work. Checked all 12 local Markdown links, code-fence balance, and whitespace; no runtime code or infrastructure changed. Documentation publication includes the pending .env.example comment and this log; actual .env remains ignored. Work remains paused.
