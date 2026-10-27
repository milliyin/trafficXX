# Public click challenge and service status

Design updated **28 October 2026**. This is the implementation plan for a real
public service, requested after the completed 500-user Browse verification.
The challenge, authentication and public status service are not implemented
or deployed yet. Real player records will be durable application data.

## Product scope

A small responsive website offers timed click rounds, guest play, optional
email sign-in, personal scores and a shared leaderboard. Visitors can join
the same countdown and compete. The server awards points only for accepted
clicks during an active round. The interface distinguishes pending requests,
accepted scores, rate limiting and temporary unavailability.

The click itself performs the scoring transaction. It does not call an
unrelated ticket-availability endpoint merely to manufacture traffic. Existing
Browse and Booking remain separate application paths. The measured 500-user
Browse result is not a capacity claim for this new write workload.

## Application shape

- A stable public HTTPS hostname routes the website and its API through the
  same origin to avoid unnecessary cross-origin authentication complexity.
- A separate FastAPI challenge deployment runs on the existing AKS foundation.
  It has its own database identity, connection pool and reviewed resource limits.
- Private PostgreSQL stores the authoritative player, round, receipt and score
  records in a separate schema with a least-privilege application role.
- Start with database-backed scoring. A cache may accelerate leaderboard
  reads after measurement; cache loss must not erase accepted scores.
- Existing ticket inventory, operational endpoints and metrics are not part of
  the public challenge API. Public routes are explicitly allowlisted.

The visual scope stays small: a play page, leaderboard, sign-in/account controls
and a link to public status. Production data requirements do not require a
large UI or additional microservices.

## Real-data correctness

The initial data model separates players, account credentials, server sessions,
rounds, click receipts and per-player round scores. Score ordering and ties
have explicit deterministic rules. Different players do not contend on one
global score row.

An accepted click must atomically validate round eligibility and server-side
limits, insert a unique receipt, and update that player's score in PostgreSQL.
Points are acknowledged after commit. The server uses database time and
defines the eligibility/round-close boundary consistently across replicas.

Clients generate a unique identifier for each click and reuse it when retrying
that click. Replaying a committed request returns its recorded outcome without
another increment; changing the payload under the same identifier is rejected.
A connection failure after commit must be recoverable by the same identifier.
The browser never supplies a trusted score, player identity or round deadline.

Restarting or replacing an API pod must preserve accepted scores. Schema
migrations are versioned; backups and an actual restore rehearsal are required
before accepting public records. Raw receipts have bounded retention and
aggregates are retained separately, with a published retention policy.

## Identity and public controls

Guests receive an opaque server session and a public nickname. A guest session
identifies a browser, not a verified human. Account signup accepts an email and
generates a memorable passphrase, shown once in the browser for the player to
save. Later sign-in uses that email and passphrase. There is no GitHub login,
verification email, emailed password, verification code or password-reset flow.

Generate five independently random words using a cryptographically secure
generator and a vetted large word list. Do not derive passwords from names,
email addresses or dates. Store only a salted Argon2id hash, never the plaintext
passphrase. Signup must never replace an existing account's password or reveal
its credentials. Rate-limit signup and login across replicas.

The email is a private, unverified login identifier: it does not prove mailbox
ownership or a player's identity. Only the public nickname appears on rankings.
Before signup completes, clearly explain that losing the passphrase means losing
access once the player is signed out or their session expires. Forgotten
credentials do not delete records, release the email or permit another signup
to take over the account. No recovery through email or operator reset is offered.

Application sessions require secure, HttpOnly cookies, session expiry, logout,
revocation and protection against cross-site request forgery. Guest-to-account
linking requires the existing guest session and an authenticated account, with
an atomic merge rule; clients cannot claim an arbitrary guest or account ID.
The UI labels guest and signed-in identities clearly. Sign-in alone is not
proof that traffic is human.

Round management, pause controls and incident publishing require operator
authorization. Public users cannot start arbitrary generators, submit target
URLs or change request budgets. Round deadlines, per-player limits and a
global admission budget bound public traffic. Scores advance only on server
acceptance, and overload responses remain visible rather than becoming fake
successful clicks. Public display names are validated and escaped.

Only necessary profile information is stored. Logging excludes passphrases,
tokens, cookies and credentials in request bodies or headers. Account deletion, guest cleanup
and public-name visibility have defined behavior before launch.

## Public status

The public status page follows the useful structure of
[GitHub Status](https://www.githubstatus.com/): overall condition, component
health, daily uptime bars, latency history, maintenance and incident updates.

Host the status frontend outside the AKS cluster and probe the public HTTPS
path from outside it. Persist probe history independently of the monitored
cluster. Start measurement history at monitoring activation; do not fabricate
earlier uptime. Show the last check time and report missing/stale evidence as
unknown. Availability percentages describe their actual probe coverage.

Initial components are the website, click API, login and leaderboard. A
functional canary uses a dedicated synthetic identity and round, rather than
adding fake scores to real leaderboards. Authenticated internal health signals
can enrich component status through a sanitized public snapshot; private
Grafana, Prometheus, database addresses and operational details are not exposed.
Operator alerts are a separate delivery path and must be configured and tested.

## Real usage and controlled tests

Real-player records are never reset to clean up a load test. Automated runs use
explicit synthetic identities and rounds excluded from public rankings, with
separate metrics labels and cleanup rules. Recovery tests use those records
while checking that real records remain intact.

The existing 500-user Browse soak remains pending. After the click path exists,
it needs its own baseline for reads, writes and authentication, followed by
bounded longer runs and recovery checks. Public browsing, clicks/second and
concurrent requests are different measures. A public scoreboard is not proof
of a user count or latency guarantee.

## Build and launch sequence

| Stage | Concrete outcome |
| --- | --- |
| 1. Durable scoring | Schema, transactional receipts/scores, guest sessions and integration checks for duplicates, concurrency, round close and restart survival. |
| 2. Player experience | Responsive play page, shared countdown, server-confirmed score, leaderboard and clear overload/retry behavior. |
| 3. Sign-in | Email and generated memorable passphrases, one-time credential display, no recovery, guest linking, session lifecycle, ownership checks and protected operator actions. |
| 4. Public release | Reviewed HTTPS gateway, scoped database identity, shared rate limits, backups/restore evidence and a small real-user launch. |
| 5. Status and operation | Independent probes/history, public status and incident page, tested operator notifications. |
| 6. Reliability evidence | Separate synthetic load, longer runs, controlled failure/recovery and observed data consistency before increasing budgets. |

Implementation begins with correctness, not an in-memory score placeholder.
Deployment costs and connection budgets are reviewed against the existing
Azure foundation before introducing new resources. No public endpoint, new
cloud resource, account system or synthetic workload was created by this
planning update.

References: [OWASP password storage guidance](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html),
[current verified progress](progress.md), [system architecture](architecture.md).
