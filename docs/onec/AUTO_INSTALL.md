# TaskForge 1C cluster auto-install

v41.5.2 keeps the normal GitHub-built `onec-runner:${IMAGE_TAG}` small. The
proprietary 1C platform is not baked into the image or server archive. Instead,
TaskForge distributes one verified official Linux installer through the existing
per-node MinIO instances over the WireGuard cluster network.

## Normal rollout

Seed the installer **once on the current Patroni Primary** before the rolling
migration. For an already downloaded official `.run`:

```bash
sudo bash ./cluster.sh onec seed \
  --file /path/setup-full.run \
  --version 8.3.27.1508
```

For a direct official/private URL:

```bash
sudo bash ./cluster.sh onec seed \
  --url 'https://...' \
  --version 8.3.27.1508 \
  --sha256 HEX
```

If the official endpoint needs cookies, client certificates or authentication,
put the corresponding curl options in a root-readable config file and add:

```bash
  --curl-config /etc/taskforge/onec/download.curl.conf
```

The seed command is intentionally restricted to the current Patroni Primary. It
downloads external traffic only once, verifies the full SHA-256, splits the
installer into verified chunks, writes them to that node's MinIO and publishes
`onec/desired.json` only after the cache is complete.

After that, use the ordinary rolling update: standby nodes first, actual Primary
last.

```bash
sudo bash ./cluster.sh migrate
```

No per-node `onec configure` is required. During migration every node:

1. discovers the cluster-wide desired 1C version from peer MinIOs;
2. finds healthy seed nodes that already have the matching complete cache;
3. downloads different chunks in parallel from up to four different MinIOs;
4. verifies every chunk and the reassembled full installer SHA-256;
5. mirrors the verified chunks and completion marker into its **own** MinIO;
6. installs/reuses the exact platform version in persistent `onec-platform`;
7. creates/reuses the file-infobase template in `onec-runtime`;
8. runs the unprivileged driver probe and real Designer -> EPF -> ENTERPRISE smoke test.

A node advertises itself as a seed only after the full installer checksum has
passed. Different requesting nodes deterministically choose different seed
subsets, so a large cluster does not always concentrate traffic on A/B/C.

## Cache layout

Each node keeps the same private bucket in its local MinIO:

```text
taskforge-runtime-cache/
  onec/
    desired.json
    <version>/<sha256>/
      manifest.json
      complete.json
      chunks/
        00000000.part
        00000001.part
        ...
```

The bucket is not made anonymous. Peer access uses the existing cluster MinIO
credentials and WireGuard addresses from `cluster.json`.

The host also retains a verified reconstructed installer under
`/var/lib/taskforge/onec-distribution` so repeated migrations do not have to
reassemble it from MinIO. Docker's `onec-installer-cache` volume remains the
bootstrap-local installer cache.

## Traffic controls

Defaults are deliberately conservative:

```env
ONEC_DISTRIBUTION_BUCKET=taskforge-runtime-cache
ONEC_DISTRIBUTION_CHUNK_SIZE_MB=32
ONEC_DISTRIBUTION_MAX_SEEDS=4
ONEC_DISTRIBUTION_PARALLEL=4
ONEC_DISTRIBUTION_STATE_DIR=/var/lib/taskforge/onec-distribution
```

For 20 nodes, the expected rollout is cumulative: A seeds first, B initially
pulls from A, C can pull from A+B, later nodes can pull from several already
completed seeds. Nodes migrated concurrently only use seeds that were complete
when their sync started.

## Legacy/manual source mode

`cluster.sh onec configure --file/--url` remains for repair and compatibility.
If the cluster has no desired cache, an enabled current Primary can promote that
configured source into the cluster cache during migration. New deployments
should prefer `onec seed`.

`sudo bash ./cluster.sh onec status` prints local platform state plus, when run
as root, cluster desired/cache/seed state. `sudo bash ./cluster.sh onec disable`
disables the local service without deleting platform/runtime/cache data.

Licenses, when required, remain only under `/etc/taskforge/onec/licenses` and
are mounted read-only. Learner jobs remain under `/var/lib/taskforge/onec-work`.
The long-lived `onec-runner` has no external network.
