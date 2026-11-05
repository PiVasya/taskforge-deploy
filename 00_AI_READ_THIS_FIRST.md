# TaskForge server v41.5.9

This is the state-based A/B/C server package paired with the matching TaskForge
application release. Use the same archive on all nodes, migrate standbys first and
the actual Patroni Primary last. Persistent PostgreSQL, MinIO, RabbitMQ, Redis and
etcd volumes are preserved.

## v41.5.5 application control and telemetry

Read `docs/operations/AUTO_UPDATES_AND_METRICS.md`. Application updates belong to
the existing Node Agent, not to a periodically stopped/started Watchtower.
Preserve the separate registry/lifecycle/host-sampling workers, per-service retry
and rollback state, incident notification deduplication, role checks and fences.
Never auto-recreate persistent data engines as floating application images.
Missing CPU/RAM/thermal measurements must stay null. History is bounded.

## v41.5.5 updater ownership and large-telemetry hotfix

This release fixes false legacy-updater detection after Watchtower was
removed. Node Agent checks the actual project-labelled Docker containers and
reports Docker-query failures separately. It also avoids passing unbounded
HA telemetry JSON (which can exceed the Linux 128 KiB environment-variable
limit) via environment variables during migration and verification. Healthy
optional SQL engines are not recreated merely to upgrade the control agent.
All v41.5.1 registry retries, graceful last-good rollback and 1C protections
remain in place. Other Docker projects and persistent volumes are untouched.

Migrate standby nodes first, then check Patroni and migrate the actual Primary:

    sudo bash ./cluster.sh migrate
    sudo bash /opt/taskforge/control/cluster.sh status
    sudo bash /opt/taskforge/control/cluster.sh updates status

Only the server archive is changed; frontend/C# image rebuild is not required
for this v41.5.5 hotfix if v41.5.0's companion images were already published.
See docs/operations/V41_5_2_HOTFIX_QA.txt for tested scope and limitations.

## Preserved cluster-distributed 1C runtime

The normal GitHub-built `onec-runner` remains free of the proprietary platform.
Seed an official Linux installer once on the current Patroni Primary with
`cluster.sh onec seed`. TaskForge verifies it, chunks it into the Primary's local
MinIO and publishes cluster desired state only after the cache is complete.

Every other node automatically discovers that desired state during `migrate`,
downloads verified chunks from up to four already-complete peer MinIO seeds over
WireGuard, checks chunk and full-file SHA-256, mirrors the installer cache into
its own MinIO, then performs the existing persistent-volume 1C bootstrap and real
Designer -> EPF -> ENTERPRISE smoke test. There is no per-node installer setup.

Different requesting nodes choose different seed subsets so larger clusters
distribute upload traffic instead of always concentrating it on A. The actual
`onec-runner` still has no external network and mounts platform/runtime/licenses
read-only. See `docs/onec/AUTO_INSTALL.md`.

All previous v41.x unified-runner, HA/Cloudflare, SQL, diagnostics and stable-path
behavior remains in place.

## v41.5.5 lower standby CPU overhead

A long-lived full standby keeps data quorum services live, but leaves optional
applications stopped. RabbitMQ's `rabbitmq-diagnostics ping` required frequent
Erlang interpreter startup (often >100% of one CPU on A). Change its Docker
healthcheck from interval 5s / retries 60 to interval 30s / retries 10,
preserving the approximate 300-second unhealthy threshold and retaining an
actual RabbitMQ readiness probe (not a TCP-only false-positive).

The Node Agent now wakes its application reconciliation worker early only on
actual desired-service or routing changes; normal bounded periodic checking is
preserved for standby fencing and failover. Compose image reference generation
is cached for 90 seconds (with file-mtime/size invalidation) to avoid repeated
`docker compose config` on each registry batch, while GHCR pulls still discover
new mutable-tag digests. `updater_compose` counters in `cluster.sh status`
identify image-prepare versus registry-config Compose invocations.

Migration installs control and changes the *desired Compose configuration*,
but intentionally does NOT recreate data-bearing RabbitMQ. A separate
operator-approved `cluster.sh optimize rabbitmq-standby` command is provided to
apply the healthcheck to a confirmed hot standby safely with preflight,
acknowledged updater maintenance and volume-identity checks. Do NOT run it on the active Primary. On A, check role
immediately before applying and inspect status during readiness recovery.

## v41.5.5 verified standby application staging + live RabbitMQ tuning

A simulated successful Compose no-op reproduced a standby CPU loop in the
previous updater: it wrote `current_image=target` without Docker verifying the
container image ID. The new updater independently inspects the prepared
container, retries once with `--force-recreate --no-start --no-deps` if needed,
and records failures using bounded per-service backoff rather than spinning on
5-second HA ticks. No app is started on Standby.

For the *operator approved* RabbitMQ-only healthcheck application use:

    sudo bash /opt/taskforge/control/cluster.sh optimize rabbitmq-standby

