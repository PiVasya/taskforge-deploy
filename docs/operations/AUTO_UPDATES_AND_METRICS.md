# Automatic application updates and real host metrics

## Release pair

Server: v41.5.5, revision 410505. Application source: develop 313 with the
healthcheck stack-overflow fix plus this update/telemetry integration.
The standalone server package is authoritative for production. The historical
`deploy/cluster` scripts in the application tree are not its replacement.
No EF model, migration or ModelSnapshot changes are included.

## One-time installation

Use the SAME server archive on every node. Read current roles using the installed
`cluster.sh status`; migrate standby nodes first and the current Primary last.
From the extracted v41.5.5 directory run:

```sh
sudo bash ./cluster.sh migrate
sudo bash /opt/taskforge/control/cluster.sh status
sudo bash /opt/taskforge/control/cluster.sh updates status
```

This is the server control-plane installer, not an EF migration-generation command.
It preserves existing secrets, topology, persistent volume identities and stable
paths. It pauses the application controller while replacing control files, retires
its old TaskForge-scoped Watchtower container (without removing data volumes),
then resumes the existing Node Agent.
It does not stop another project's Watchtower. If migration fails while paused,
fix the reported blocker and rerun the SAME migration; do not manually remove
fences or recreate database volumes. The migration lock rejects concurrent installs.

Publish the accompanying application source with the usual GitHub Full Rebuild.
The companion application update introduced `front`, `observability-api`,
and `support-bot`; this v41.5.5 server-only hotfix does not require new
frontend or C# images beyond that release. The full
archive retains all earlier fixes. The Node Agent discovers the published images
without per-container pull/up commands. Existing healthy unchanged containers are
adopted, not restarted just to populate updater state.

## Update ownership and lifecycle

The existing `taskforge-cluster.service` is the application lifecycle owner. No
extra daemon/service is installed. Watchtower is retained only as an explicitly
legacy Compose profile, disabled for normal application updates and not restarted
on each HA tick. All selected application labels disable its ownership.

An independent registry loop resolves the official Compose configuration, checks
all services assigned to that node/profile in batches of up to six,
with two bounded concurrent downloads. Batches continue independently every
five-second worker tick, so a couple of slow references do not hold a single
large registry scan hostage. Pull timeout is 420 seconds; transient failures
use separate per-service retry delays of 30 seconds to 30 minutes. Its default scan delay is 300 seconds after a completed scan, adjustable
through TASKFORGE_UPDATE_POLL_SECONDS. An unavailable reference does not abort the
other services. A new image ID bypasses that service's old failed-candidate backoff.
A root-only `cluster.sh updates check` command requests an early check; it changes
neither leadership nor DNS.

Downloads, lifecycle reconciliation and five-second host sampling are separate.
Failed startup, unhealthy containers and stopped standby applications do not
prevent discovering a later fixed image. Docker/registry command durations and
retained output are bounded. Readiness is checked without waiting on a Compose
batch or its dependency graph.

Apply uses the official TaskForge Compose wrapper, an invocation-local immutable
image ID override, `--no-deps --no-start --no-build --pull never`, then a targeted
start only when the role permits it. Secrets/path variables are not reconstructed
from container labels. Standbys stage images/containers without starting writers,
workers or bots. Explicit configured assist profiles retain their existing policy.
Authoritative Patroni leadership is checked before and after slow preparation.
Demotion/fencing prevents a late background start. A per-node file lock prevents
two application reconcilers from taking ownership simultaneously.

A candidate has a 180-second health grace. A newly running image does NOT
replace the saved last-good rollback tag on its very first healthy probe: it
must remain continuously healthy for 60 seconds (with a stable container start
timestamp and restart count). Any health flap or container restart resets the
proof. Old last-good images are not queued for cleanup until the candidate is
proven stable and its rollback image has been successfully preserved. Failed
candidates use increasing per-service retries (30 seconds up to 30 minutes).
When a previously observed healthy image exists, a failing candidate is rolled
back to it and the desired newer image remains visibly pending/failed. An initially broken installation has
no invented rollback image. A pulled standby image is shown as prepared, not
as runtime-verified. Old superseded application images are removed only by exact
ID, without force, without global pruning, while current/target/last-good images
remain protected. Docker refuses removal if another container still references it.

There is no claim of atomic multi-service releases, zero-downtime single-instance
restarts, or cross-node canary validation. Image publication, registry reachability
and valid application code remain necessary. Errors stay visible and are retried;
no false 'updated successfully' status is emitted for a broken/unpublished image.

## Protected persistent infrastructure

postgres/Patroni, etcd, Redis, RabbitMQ, MinIO and SQL PostgreSQL/MySQL engine
images are NOT blindly replaced from moving application tags. They follow the
existing controlled server/SQL/1C procedures. SQL-worker and ordinary code runners
are applications and ARE covered. Disabled/absent optional services (for example
1C without a configured licensed platform) are not silently enabled or installed.

