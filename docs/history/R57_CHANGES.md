# R57 changes

r57 is a minimal readiness/wait hotfix on top of r56. It does not change the
state-based migration model, Docker volume pinning, Patroni switchover policy,
Cloudflare record ownership, Certbot ordering, A/B full profiles, or C lite.

## Fixed

- `cloudflare wait` and `cluster.sh primary ... --timeout` no longer accept an
  in-progress `route-check` as a completed handoff merely because a previous
  still-fresh edge proof keeps `traffic_ready=true` during periodic revalidation.
- Public handoff completion now requires the current edge state to be
  `phase=ready`, `status=synced`, `success=true`, correct `target_node`, a proven
  route, all declared route confirmations, and TLS readiness (or explicit
  HTTP-only mode).
- `cloudflare wait` returns the actual Agent `traffic_ready` value instead of a
  hard-coded `true` field.

## Why traffic_ready may stay true during periodic revalidation

Once a node has already completed a full DNS -> route -> certificate -> HTTPS
proof, r56/r57 intentionally keep that fresh proof valid for a bounded period
while the background worker performs its next 60-second recheck. Revoking public
readiness on every recheck would create needless readiness flapping.

The bug was not that the existing proof remained usable. The bug was that an
explicit operator wait/switchover command could treat the *new in-progress
recheck* (`route-check 1/3`) as the completion receipt. r57 separates those two
semantics: serving may continue on a fresh proof, but an explicit handoff wait
returns only on a current `phase=ready` receipt.

## Operator wait fail-fast

`cluster.sh cloudflare wait` now requires the installed stable control to be r57
and executes the installed r57 implementation. The readiness loop also rejects a
responding target Agent with a different revision immediately. This prevents the
real r56 failure mode where a command launched from an unpacked newer directory
could poll an older installed Agent for the entire timeout.