This requires a live healthy warm Standby, obtains an updater maintenance ACK,
verifies the exact persistent RabbitMQ volume identity and only then recreates
RabbitMQ. The lease expires and is overridden by Primary promotion. Do NOT run
it on the current Primary. No infrastructure is recreated by normal migration.
See docs/operations/V41_5_5_DOCKER_INSPECT_QA.txt.

## v41.5.5 Docker stopped-container inspect fix

The v41.5.4 updater on A reported `docker-inspect-unavailable` on ten staged
services. Formatted Docker inspect can fail for missing objects or malformed
formatting, and English error-text matching is not a safe source of truth.
The new agent falls back to `docker ps -a` and raw JSON inspection of confirmed
project containers, exposes its counters in `cluster.sh status`, and clears
only the obsolete diagnostic errors on start so they can be rechecked.

The RabbitMQ optimizer no longer mistakes unrelated application image errors
for RabbitMQ safety blockers when A is a reconciled idle warm Standby with
`hot_ready=True`. It still requires updater pause acknowledgement and data
volume identity checks. On A run migration first, then the explicit optimizer.
See `docs/operations/V41_5_5_DOCKER_INSPECT_QA.txt` for limits.

## v41.5.6: eliminate needless Docker fallback inspection on Standby

Real A telemetry on v41.5.5 showed 553/553 formatted Docker inspections
falling back because stopped, prepared containers often lack `State.Health`.
`{{if .State.Health}}` fails under Docker Go-template missing-key behavior.
Use `{{with index .State "Health"}}...{{else}}...{{end}}` in all affected
Docker-inspection paths (Node Agent, updater, Edge, SQL, resource checks,
RabbitMQ optimizer, MinIO and 1C lifecycle) so missing health status is
normal rather than a second `docker ps -a` plus raw inspect each time.

Retain the v41.5.5 strict fallback for *real* missing containers and errors;
never misreport a missing container as healthy, and never remove data volumes.
RabbitMQ on the already-optimized standby remains at interval 30 seconds;
normal migration does not recreate it. The same server archive may be used on
other nodes after validation, keeping the current Primary last.
See docs/operations/V41_5_6_DOCKER_HEALTH_TEMPLATE_QA.txt.

## v41.5.7 Primary application lifecycle hotfix (2026-10-21)

While Patroni local `/patroni` still confirms Primary, one missing `/cluster`
leader response no longer makes the Node Agent publish an empty application
set or revoke the public edge. This prevents an intermittent REST lookup from
stopping an otherwise healthy primary site. Positive leader conflicts,
confirmed standby roles and sustained local Patroni outages remain fenced;
image starting still requires a fresh positive Patroni authority check.
HA/update telemetry includes the decision and its evidence.

This release advances the package and Agent revision from v41.5.6 / 410506
to v41.5.7 / 410507, so migrations, status checks and telemetry identify the
hotfix unambiguously. State-based migration retains existing data volumes.
Read `docs/operations/V41_5_7_PRIMARY_APP_HOLD_HOTFIX_QA.txt` before rollout.

## v41.5.8 stopped/unhealthy retry starvation hotfix (2026-10-21)

The v41.5.7 updater marked a stopped Docker container with a cached
`unhealthy` health state as `healthcheck-failed:exited/unhealthy` before
reaching its desired-running `docker start` repair branch. Each elapsed retry
deadline could be extended indefinitely. v41.5.8 now gives stopped/created
containers their proper bounded start attempt, honors persisted retry delays,
and applies backoff if a restarted container exits again before its grace
window expires. The specific stale v41.5.7 error gets one recovery attempt
after upgrade without repeatedly bypassing cooldowns on Agent restarts.
No change to Patroni, storage, Docker configuration or database schema.
Full server package version v41.5.8 / Agent revision 410508.
See `docs/operations/V41_5_8_STOPPED_HEALTH_RETRY_HOTFIX_QA.txt`.

## v41.5.9 / 410509: confirmed failure and guarded container lifecycle

Read `docs/operations/V41_5_9_HA_STABILITY.txt` and `V41_5_9_QA.txt` before
rollout. Failure evidence is observer-local and read-only; never turn probes
into an independent election controller. Keep separate START permission and
positive STOP evidence. Keep role fences, bounded restart confirmation and
v41.5.8 exited-container recovery. No data-engine recreation is permitted here.

A live cluster timing PATCH changes only ttl/loop_wait/retry_timeout, after all
nodes run 410509+, agree on one Primary and pass health checks. It must not be
replaced by editing bootstrap YAML alone. Preserve customized policy unless the
operator explicitly applies the new profile. Genuine failures may take longer
to fail over. Never disable Patroni's protective demotion to hide a partition.

HA diagnostic archives default to the caller's current working directory and
must belong to the invoking sudo user. Do not erase PostgreSQL/etcd forensic logs.