A/B full and C lite remain supported. Authentication, core course/solution APIs,
execution and code-analyzer safety remain traffic-critical. Auxiliary AI, bots,
Minecraft, browser and optional image/1C/SQL services are reported as degraded
without holding the entire public site hostage to one optional service.

## Telegram

New updater errors form one persistent per-node incident. A problem persisting
60 seconds produces one short notification naming services, not raw Compose logs.
It is not sent again on every retry or Agent restart. Once all tracked errors
have stayed clear for 30 seconds, one recovery event is emitted. Stable event IDs
survive Agent restarts. New errors remain visible per service in Cluster even
while an existing incident is already open.

Observability and support-bot both suppress the three legacy repetitive
ha.container_* failure notifications during mixed-version rollout. Node-down,
leadership and other important alerts remain enabled. This is not a promise of
exactly-once Telegram delivery across network failures. Full debug logging remains
on; detailed container logs/diagnostics remain available locally.

## Graphs and metrics

Each Node Agent samples the HOST every five seconds from /proc/stat and
/proc/meminfo. CPU is busy time divided by the delta of all logical CPU time;
guest CPU is not counted twice, and steal/iowait are separate. RAM is
MemTotal - MemAvailable, not 'all cache is used memory'.

CPU package/core temperatures and fan RPM come from read-only hwmon/thermal
sysfs files when exported by hardware/driver. GPU power is not treated as CPU
load; missing VPS thermal sensors return null. No guessed or random values.

History is bounded to 720 samples (about one hour). A bounded file checkpoint
is written every 60 seconds and reused on Agent restart only for the same OS
boot. Closing the browser does not stop sampling. Observability fetches peer
telemetry every ten seconds by default; existing explicit slower overrides remain
operator-owned. The UI has separate CPU, RAM and CPU-temperature graphs for
every node, 15/60-minute ranges, freshness timestamps, memory byte totals and
available fan measurements. CPU steal has a separate dashed line. Missing values
and gaps are not interpolated as zero. This is host measurement, not a stress test.

## Diagnostics and limits

`cluster.sh status` shows updater phase, pending/failed counts, source metric
sample time and actual load. `cluster.sh updates status` shows each image target
and error. The Cluster UI shows per-service applying/verifying/prepared/healthy/
retry/rollback states and next retry. Registry failures are not hidden behind a
Docker 'healthy' badge.

No physical A/B/C deployment, registry publication, C# compilation or real Docker
integration has been executed in the editing environment. Use the accompanying
regression tests and GitHub build gates before installing to production.

## v41.5.5 production hotfix (upgrade from v41.5.0)

- Disabled 1C no longer pulls/recreates healthy front/tasks/solutions/execution
  services during a server control-plane migration. If 1C is enabled, its
  licensed provisioning branch is preserved.
- Legacy Watchtower is stopped with restart disabled and its stale Docker
  container removed WITHOUT `docker rm -v`. Subsequent Node Agent reconciles
  check for accidental legacy restarts no more than once a minute. Neither
  `verify.sh` nor `rehome-replica.sh` requires a running Watchtower.
- Image checks are fair batches of six across all assigned app services.
  Transient GHCR timeout errors are retained as per-service errors, retried
  automatically, and cleared only after a successful check. A stalled pull does
  not block the independent lifecycle/HA/telemetry workers.
- `cluster.sh status` reports update-specific degradation independently from
  `traffic_ready`. The public site can remain healthy while registry pulls are
  delayed, without hiding failed downloads. A high VPS CPU-steal percentage
  cannot be fixed by the server package itself.
- The revised source revision is 410505 and applies with the normal state-based
  `sudo bash ./cluster.sh migrate` procedure. Use standby nodes before the
  current Patroni Primary and inspect the live role immediately beforehand.

The package is not proof of real-world zero-downtime single-instance app updates
or permanent GHCR availability. Retried failures are visible, not ignored.


## v41.5.5 standby preparation verification

`docker compose up --no-start` finishing successfully is insufficient to
assert an immutable image was applied. Node Agent now verifies Docker's actual
container image ID, retries a stale Compose config once with `--force-recreate`
without starting the service, and backs off visibly on persistent mismatches.
`cluster.sh status` exposes `updater_prepare=verified:N mismatch:N` and the
Compose `force_recreate` count. `cluster.sh updates status` includes the last
preparation reason and retry attempts per service.

On a healthy Standby, `cluster.sh optimize rabbitmq-standby` negotiates a
10-minute maximum, root-only operator lease with the existing Node Agent. It
waits until the current update finishes and receives a maintenance ACK before
recreating only RabbitMQ with the existing volume. Failover promotion ignores
this lease immediately; a failed optimizer removes the lease automatically.
No storage engine gets recreated by the control-plane migration.
