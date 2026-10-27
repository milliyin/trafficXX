# ClickClash implementation and release

Implemented **28 October 2026**. ClickClash is now a working React/TypeScript
frontend and FastAPI/PostgreSQL application in this repository. It has been
tested locally against disposable PostgreSQL, including real browser flows.
The ClickClash Azure resources and public game have **not** been deployed.
The existing ticket services and their verified Browse benchmark are unchanged.

## What works

- `/`: public all-time leaderboard, ranked accounts, click totals, immediate
  bio/website/image promotions and report controls.
- `/play`: unlimited-duration guest/account play, confirmed scores, pending
  requests, retries using the original click ID, and optional automatic clicks.
- `/account`: email signup, one-time five-word generated passphrase, login,
  logout, profile editing, image upload/removal and account deletion.
- Guest play stays unranked until signup or authenticated account linking.
  Linking transfers the guest score and receipt ownership atomically.
- `/status`: reads the independent external status snapshot. The standalone
  [public status page](public-status/index.html) is hosted alongside the docs,
  outside AKS, and remains unknown until probes are enabled.

No fake scores or players ship with the application. Browser test records live
only in disposable test databases. Email is an unverified private login name;
the application sends no verification messages or reset emails. There is no
password recovery. Passphrases are generated with `secrets.choice` from the
7,776-entry EFF list and stored using salted Argon2id hashes. The one-time
plaintext response is not cached, logged or placed in browser storage.

## Scoring and capacity

One click request contains only a UUID. An atomic PostgreSQL transaction
records that UUID and adds one point. Repeating it cannot add another point;
a UUID belonging to another player returns a conflict. Scores and receipts
survive application restarts. Public clients cannot choose their score.

There is **no individual click rate cap**. The initial shared admission budget
is **120 click requests/second** and **16 concurrent scoring transactions**,
enforced through database state and transaction-scoped advisory locks across
replicas. These are initial configured ceilings, not measured throughput or a
500-user click-write guarantee. Each of the two proposed API replicas has at
most four database connections; normal steady connection cap is eight, with
up to four more during a one-pod rolling surge. This is separate from Browse's
connection budget. The total application database budget needs the extra
connections included before launch.

Overload and pause return explicit 503 responses without creating a new score.
If a connection is interrupted after commit, retrying the original UUID resolves
the uncertainty. The browser retains at most 64 unconfirmed IDs in session
storage; losing this local retry list does not erase server-accepted scores.
The UI limits in-flight browser requests to eight for bounded client memory;
it does not impose a server-side individual click rate limit. Its built-in
automatic mode sends up to ten requests/second and stops on errors.

Signup/login have separate shared and IP-based limits, plus bounded Argon2
concurrency. The gateway overwrites the client-IP header and the application
only uses it from the explicitly configured AKS pod network. Direct public
clients cannot supply a trusted proxy identity. Uvicorn's request concurrency,
body-size limits, database pool limits and container resource limits add bounds.
There is no ClickClash autoscaler yet: test the write path before enabling one.

## Public content and data

Bio text is limited to 180 characters, nickname to 32, and links to web URLs.
React renders text without interpreting HTML. External promotion links are
marked `ugc sponsored nofollow` and opened with `noopener noreferrer`.
No third-party scripts or ad embeds are accepted.

Images must be still PNG/JPEG/WebP, at most 1 MiB and 2048 by 2048 pixels. The
server decodes, resizes, removes metadata and re-encodes them as WebP. Each
stored image is at most 512 KiB and the shared image storage ceiling is 100 MiB.
Images are stored in PostgreSQL, not on ephemeral pod disks. The operator can
hide, remove or ban a promotion/player through private database access. Editing
a hidden promotion does not automatically unhide it.

Account deletion removes credentials, public profile, sessions and score.
Anonymous UUID receipt tombstones remain so an old retry cannot be reused to
score again. Currently raw receipts are retained indefinitely; design a bounded
receipt/retry-validity policy before extended high-volume public operation.
There are no scheduled score resets. Private operator cleanup removes expired
sessions and old rate buckets; schedule it when releasing. Backups and restore
verification must cover the separate schema, images and migration ledger.

## Run locally

Use a separate project and database. Do not point these tests at Azure.
The following optional Compose command starts its own local PostgreSQL and
application; it does not modify the ticket Compose project:

```bash
docker compose --env-file .env.example -f compose.clickclash.yml up --build
```

Open `http://127.0.0.1:8088`. The explicit local mode permits non-secure cookies
only for the documented loopback origin. Azure always uses HTTPS cookies.
Keep the `clickclash-data` volume to retain records.

Without Docker, install PostgreSQL, create a development database, install
`requirements-clickclash.txt`, set `DATABASE_URL`, build the frontend and run:

```bash
cd clickclash-web
npm ci
npm run build
cd ..
python -m trafficxx.clickclash_migrate
export CLICKCLASH_ORIGIN=http://127.0.0.1:8088
export CLICKCLASH_INSECURE_LOCAL=1
python -m uvicorn trafficxx.clickclash:app --host 127.0.0.1 --port 8088 --no-access-log
```

The API serves the production frontend. `npm run dev` is for frontend editing;
the production build plus API origin is the verified full application path.

## Azure release from Cloud Shell

