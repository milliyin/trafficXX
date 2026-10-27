# Banner storage migration and security review

Prepared 28 October 2026. Source and local tests are separate from live Azure
verification. Do not deploy the new app before applying the additive schema.

The dependency audit flagged Pillow 12.3.0. This release upgrades the image
processor to Pillow 13.0.0, requiring Python 3.11 or newer (the deployed image
uses Python 3.12). CI now runs backend and frontend dependency audits as well
as correctness tests. A clean advisory scan is not proof of vulnerability absence.

## Storage design

Banner files move to the private `banners` container in
`trafficxxcce8708a85`. PostgreSQL keeps the owner, generated object name,
SHA-256 digest and byte count in `clickclash.image_blobs`. Existing
`clickclash.images` records remain readable until migrated.

The application uses its projected Azure workload identity. There are no
storage keys, connection strings, browser SAS tokens or public upload endpoints.
The custom role allows blob read/write/delete and container read, scoped to this
container; it does not grant key listing, SAS delegation or container deletion.
Anonymous access and Shared Key authentication are disabled. The storage service
has an authenticated public network endpoint; this is **not** a private endpoint.

Browsers continue using `/api/images/{player_id}`. Every request checks the
current account, ban and ad visibility before accessing storage. Private object
paths are never sent to the browser. This preserves immediate moderation and
same-origin image loading; application bandwidth is still used to deliver images.

## Cloud Shell rollout

Run from the existing Azure Cloud Shell, not a locally authenticated Azure CLI:

```bash
cd ~/trafficxx &&
git pull --ff-only origin main &&
python3 scripts/plan-clickclash.py --blob
```

The guard permits only four new ClickClash storage resources: account,
container, custom role definition and container-scoped assignment. It rejects
updates, replacements or deletions of existing services. Apply the exact saved
plan path printed in `plan_file`:

```bash
terraform -chdir=infrastructure/foundation apply clickclash-PRINTED_ID.tfplan
```

Then apply the additive `002-blob-banners.sql` through the reviewed registration
file. The original migration is unchanged and checksummed; no player or score
is reset:

```bash
python3 scripts/clickclash-azure.py prepare &&
bash scripts/run-postgres-admin-sql.sh test-results/clickclash/azure-register.sql
```

Build and finish the rollout before migrating; do not run the migration while
old application replicas are still serving writes:

```bash
python3 scripts/clickclash-azure.py build &&
python3 scripts/clickclash-azure.py deploy &&
python3 scripts/clickclash-azure.py check &&
python3 scripts/clickclash-azure.py migrate-blobs &&
python3 scripts/clickclash-azure.py check-blobs
```

RBAC can take time to propagate. A failed copy leaves its database source
intact; rerun migration after addressing the reported failure. It processes up
to 100 images per invocation; repeat until `remaining` is zero. The check reads
the private container and verifies up to 200 current images by digest and size.
It refuses a larger verification batch rather than reporting a partial pass.
Finally test uploading/replacing/removing a banner and displaying it in the
public leaderboard through the browser.

## Failure handling and cleanup

Objects use server-generated owner UUID and content digest paths. Uploads are
immutable and read back before the metadata transaction commits. A failed
upload, failed read-back or SQL rollback leaves the previous visible banner
and byte budget unchanged. Retrying the same content verifies the existing
object rather than overwriting it. No cloud error body or credential is returned
in API responses.

Migration commits one image at a time, serializes with image changes using the
control row lock, and removes source bytes only after verified storage copy.
Deletion, replacement and moderation immediately remove public access to an old
banner. Old objects become private orphans. Cleanup waits 15 days, beyond the
currently configured 14-day PostgreSQL restore window. Cleanup takes the same
lock and rechecks references before deleting, so it cannot delete a concurrently
published banner. Deleted blobs and containers have 14-day soft deletion, and
blob versioning is enabled. These settings are not a tested disaster recovery plan.

Cleanup is manual, with a dry run by default:

```bash
python3 scripts/clickclash-azure.py cleanup-blobs
```

An operator can execute cleanup in the private application pod:

```bash
kubectl exec -n trafficxx deployment/clickclash -c app -- \
  python -m trafficxx.clickclash_blob_maintenance cleanup --execute
```

Each run scans one page of up to 100 objects. If `next_cursor` is present, pass
it with `--cursor` to the private maintenance command for the next page.
Cleanup is not scheduled automatically. The 100 MB budget limits current
published images, **not** orphan, version or soft-deleted storage. Keep cleanup
and storage usage under observation.

Do not roll back to the old, database-only image implementation after migration:
it cannot read blob metadata. Roll back to a compatible version or restore the
database image bytes from verified objects first. A PostgreSQL backup alone no
longer contains banner files; recovery must also preserve the matching objects.

## Security review and test boundaries

| Area | Reviewed behavior |
| --- | --- |
| Uploads | Exact Origin and session CSRF required; account required; bounded request body and two concurrent uploads per pod; PNG/JPEG/still WebP decoded and re-encoded; SVG/HTML and oversized/decompression-bomb images rejected; metadata stripped |
| Storage access | Workload identity only; fixed private container; generated UUID/digest paths; no arbitrary URL fetch; bounded download and SHA-256 verification; storage failures sanitized |
| Ownership | Upload/delete derives player from session, never a submitted owner; hidden/banned/deleted players cannot publicly serve images |
| Concurrency | Serialized image mutations and migration; previous banner preserved on failure; reference-checked delayed cleanup; login/signup/scoring recheck sessions after locks or password work |
| Frontend | React escapes public text; external links accept only HTTP/HTTPS without credentials, use noopener/noreferrer; no inline scripts; CSP and nosniff headers; passphrase not persisted in browser storage |
| Accounts | Hashed opaque sessions; Secure/HttpOnly/SameSite cookies; salted Argon2id; rate-limited account and image changes; no public operator API; restricted SQL runtime role |
| Scoring | UUID idempotency and ownership conflicts; transaction-backed scores; bounded shared admission; receipt history retained |

Changes also reject non-ASCII CSRF headers with 403 instead of producing a
server error. Existing tests cover cookie/session expiry, invalid Origin,
tampered click payloads, concurrent retries, cross-account click IDs, account
deletion, image moderation, unsafe website URLs and restricted SQL permissions.

Local integration tests use real disposable PostgreSQL and an in-memory storage
adapter for storage failure injection. Storage SDK boundary tests mock the Azure
transport. They do not prove live Azure IAM, network availability, object
recovery, DDoS resilience or performance under public write load. No review can
guarantee that every edge case or vulnerability is absent.

Browser tests also feed hostile markup and a JavaScript website URL through the
public profile response: markup stays literal text and the unsafe website does
not create an anchor.

Local verification completed: Terraform formatting/schema validation, strict
documentation build, 576 offline tests, the existing PostgreSQL account/scoring
tests plus 12 new storage/security integration tests, and three browser tests.
The Python 3.12 resolved dependency set (46 packages including test dependencies)
and the frontend dependency audit report no known advisories after the Pillow
upgrade. Azure blob transfers in these tests use a controlled storage adapter;
the live migration and IAM checks have not run yet.

Email is intentionally unverified and has no recovery flow. Public ads appear
immediately and need operator moderation. Automatic clients remain possible;
global limits are not a DDoS protection service. Notifications, automated
retention scheduling and paired database/blob disaster recovery remain separate
operational work.

References: [Azure Blob Python authentication](https://learn.microsoft.com/en-us/azure/storage/blobs/storage-blob-python-get-started),
[Azure data authorization](https://learn.microsoft.com/en-us/azure/storage/common/authorize-data-access).
