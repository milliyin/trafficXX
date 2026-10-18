# Container CI

The workflow in `.github/workflows/ci.yml` runs on pushes, pull requests, and manual dispatch. It builds and checks code; it does not deploy to Azure or publish container images.

Pushes that change only `memeory.md` skip redundant runs. All pull requests still run the workflow, including work-log-only PRs, so required PR checks do not get stranded by this push filter.

## Checks and evidence

| Job | Checks | Evidence |
|---|---|---|
| Workflow validation | actionlint and ShellCheck | Job output |
| PostgreSQL tests | Reservation and messaging correctness tests in Docker | JUnit XML, Compose logs, cleanup log |
| Broker tests | Real AMQP delivery, duplicates, retries, and dead letters | JUnit XML and logs; requires CI license opt-in |
| Runtime security | Build the runtime target; inspect OS and installed Python package vulnerabilities | Full Trivy JSON, gate report, CycloneDX SBOM, image metadata |
| Broker coverage setting | State whether license-gated tests are enabled | GitHub job summary |

Actions are pinned to full commit SHAs. Tool images are pinned by digest in `ci/tools.env`. The token has read-only repository access, checkout does not persist credentials, and the workflow uses ordinary `pull_request` events rather than `pull_request_target`.

Each test job has its own `trafficxx-ci-*` Compose project, database, network, and temporary broker when needed. The runner script rejects project names outside that prefix. Its exit trap collects logs, removes only that project's containers and volumes, and preserves the test exit code. It does not stop or reset the normal `trafficxx` demo.

The CI overlay allows SQL Server a longer cold-start health-check budget (three-minute grace period and one hundred subsequent checks). Fresh runner containers must finish upgrading their system databases before the emulator can start. The check still executes a SQL query; readiness is not bypassed. The broker test itself then allows five minutes for emulator initialization.

CI mounts a reports directory into the test container and runs the test process as the Linux runner's UID/GID so JUnit output remains writable without changing the runtime image's non-root user.

## Security policy

The image scan covers operating-system packages and installed Python dependencies, including transitives. The complete JSON report includes all severities and findings without an available fix. The gate blocks **HIGH or CRITICAL findings with an available fix**. Unfixed findings still require review; passing this gate does not mean the image has no vulnerabilities.

There is no vulnerability ignore list. Scanner/database failures fail the job. The scanner reads a saved image archive rather than mounting the Docker engine socket. Runtime image archives are not uploaded as artifacts; JSON/text evidence is retained for seven days. Trivy telemetry is disabled.

The GitHub job summary lists every severity, findings with listed fixes, and the number of distinct HIGH advisories so a green gate does not conceal unresolved vulnerabilities. The initial local scan reported 44 HIGH package findings covering eight distinct advisories, with no fixes listed by Trivy. They remain unresolved; exploitability has not been assessed. The SBOM contained 119 components.

SBOM generation records the software in the tested image. Image signing, publishing by digest, provenance attestations, ACR access, deployment approvals, and GitOps updates are later milestones.

## Enable broker tests on GitHub

Local license acceptance is not automatically treated as CI acceptance. To enable this job:

1. Review the [emulator](https://github.com/Azure/azure-service-bus-emulator-installer/blob/main/EMULATOR_EULA.txt) and [SQL Server](https://go.microsoft.com/fwlink/?LinkId=746388) license terms for use on your CI runners.
2. In the repository, open **Settings -> Secrets and variables -> Actions -> Variables**.
3. After acceptance, create repository variable `SERVICEBUS_ACCEPT_EULA` with value `Y`.

Until then, the broker job is explicitly skipped and the workflow summary reports that missing coverage. PostgreSQL tests, workflow validation, and the security gate still run. No Azure credentials or GitHub secrets are required for these checks. The broker job uses synthetic data and disposable local credentials on an isolated runner.

An owner can make the database, validation, and security jobs required branch checks after the first successful run. Require the broker job as well when it has been enabled. Branch protection is not configured by these files.

## Run the Linux CI entry points

On a Linux machine with Bash, Docker, and Compose:

```bash
bash ci/test.sh database
# Set only after accepting both licenses for this environment:
SERVICEBUS_ACCEPT_EULA=Y bash ci/test.sh broker
bash ci/security.sh
```

The scripts create evidence under ignored `test-results/ci/`. A local scanner cache volume is removed on script exit. The generated runtime image and its archive remain local for inspection; the archive is excluded from Git and GitHub artifact uploads.

Windows development can continue using the Docker commands in [local development](local-development.md) and [messaging](messaging.md). GitHub's Linux jobs invoke the Bash wrappers to add isolated project names, JUnit evidence, and cleanup.

## Repository

Remote: https://github.com/milliyin/trafficXX.git. This workspace was initialized as a Git repository during CI setup because it previously contained no Git metadata and the remote was empty.

References: [GitHub workflow triggers](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow), [GitHub secure workflow use](https://docs.github.com/en/actions/reference/security/secure-use), [Trivy image scanning](https://trivy.dev/docs/latest/target/container_image/), [Trivy SBOM support](https://trivy.dev/docs/latest/supply-chain/sbom/).