Run cloud commands in your own Cloud Shell. No local Azure credentials are
needed for repository development. Start with the image build:

```bash
cd ~/trafficxx
git pull --ff-only origin main
python3 scripts/clickclash-azure.py build
```

If the helper reports a failure, pull the latest code and run
`python3 scripts/clickclash-azure.py doctor`. It checks the subscription and
registry and lists recent build statuses without starting a build or requiring
a Kubernetes context. Failures identify a fixed operation name, exit code and
timeout flag without exposing raw command output. Building an image does not
require AKS credentials; preparing/deploying/checking still require the exact
lab cluster context.

For a fresh Cloud Shell checkout, generate the plan with:

```bash
python3 scripts/plan-clickclash.py
```

This reconnects to the existing `lab/foundation.tfstate` backend, checks the
recorded subscription/administrator and existing resources, persists the
ClickClash/Grafana/KEDA opt-ins in an ignored settings file, and passes those
flags explicitly to Terraform. It never applies. It rejects unrelated creates,
updates or deletes and prints only a safe summary plus the uniquely named
saved plan path. Expect four additions: identity, federation, public IP and
the narrowly scoped AKS attachment role. Existing managed resources must stay
unchanged. Review the summary before applying that exact plan in Cloud Shell.
The public hostname comes from the reserved IP; it is not the Grafana hostname.

Once those resources exist:

```bash
python3 scripts/clickclash-azure.py prepare
# Review test-results/clickclash/azure-register.sql before running it.
bash scripts/run-postgres-admin-sql.sh test-results/clickclash/azure-register.sql
```

The generated SQL registers the exact managed identity and creates only the
separate ClickClash schema and ledger. The runtime role receives DML privileges
for that schema, no DDL or operator control privileges, and no ticket grants.
Stage the application privately first:

```bash
python3 scripts/clickclash-azure.py stage
```

This applies only the application, workload service account and ClusterIP
service. Readiness checks the real database connection; no public listener or
synthetic player clicks are created. It refuses if a public ClickClash Service
already exists, since staging is not a command to withdraw an existing site.

Review backup coverage and rehearse restoring records before inviting players.
Then deploy and verify trusted public HTTPS:

```bash
python3 scripts/clickclash-azure.py deploy
python3 scripts/clickclash-azure.py check
```

The gateway uses its own persistent certificate volume and exposes only ports
80/443. App Services stay ClusterIP. Pod readiness is not certificate proof:
require `CLICKCLASH_HTTPS_OK`, then test browser signup, a confirmed click,
profile editing and login through the returned HTTPS URL. Live cloud and public
browser checks remain pending; local tests do not replace them.

## Independent status monitoring

After the public URL passes HTTPS checks, set GitHub repository variable
`CLICKCLASH_PUBLIC_URL` to that exact HTTPS origin. Enable/run the
**ClickClash external status** workflow. Its scheduled job probes the website,
readiness and leaderboard from outside AKS, records actual results and requests
a docs Pages refresh. Without that variable the job is skipped.

Status is served at `https://milliyin.github.io/trafficXX/public-status/`.
History starts at activation and retains 288 checks; each bar is an actual
sample, not fabricated daily uptime. Evidence older than 15 minutes is unknown.
GitHub schedules and Pages deploys may be delayed. The probe does not validate
authenticated scoring, existing private ticket services, or send notifications.
Authenticated write canaries, incident publishing and operator notifications
remain separate work. The status workflow needs contents/actions write rights
to store snapshots and dispatch the existing docs publisher.

## Private operator actions

`python -m trafficxx.clickclash_admin` supports `pause`, `resume`, `hide-ad`,
`show-ad`, `remove-ad`, `ban`, `unban`, `reports` and `cleanup`. It requires a
private operator database connection with the relevant privileges. The public
runtime identity intentionally cannot change pause/ban controls or read the
operator audit. Do not give its role administrator privileges to run these
commands. Each mutation is recorded in the private operator audit.

For Cloud Shell, use the existing reviewed administrator SQL helper with
ClickClash-scoped SQL. For example, a pause transaction is:

```sql
\connect trafficxx
BEGIN;
UPDATE clickclash.control SET paused=true WHERE id;
INSERT INTO clickclash.operator_audit(action) VALUES ('pause');
COMMIT;
```

Never paste credentials into chat or commit them to the repository. Access
logs are disabled in the application image and credentials never appear in
public API errors. Operator permissions remain separate from player accounts.

## Verification

The independent `ClickClash correctness` workflow runs disposable PostgreSQL
tests and a real-browser account/game/profile flow. Locally verified: 17 new
PostgreSQL integration tests, the browser flow at desktop and 390-pixel mobile
width, TypeScript/Vite production build, and the existing 552 offline tests.
A local dump/restore rehearsal matched scoring records and the migration ledger;
it does not establish Azure backup/restore coverage. Terraform validation,
gateway configuration validation and workflow linting also passed. The frontend
lockfile audit reported no known advisories at this checkpoint.
This is functionality evidence, not a write-load SLO or production reliability
certification. Docker image/ACR, Azure rollout, public TLS and longer load checks
remain required release evidence.

References: [OWASP password storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html),
[OWASP uploads](https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html),
[EFF word list attribution](https://github.com/milliyin/trafficXX/blob/main/trafficxx/data/README.md).
