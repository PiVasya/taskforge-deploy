# SQL environment and resource defaults

These SQL engines contain disposable educational data only. Never put production business DB
credentials into `SQL_*` variables and never expose the engine DB ports publicly.

## Locally generated and preserved

`scripts/sql/prepare-runtime.py` writes a mode-0600 node-local environment file atomically. It
generates local engine credentials/guard marker, resolves immutable image pins and preserves
operator overrides. It never creates business databases, migrations or persistent volumes.

| Variable | Meaning |
| --- | --- |
| SQL_ENABLED | true on full nodes unless explicitly disabled; false on C/lite |
| SQL_WORKER_ID | node-local worker identity such as sql-A or sql-B |
| SQL_SANDBOX_MARKER | generated runtime-only namespace authorization marker |
| SQL_POSTGRES_PASSWORD / SQL_MYSQL_PASSWORD | disposable runtime admin credentials |
| SQL_POSTGRES_IMAGE | immutable PostgreSQL runtime pin |
| SQL_MYSQL_IMAGE | immutable MySQL runtime chosen for this node |
| SQL_POSTGRES_RUNTIME_DIGEST / SQL_MYSQL_RUNTIME_DIGEST | runtime identity registered in immutable SQL profiles |
| SQL_MYSQL_RUNTIME_FAMILY | audited TaskForge runtime family marker |
| SQL_MYSQL_RUNTIME_VARIANT | preferred, cpuv1 or explicit operator variant |

v41.5.2 MySQL selection:

- preferred modern host: MySQL 8.4.11 OL9 `sha256:3466ba4a4828aa8d46fb7c3bc16b67b781c98413cf4ea0fac6feaa6e881faa26`;
- older/masked CPU fallback: MySQL 8.4.0 OL8/cpuv1 `sha256:f7a8e140a7d6d1e6e0c99eeb0489c50a186ee4ac44ff55323a176529b9a43d33`;
- former r70 OL9 MySQL 8.4.0 `sha256:dab7049abafe3a0e12cbe5e49050cf149881c0cd9665c289e5808b9dad39c9e0`
  remains an audited compatibility identity but is not the preferred modern-node selection.

The matching application source is what authorizes compatibility among those exact identities.
Do not broaden compatibility to arbitrary MySQL 8.4.x images. An intentional runtime outside the
certified family is a new immutable profile and needs explicit validation/publication.

## Resource defaults

| Variable | Default |
| --- | --- |
| SQL_CONCURRENCY | 2 per worker (validated 1..8) |
| SQL_POOL_MAX_READY | 6 READY/replenishing slots per worker |
| SQL_POOL_MAX_MATERIALIZATIONS | 4 per engine |
| SQL_POSTGRES_MEM_LIMIT / SQL_MYSQL_MEM_LIMIT / SQL_WORKER_MEM_LIMIT | 768m / 1024m / 768m |
| SQL_POSTGRES_CPUS / SQL_MYSQL_CPUS / SQL_WORKER_CPUS | 0.75 / 0.75 / 0.75 |

Engine data and worker caches remain disposable. The SQL execution plane must never be attached
to business PostgreSQL/Patroni or used as a backup location. `/health` is process liveness and
`/ready` is profile readiness; no SQL DB port is published by default.
