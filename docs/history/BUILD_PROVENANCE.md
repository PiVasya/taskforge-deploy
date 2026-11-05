# r57 build provenance

Target: TaskForge v40-r57.
Input: taskforge-prod-linux-v40-cluster-control-final-r56.tar.gz
Input SHA256: a93c2d265b077d150b47b6ef4be75b6afaca95de14de68829e5e0880abb2402d

r57 is built from a copy of the supplied r56 artifact, not from a changing branch.
No historical release executable payload, credentials, live environment file,
certificate/private key, Docker volume data or caches are shipped.

The only behavioral hotfix is the explicit handoff/wait completion gate in
cluster/primary.py plus the wait result field in cluster/cloudflare_dns.py.
Release identity, tests and documentation were updated to r57. Existing
state-based migration and storage-baseline-r55.json naming are retained for
interrupted-state continuity; those names are state format identifiers, not old
release dependencies.

R57_MANIFEST.sha256 covers every packaged file other than the manifest itself.
