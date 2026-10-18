# Private PostgreSQL learning step

AKS and the network exist, based on the user's Cloud Shell evidence. This step
adds managed PostgreSQL to the same Terraform foundation and remote state.
After an East US provisioning restriction, the user applied the revised Central
US configuration and supplied the server FQDN, database name, and regional subnet
outputs. The user subsequently verified private DNS/readiness and authenticated
SQL from an AKS apps-node client with Entra identity and verify-full TLS 1.3.
Application identities, SQL permissions, migrations, and application access
are the next steps.

## What the pieces mean

| Piece | Choice | Plain meaning |
|---|---|---|
| Server | PostgreSQL Flexible Server, version 17 | Azure runs the database engine; same major version as our local database |
| Compute | GP_Standard_D2ds_v5 | General Purpose starting size; availability is checked by Azure |
| Storage | 32 GiB initially, automatic growth enabled | Persistent space for database files; this is not connection pooling |
| Backups | 14 days, no geo-redundant backup | Retention for recovery; a restore experiment is still needed |
| Network | Central US VNet and delegated PostgreSQL subnet, peered with AKS | Private connectivity despite the East US provisioning restriction |
| DNS | Private zone linked to both VNets | AKS and the database network can resolve the private database name |
| Authentication | Entra only | Authorized identities use tokens; password sign-in is disabled |
| Human administrator | Same explicitly selected human as the AKS lab administrator | Bootstraps database administration; applications will get separate roles |
| TLS | require_secure_transport=on | Clients must use encrypted connections |

No standby/high-availability configuration or regional disaster recovery is
included yet. This is a single-server starting point, not evidence of the
README's availability or throughput targets. Azure chooses the server's zone.
Terraform ignores only the server's zone field because placement is delegated
to Azure. A later workload-identity plan exposed zone 1 -> null drift; this
lifecycle rule avoids an unrelated database update. Other server settings remain
managed. Intentional Terraform zone placement would require changing this rule.

The private DNS zone is `trafficxx.postgres.database.azure.com`. Azure manages
the server's private DNS record; the application should use the server's
reported FQDN, not a hardcoded IP. Both VNet links make this private zone visible
through Azure DNS. VNet integration uses the new Central US delegated subnet;
no private endpoint is created for this server. The original East US PostgreSQL
subnet remains reserved and unused rather than being deleted.

The AKS VNet stays in East US at `10.40.0.0/16`. The database VNet is in Central
US at `10.50.0.0/16`, with delegated subnet `10.50.0.0/24`. Two directional
peering resources connect them. These ranges also do not overlap the AKS pod
and service networks. DNS visibility and network reachability are separate
requirements; the configuration provides both. Peering does not grant database
login permissions. Cross-region traffic adds latency and data-transfer charges;
measure these before making performance claims. This is not disaster recovery:
there is still one database server and no replica in another region.

The PostgreSQL subnet receives a Microsoft.Storage service endpoint for WAL
archival. Azure adds it during provisioning; declaring it explicitly prevents
Terraform from treating the required setting as unwanted drift on a later run.
It allows routing to Azure Storage, not public access to the database.

Private connectivity is not database authorization. No application identity,
database role, schema migration, demo data, or API connection is created here.
The existing Python application's DATABASE_URL connection path still needs
Entra token acquisition/refresh support before it can use this server.

## Cloud Shell steps: user executes Azure commands

After the failed East US attempt, upload `trafficxx-postgres-centralus-update.zip`
and update the existing foundation:

```bash
cd ~/trafficxx/foundation
unzip -o ~/trafficxx-postgres-centralus-update.zip -d ~/trafficxx/foundation

export TF_VAR_aks_admin_object_id="$(az ad signed-in-user show --query id --output tsv)"
export TF_VAR_postgres_admin_principal_name="$(az ad signed-in-user show --query userPrincipalName --output tsv)"

terraform validate
az provider show --namespace Microsoft.DBforPostgreSQL \
  --query registrationState --output tsv
```

The object ID and principal name must refer to the same intended administrator.
Exports last for the current shell session. Supply these consistently on later
runs, or place them in a private ignored `.auto.tfvars` file. No database password
is requested, generated, or placed in the Terraform configuration by this step.

If the provider is not registered, the user can register it before planning:

```bash
az provider register --namespace Microsoft.DBforPostgreSQL --wait
```

Then create a fresh plan and review it:

```bash
terraform plan -out=postgres.tfplan
```

For the user-reported partial state, expected: **nine additions**, with no
existing resource changes or destruction:

- Central US VNet, delegated subnet, and its private DNS link: three resources.
- Peering in each direction: two resources.
- PostgreSQL server, human Entra administrator, empty `trafficxx` database,
  and TLS-required server configuration: four resources.

