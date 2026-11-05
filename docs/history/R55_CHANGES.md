# r55 changes

- Replaces version-chain migration with a self-contained state-based migration.
- Removes all embedded r47/r48 compatibility releases and historical migration engines.
- Source revision is informational only; no source revision allow-list controls migration.
- Discovers live configuration/secrets from stable paths or the actually active historical release.
- Pins current logical storage to the exact live Docker named volumes before re-home.
- Records an immutable volume fingerprint baseline and PostgreSQL `system_identifier`; retries verify the first baseline instead of redefining it.
- Re-homes replicas and Primaries using current r55 logic and actual Patroni leadership.
- Adopts already-stable mounts without recreating healthy containers unnecessarily.
- Archives stale legacy transaction evidence after current stable state is proven; it does not execute old transaction code.
- Preserves A/B as full and C as lite while allowing A/B/C to be Primary.
- Keeps manual Primary selection persistent; no automatic preferred-A failback.
- Preserves operator ownership across atomic config rewrites so non-root `status` and `doctor` remain usable.
- Keeps Cloudflare DNS failover current and symmetric for all Primary-eligible public nodes.
