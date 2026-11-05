# r57 validation record

## Executed automated checks
Run from the unpacked artifact: bash ./tests/test-r57-release.sh.
The full release script needs a disposable root-capable Linux test environment,
Python with PyYAML, runuser/nobody, curl, OpenSSL, bash and standard coreutils.
It tests temporary fixture files. Do not treat it as the production migration command.

- R57 manifest, shell/Python syntax, release identity, no historical release
  payload, no pycache artifacts.
- Preserved A/B full, C lite and eligibility/no-preferred-failback policy.
- Root-owned configuration normalization and an actual nobody-user read check.
  This is not an end-to-end invocation of installed status/doctor on real VPS data.
- Actual state import shell + Python against mocked Docker/systemctl, arbitrary
  source revision, key/env preservation, existing volume mapping, immutable
  baseline reuse, missing mount recovery and rejection of changed volume identity.
- Static mutation checks and current Compose/re-home service inventory comparison.
- Mocked manual Primary A -> C eligibility check (no real election).
- 46 tests in test_edge_lifecycle.py: ordered pipeline, timeouts/confirmation
  resets, Cloudflare error and rollback cases, demotion/cancellation, valid cert
  reuse, retry ledger, wrong/cached/redirected HTTP, C/lite, timeline/fences,
  secret/config file errors, process cancellation, responsive Agent HTTP while
  edge work waits, and expiring readiness proof.
- 16 tests in test_edge_integration.py: actual local-cert.sh and OpenSSL with
  mocked Docker volume lookup; custom baseline volume; wrong SAN/key rejection;
  missing/disagreeing volume rejection; real loopback Patroni-shaped HTTP server;
  no public success on API-only proof or wrong node; route-check revalidation cannot satisfy an explicit wait before `phase=ready`/3-of-3 confirmations; Certbot ownership and cleanup.
- The first suite also runs real curl against loopback HTTP and TLS servers with
  an ephemeral test certificate trusted only by the test transport. The CF headers
  and gateway responses there are emulated, not Cloudflare observations.

Total: 62 dedicated automated tests plus the base release regression script.
The final tarball is separately extracted, its manifest checked, and the same full
suite run from the fresh extraction. The adjacent report contains actual output.

## Not executed / acceptance still required
No Docker daemon or production container images were available here. Cloudflare
API behavior is mocked in tests; no real DNS record was modified. No ACME production
or staging CA request was made. No three-VPS failover, real certbot container,
real gateway restart, origin firewall, WAF/redirect rules or external-vantage route
was exercised. No claim of zero bugs or globally complete propagation is made.

Read-only gateway source compatibility inspection used repository
PiVasya/taskforge, commit 98857255ee4afb182a32fbe1a0eb46e5dcd0ba55. This does not prove
that the running gateway image has that exact code. Source inspection found HTTP
ACME and HTTPS /ha/live; the new pipeline tests each publicly before success.

The release preserves the existing state-based production migration gates. Those
will still execute real persistent-volume checks and operator status/doctor on
each server. Final public acceptance is cloudflare wait followed by a controlled
A -> B -> A test, after all three nodes have been migrated and configured.