The existing private DNS zone and East US DNS link are already in state. The
original subnet's storage endpoint was part of the failed apply's earlier work;
the new regional subnet declares its own endpoint too. `postgres_subnet_id`
output now points to the new regional subnet. The shared `location` input stays
`eastus`; the separate `postgres_location` input defaults to `centralus`.

The DNS link precedes the server; the Entra administrator precedes database
creation. The server and application database have `prevent_destroy` lifecycle
rules to reject accidental destruction while these rules remain configured.
They do not block direct deletion in Azure or replace backups. Intentional
destruction requires a deliberate configuration change and reviewed plan.

After plan review, the user can apply `postgres.tfplan`. Do not reuse the old
AKS or foundation saved plans. If Azure reports regional restrictions, unavailable
compute, or a name conflict, adjust the relevant input and generate a fresh plan;
local validation does not check Azure availability or permissions.

## East US provisioning restriction discovered

On 2026-10-19, user-run apply failed at server creation with `ParameterOutOfRange`
and an empty allowed-version list. The subsequent East US capability response
explicitly said provisioning is restricted for the subscription; both normal
server-edition and version lists were empty, and OfferRestricted was Enabled.
Downgrading PostgreSQL is not an established fix for this failure.

The DNS zone and VNet link are already tracked in state. East US 2 is also
restricted. The user then checked Central US: it reported no restriction reason
and versions 11 through 18, including 17. The revised draft therefore uses a
separate Central US VNet/subnet and global peering, preserving the existing AKS
region and state. Capability output establishes version availability, not a
guarantee of a specific SKU's live capacity or successful provisioning.
See Microsoft's [capacity and region-access guidance](https://learn.microsoft.com/en-us/azure/postgresql/troubleshoot/how-to-resolve-capacity-errors).
Global peering is described in the [VNet peering overview](https://learn.microsoft.com/en-us/azure/virtual-network/virtual-network-peering-overview).

## What we verify next

The user ran a temporary postgres:17 client on the AKS apps pool. Its DNS lookup
resolved the server to `10.50.0.4` and pg_isready reported accepting connections.
Kubectl failed to attach and fell back to streaming logs, which captured the
successful result. The repeated DNS lines reflect the displayed output; they
are not evidence of two databases or a database failure. This confirms private
DNS and PostgreSQL readiness across the peering, not authenticated SQL or TLS
certificate verification.

For the next check, upload `scripts/check-postgres-auth.sh` to Cloud Shell and run:

```bash
bash ~/check-postgres-auth.sh
```

The script checks the selected subscription/context, creates a unique temporary
non-root client pod on the apps pool, disables service-account token automount,
and waits for readiness. It copies Cloud Shell's public PEM CA bundle into the
pod and verifies the bundle/client prerequisites before acquiring a token.
It then obtains an Entra database token using the user's existing Azure CLI
login, pipes it into one exec process, and uses psql with `sslmode=verify-full`
and that trusted CA bundle. Token contents are not
printed, written to a file, placed in pod settings, or stored in a Kubernetes
Secret. SQL reads the current database/user and the current session's SSL status
and TLS version; it changes no database data. An EXIT trap deletes the temporary
pod on ordinary success/failure. Do not run with tracing or print tokens.

The first user-run check failed before database authentication because the
postgres:17 image lacked the assumed `/etc/ssl/certs/ca-certificates.crt` file.
The receiving process exited early, producing a downstream BrokenPipeError from
the token producer. The script now uses the copied Cloud Shell bundle and checks
it before token acquisition; certificate verification remains enabled.

Bash syntax checks and four stubbed control-flow checks passed: success,
preflight failure, SQL failure, and a missing local bundle. Failure cleanup and
absence of a printed test token were verified. No real Azure login or TLS test
ran locally. The user subsequently reran the corrected script successfully:
database trafficxx, the configured human Entra identity, ssl=t, and TLSv1.3.
The script reported success and invoked temporary-pod cleanup. This verifies the
administrator path; application federation and permissions remain untested.

After user-run apply, inspect the server's state, private access, and authentication
settings through Azure CLI. Then test DNS, TLS, and database authentication from
inside the AKS network. Default browser Cloud Shell does not automatically have
private VNet connectivity; a database connection attempt from it is not a valid
test of AKS-to-database reachability. Management-plane Terraform operations from
Cloud Shell can provision these resources without opening the private SQL port.

Do not grant the API or workers database administrator permissions. The next
step will create separate workload identities and SQL permissions, then run
migrations before deploying the APIs. Restore, least-privilege enforcement,
pooling, and load behavior still need their own tests.

References: [private networking](https://learn.microsoft.com/en-us/azure/postgresql/network/concepts-networking-private),
[Entra authentication](https://learn.microsoft.com/en-us/azure/postgresql/security/security-entra-configure),
[provider resource](https://registry.terraform.io/providers/hashicorp/azurerm/5.10.0/docs/resources/postgresql_flexible_server).
