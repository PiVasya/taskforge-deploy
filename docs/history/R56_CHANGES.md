# r56: ordered public-edge and certificate lifecycle

## Scope
Built from the supplied r55 archive. No repository deployment, database schema change,
new leader-election policy, paid load balancer, migration chain, peer-certificate
copying, TLS downgrade, or monitoring architecture rewrite is included.

A/B are full; C remains lite. All three remain eligible as Primary.
The existing state-based importer, volume baseline, storage bindings and node
re-home algorithms are retained. Existing runtime filenames containing r55 are
persistent state format identifiers, NOT an instruction to install or execute r55.

## Why an API success is not sufficient
In r55 the Agent called certificate maintenance even after failed Cloudflare
reconciliation. Certificate maintenance did an HTTP-01 check when a certificate
was needed, but this did not provide a uniform, separately visible public-route
stage. A DNS API result alone cannot establish that the public edge has converged.

## New sequence
1. Independently prove the local Patroni /primary (including its leader-lock
   contract) and /cluster, bind work to its timeline, verify fences and critical apps.
2. Reconcile the existing proxied A records; revalidate CNAME dependencies.
3. Wait at least 15 seconds when Cloudflare is configured.
4. Publish a fresh unpredictable file only in this node's proven certbot-webroot.
   Query every managed hostname over public HTTP using normal name resolution.
   Require three consecutive successful rounds, five seconds apart. Reset the
   count after any failed round. Require CF-Ray for configured Cloudflare routes.
   Reject redirects, cached hits and wrong bodies; never force the public request
   to the target IP. This is a single-vantage observation, not global propagation proof.
5. Check/reuse the existing matching local certificate. When issuance or renewal
   is due, invoke the existing Certbot Compose profile without --force-renewal.
6. If certificate changed or HTTPS is not active, restart only the gateway (throttled).
7. Validate direct local HTTPS with normal CA/hostname checks, then public HTTPS
   /ha/live with the expected gateway node ID and uncached Cloudflare response.
8. Grant public readiness only after all checks and another Primary/core check.

The gateway currently serves the ACME location in its HTTP server block. Its
HTTPS /ha/live route is used for final origin identity; HTTPS ACME files are not assumed.

## Failure handling
Cloudflare/probe/CA errors are visible in status. No HTTP proof means no Certbot.
Losing leadership, changing timeline/config or an armed migration fence cancels
the attempt. DNS writes have guards before/after each operation. Best-effort
rollback is permitted only while the old writer can still prove leadership.
An in-flight provider request is not a distributed fencing transaction; subsequent
Primary reconciliation repairs external state. Multi-record DNS propagation is not atomic.

One labelled, node-local Certbot container is used, with cancellation cleanup.
No volume removal flags are used. Unlabelled conflicting containers are not removed.
Failed CA attempts are throttled for 15 minutes across Agent restarts. A still-valid
certificate may remain ready after a failed renewal, with an explicit warning.
There is no automatic database rollback after public-route or certificate failure.

## Responsiveness and observability
The bounded lifecycle runs in a separate, cancellable worker. It does not hold
up Agent HTTP /ha/live or /ha/telemetry. This is not a claim that all historical
notification false positives have been diagnosed or fixed.

Status adds phase, DNS sync, public route, TLS, confirmation count, retry delay,
last error and warning. cloudflare wait and primary have a 600-second default
observation window and progress output. A timeout explicitly warns that the
database may already have switched.

Non-root doctor no longer calls root-only certificate volume files 'missing': it
reports ROOT_CHECK_REQUIRED and points to the privileged Agent's local_cert_ready.

## Defaults / configuration
TASKFORGE_EDGE_SETTLE_SECONDS=15
TASKFORGE_ACME_ROUTE_WAIT_SECONDS=180
TASKFORGE_EDGE_PROBE_INTERVAL_SECONDS=5
TASKFORGE_EDGE_ROUTE_CONFIRMATIONS=3
TASKFORGE_CERTBOT_TIMEOUT_SECONDS=240

Defaults are internal; existing environment files are not rewritten to add them.
Per-step time budgets have bounded command/network overhead. Rechecks run after
60 seconds; failed ACME issuance has its own persisted 900-second cooldown.

## Not changed
Application images, Compose service definitions, full/lite assignment, HA databases,
replication, data ownership normalization, and volume recovery policy are unchanged.
New edge operations and preflight require existing correct public DNS/routing.
No zero-downtime claim is made: a cold node without a certificate needs public
HTTP-01 validation after the origin moves, and HTTPS can be unavailable during that time.
