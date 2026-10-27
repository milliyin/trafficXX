# ClickClash: public click challenge and service status

Design updated **28 October 2026**. This is the implementation plan for a real
public service, requested after the completed 500-user Browse verification.
The challenge, authentication and public status service are not implemented
or deployed yet. Real player records will be durable application data.

## Product scope

The public product name is **ClickClash**. Automatic clickers may compete and
generate heavy traffic; manual-only play is not the competition policy. The user wants
people to push the service rather than selecting a low per-player limit.
Whether to omit a per-player click limit remains to be clarified. A finite
global admission ceiling, connection budget and operator pause are still
proposed; do not interpret this preference as permission for unlimited costs.
Do not promise one account per human.

A small responsive React and TypeScript website offers unlimited-duration
clicking, guest play, optional email sign-in, personal scores and a shared
leaderboard. Guests can play, but an account is required to keep a public
leaderboard entry. This supersedes the earlier guest-ranking preference.
There is no countdown or round
time limit. Scores accumulate as all-time totals with no scheduled resets.
The server awards points only for
accepted clicks. The interface distinguishes pending requests, accepted
scores, rate limiting and temporary unavailability.

The public leaderboard is a separate page from the clicking game. Each entry
shows the player's public nickname, accepted click total and optional bio/ad
beside it, visible to visitors. The optional bio/ad supports short text, one
website link and an image. New and edited content appears immediately without
prior approval; the operator can hide or remove it. Profile editing requires
the owning guest session or authenticated account. Arbitrary HTML, scripts and
third-party ad embeds are outside this format.

Image uploads require bounded file sizes and dimensions, validated image
formats and safe delivery as non-executable content. Links require validated
web URL schemes. Reporting and operator removal controls must be available
for publicly displayed content; hiding an ad does not reset a player's score.

The click itself performs the scoring transaction. It does not call an
unrelated ticket-availability endpoint merely to manufacture traffic. Existing
Browse and Booking remain separate application paths. The measured 500-user
Browse result is not a capacity claim for this new write workload.

## Application shape

- A stable public HTTPS hostname routes the website and its API through the
  same origin to avoid unnecessary cross-origin authentication complexity.
- A separate FastAPI challenge deployment runs on the existing AKS foundation.
  It has its own database identity, connection pool and reviewed resource limits.
- Private PostgreSQL stores the authoritative player, profile, receipt and score
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
public profiles, click receipts and per-player cumulative scores. Score ordering and ties
have explicit deterministic rules. Different players do not contend on one
global score row.

An accepted click must atomically validate player eligibility and server-side
limits, insert a unique receipt, and update that player's score in PostgreSQL.
Points are acknowledged after commit. The server uses database time and
enforces eligibility and operator pause behavior consistently across replicas.

Clients generate a unique identifier for each click and reuse it when retrying
that click. Replaying a committed request returns its recorded outcome without
another increment; changing the payload under the same identifier is rejected.
A connection failure after commit must be recoverable by the same identifier.
The browser never supplies a trusted score or player identity.

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
Guest scores remain unranked until linked to an account. Clearing the browser
session or changing devices cannot establish ownership of an unlinked guest.
The UI labels guest and signed-in identities clearly. Sign-in alone is not
proof that traffic is human.

Game management, pause controls and incident publishing require operator
authorization. Public users cannot start arbitrary generators, submit target
URLs or change request budgets. Per-player request limits and a
global admission budget bound public traffic. Scores advance only on server
acceptance, and overload responses remain visible rather than becoming fake
successful clicks. Public display names and bio/ad content are validated and
escaped. Any allowed destination link is a player promotion, not a URL the
server fetches or load-tests.

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
functional canary uses a dedicated synthetic identity excluded from rankings, rather than
adding fake scores to real leaderboards. Authenticated internal health signals
can enrich component status through a sanitized public snapshot; private
Grafana, Prometheus, database addresses and operational details are not exposed.
Operator alerts are a separate delivery path and must be configured and tested.

## Real usage and controlled tests

Real-player records are never reset to clean up a load test. Automated runs use
explicit synthetic identities excluded from public rankings, with
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
| 1. Durable scoring | Schema, transactional receipts/scores, guest sessions and integration checks for duplicates, concurrency, operator pause and restart survival. |
| 2. Player experience | React/TypeScript, separate unlimited-duration game and public leaderboard pages, persistent all-time scores, immediate text/link/image bio/ads with operator hide/remove controls and clear overload/retry behavior. |
| 3. Sign-in | Email and generated memorable passphrases, one-time credential display, no recovery, guest linking, session lifecycle, ownership checks and protected operator actions. |
| 4. Public release | Reviewed HTTPS gateway, scoped database identity, shared rate limits, backups/restore evidence and a small real-user launch. |
| 5. Status and operation | Independent probes/history, public status and incident page, tested operator notifications. |
| 6. Reliability evidence | Separate synthetic load, longer runs, controlled failure/recovery and observed data consistency before increasing budgets. |

Implementation begins with correctness, not an in-memory score placeholder.
Deployment costs and connection budgets are reviewed against the existing
Azure foundation before introducing new resources. The user has Azure
sponsorship, described as lasting five years; its exact expiry, remaining
credits and an approved spending ceiling are not yet established.
Sponsorship is not authorization for unlimited resource
spending. No public endpoint, new
cloud resource, account system or synthetic workload was created by this
planning update.

References: [OWASP password storage guidance](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html),
[current verified progress](progress.md), [system architecture](architecture.md).
