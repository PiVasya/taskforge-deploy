# TaskForge v41.5.2 SQL deployment gates

v41.5.2 is the standalone A/B/C cluster-control package paired with the matching TaskForge application
source update. It does not replace application images by itself and it does not require a new EF
migration.

## Why v41.5.2 exists

Production diagnostics showed that existing published MySQL assignments are pinned to a MySQL
8.4.11 immutable target while r70 workers were advertising only current/compatible MySQL 8.4.0
targets. Solutions API rejected the published target at the capability precheck, before creating a
job. This release restores a runtime capable of serving that published contract and the paired
application source contains the explicit audited compatibility relation.

## Source/build gates

From the matching source tree run the normal repository/SQL checks before pushing images. The
important application components for this update are Tasks API, sql-worker, observability API and
front; normal production image rollout may rebuild the full image set.

The immutable compatibility relation must include only these audited identities:

- MySQL 8.4.11 OL9: 3466ba4a...
- MySQL 8.4.0 OL9 (r70): dab7049a...
- MySQL 8.4.0 OL8/cpuv1: f7a8e140...

Unknown versions/digests remain new profiles. Never rewrite previously published fingerprints.

## Cluster rollout

Use the SAME v41.5.2 archive on A, B and C. A/B remain full; C remains lite. Update standbys first
and the actual current Primary last when practical; do not assume A is Primary.

From each extracted v41.5.2 root:

```bash
sudo bash ./cluster.sh migrate
sudo bash ./cluster.sh status
sudo bash ./cluster.sh doctor
```

`migrate` discovers live state, preserves production storage bindings/secrets and prepares optional
SQL only on full nodes. Do not delete/recreate PostgreSQL, MinIO, RabbitMQ, Redis or etcd volumes.
Do not run broad `compose up` on a standby just to force application containers; application role
is managed by the existing control plane.

After the matching application images have rolled out, verify actual images and SQL worker state:

```bash
sudo bash ./cluster.sh compose images
sudo bash ./cluster.sh compose ps
sudo bash ./cluster.sh compose logs --tail 150 sql-worker
```

## Acceptance

Use published user paths, not only container health:

1. SQL 14 JOIN must produce the same result on SQLite, PostgreSQL and MySQL.
2. SQL 13 DELETE and SQL 12 INSERT must execute on MySQL.
3. Both RUN and CHECK must work.
4. A completed cluster diagnostics collection must appear automatically in the UI's saved archive
   list and remain downloadable from MinIO after the original browser session is gone.
5. Default stored-diagnostics retention is 7 days; expired archives are removed automatically.

A green `mysqladmin ping` alone is not sufficient acceptance. The published immutable target must
be advertised by a healthy worker and reach actual execution.

## Failure policy

Do not work around failure by broadening compatibility to all MySQL 8.4.x, rewriting DB rows,
republishing every assignment, clearing business storage, changing Primary, changing Cloudflare or
regenerating EF migrations. Diagnose the exact immutable target/runtime relation instead.
