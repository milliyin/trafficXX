# Real database token renewal across expiry

Status: **live expiry-boundary renewal passed** on 24 October 2026. The Job
completed 1/1 and reported `DATABASE_TOKEN_RENEWAL_OK` for the original run.
This proves fresh SQL login after the initial token expired in the isolated
diagnostic process; it does not prove uninterrupted serving-pod availability.

## Verified live result

| Evidence | Result |
| --- | --- |
| Job | `browse-token-renewal-check`, Complete 1/1 |
| Run | `f1b7fde54dd24bdda3598c206b3a3e45` |
| Duration | 86,428 seconds, approximately 24 hours |
| Successful SQL checks | 289 |
| Original token expiry | 24 October 2026, 16:01:10 UTC / 9:01:10 PM Pakistan |
| Final fresh SQL check | 24 October 2026, 16:01:40.434155 UTC / 9:01:40 PM Pakistan |
| Replacement token expiry | 25 October 2026, 04:02:08 UTC |
| Renewal evidence | Different token, new SQL login after original expiry, same SDK credential instance |
| Database and identity | `trafficxx`, `trafficxx-lab-browse` |
| Encryption | TLS 1.3 |
| Inventory writes | None |

The final login occurred approximately 30 seconds after the original token's
expiry. No new check needs to be started to establish this result. Preserve
the safe final output; completed diagnostic logs have a two-day TTL.

## Earlier progress, before completion

The user created browse-token-renewal-check and supplied TOKEN_RENEWAL_WAITING
with run_id f1b7fde54dd24bdda3598c206b3a3e45. Initial access-token expiry is
2026-10-24T16:01:10+00:00; the earliest final check is
2026-10-24T16:01:40+00:00 (October24at21:01:40 Asia/Karachi). Initial estimated
wait was86428seconds, with probes every300seconds. This records initial SQL
success and scheduling only, not renewal success. The final result above later
confirmed completion with the same run ID and underlying SDK credential.

On October24 the user supplied the same run_id after17hours: Job Running0/1,
pod browse-token-renewal-check-z8zdd Running1/1 with zero restarts. Progress
records207..209 ended with successful SQL check209 at09:22:34.451656UTC.
The observed SDK token expiry had advanced to2026-10-25T04:02:08Z, later than
the initial expiry. This is evidence of a later-expiring token being available
within the ongoing process, consistent with proactive SDK refresh. The progress
output does not contain the final token-value comparison or boundary verdict.
The last reported23946remaining seconds still refer to the original final-check
boundary, not the newer token expiry. Keeping the Job running until the original
October24at16:01:40UTC /21:01:40Pakistan boundary was required before a pass.
The supplied final record now confirms the fresh-login boundary and renewal.

## What this proves

The earlier SQL checks proved short-lived successful logins. Reusing an existing
PostgreSQL session after token expiry would not prove a new login can acquire a
valid replacement token. This check keeps one process and one real SDK credential
object running, opens new physical SQL sessions and waits until the first
observed database access token has actually expired.

The initial SQL success provides a baseline. An observer around the original
WorkloadIdentityCredential records AccessToken.expires_on from real get_token
results. It delegates unchanged calls to that same SDK object; cache handling,
token acquisition and projected service-account-file access remain SDK work.
The wrapper changes only this diagnostic process's cached credential reference,
not serving pods. No credential cache is cleared, expiry policy changed, token
fabricated or clock altered. Workload identity acquires replacement access
tokens; no refresh-token credential file is created by this check.

Tokens and SHA256 comparison digests are retained only in memory and never
logged or written. Logs contain UTC expiry/check times, counts and booleans.
This uses SDK expiry metadata, not unverified decoded JWT claims. It concerns
the PostgreSQL workload access token, not the human Azure browser session or
the Kubernetes projected assertion's own expiry.

Every five minutes the probe closes only its own diagnostic session, waits up
to60seconds for the pool's replacement, then checks SQL identity/database/TLS
and a changed backend PID. It uses the real Browse pool and production database
parameter callback, with max1 only in the Job. No ticket writes, alternate auth
or application-pod restart is involved. SQL transactions end after each probe.

At30seconds after the initial token expiry, success additionally requires:

- A newly observed token retrieval and physical SQL login after that boundary.
- PostgreSQL server time past the boundary, and valid replacement-token expiry.
- A later expiry and token value different from the original observation.
- The same underlying SDK credential object and expected Browse identity/TLS.

SDK proactive renewal before the first expiry is allowed; the final fresh SQL
login still must occur after that first expiry. A long-lived open connection or
an elapsed timer alone cannot pass. The result describes this isolated process,
not uninterrupted serving-pod availability, all other worker roles or a load SLO.

## Duration and resources

Read the actual initial expiry instead of assuming one hour. Earlier workload
token evidence was nearly24hours; this check may need to run overnight.
The application waits at most25hours, the Job has a26hour hard deadline, no
automatic retries and restartPolicy Never. Failures cannot silently restart the
process and claim survival across the old boundary. It requests50mCPU/128Mi on
an existing apps node. Normal pool/SDK connection-attempt retries remain in
place; an unrecovered probe failure ends the diagnostic.

The Job continues inside AKS when Cloud Shell disconnects. Completed Job/pod
logs are retained for two days by TTL; save the final safe result before then.
No PVC or database result row is created for this read-only experiment.

References: [WorkloadIdentityCredential API](https://learn.microsoft.com/en-us/python/api/azure-identity/azure.identity.workloadidentitycredential?view=azure-python),
[Azure SDK token caching](https://github.com/Azure/azure-sdk-for-python/blob/main/sdk/identity/azure-identity/TOKEN_CACHING.md),
[PostgreSQL Entra authentication](https://learn.microsoft.com/en-us/azure/postgresql/security/security-entra-concepts).

## Start in user Cloud Shell

```bash
cd ~/trafficxx &&
git pull --ff-only origin main &&
bash scripts/start-token-renewal.sh
```

The helper checks subscription/context and refuses to overwrite an existing
browse-token-renewal-check Job before changing its source ConfigMap. It uses the
deployed immutable Browse image, existing Browse service account and private
SQL connection settings. There are no new SQL grants, Azure roles, human tokens
or application Secrets. It waits only for the initial SQL/expiry log, then
returns; it does not block Cloud Shell for the full test duration.

Share TOKEN_RENEWAL_WAITING with its estimated wait/earliest check time.
TOKEN_RENEWAL_STARTED means setup passed, not that renewal passed. Progress
records every five minutes share the same run_id. To inspect later:

```bash
kubectl get job browse-token-renewal-check -n trafficxx
kubectl get pods -n trafficxx -l job-name=browse-token-renewal-check
kubectl logs -n trafficxx job/browse-token-renewal-check -c check --tail=8
```

Only record renewal success after DATABASE_TOKEN_RENEWAL_OK and a completed Job
with no restarted/replacement process. Failure JSON prints only a safe check
identifier/error class; never paste tokens or credential files. A failed run is
not a renewal success even if application traffic elsewhere remains healthy.
To cancel, delete only job/browse-token-renewal-check in namespace trafficxx;
keep its safe logs first. This does not stop Browse, Grafana or the database.

## Local verification limits

Eight offline cases use fake time and metadata to verify the required expiry,
new-login and changed-token gates, server clock checks, bounded monotonic wait,
SDK-call delegation, redaction and cleanup preventing false success. They do
not simulate a real Azure renewal. Bash/YAML/Python checks and mocked
subscription/existing-Job/startup guards passed. Live boundary proof was later
supplied by the user's completed AKS Job. No local Azure or credential access
was needed to inspect that evidence.
