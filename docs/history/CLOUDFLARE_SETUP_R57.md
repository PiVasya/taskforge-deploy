# Cloudflare setup and first live validation (r57)

The Russian quick-start is in README.md. This file supplies the full technical
prerequisites and troubleshooting details; no secret values are included.

## 1. Roll out the same archive
Assuming A is still the current Primary: migrate B, then C, then A, sequentially.
From each newly unpacked r57 directory:

```bash
sudo bash ./cluster.sh migrate &&
bash ./cluster.sh status &&
bash ./cluster.sh doctor
```

Stop on failure. The stable r55/r56 path should say that actual mounts are already
version-independent; it does not require another r48/r55 archive or a re-home.
Do not test a Primary change while versions or Cloudflare credentials are partial.

## 2. DNS and HTTP prerequisites
Existing taskforge.by and ct.taskforge.by records must be proxied (orange cloud).
Use one A record for each, or a managed proxied CNAME chain ending at the managed
A. Existing origin IPs must belong to the installed topology's public candidates.
Unknown origins, duplicate A, unmanaged CNAME targets or AAAA block setup.
The controller updates existing records only; it does not create or delete them.

For every hostname, public port 80 and /.well-known/acme-challenge/* must reach
the gateway's HTTP webroot. Do not route this path through login, bot challenges,
custom redirects, stale cache rules or Workers serving a different body.
This release deliberately requires HTTP 200 without redirect for its bootstrap
proof. That is stricter than Let's Encrypt's redirect policy and avoids depending
on origin HTTPS before the first certificate exists. Check only the failing path;
do not globally disable SSL validation or use Flexible as a workaround.
Cloudflare's built-in Always Use HTTPS documentation has its own DCV exception;
its presence alone does not establish that the path is blocked. Custom rules
and the observed response determine the result.

The HTTPS /ha/live endpoint must remain reachable and non-cached. Current gateway
source exposes status=ok, service=gateway, node=A|B|C. A running image with a
missing or incompatible endpoint will fail visibly; this package does not rebuild
or silently replace gateway images.

Confirm the deployment has an existing certbot-webroot and letsencrypt volume,
working public 80/443 forwarding, valid LETSENCRYPT_EMAIL, and Docker image access.
The CA performs its own checks from multiple locations. The controller's local
probe is useful evidence, not proof that every Cloudflare POP has converged.

## 3. Create a USER API token
Cloudflare dashboard -> profile -> My Profile -> API Tokens -> Create Token.
Use the Edit zone DNS template, or a custom token with:

- Zone / DNS / Edit
- Zone / Zone / Read
- Zone Resources: Include / Specific zone / taskforge.by

Name example: TaskForge DNS failover. Review and create, then copy the token once.
Do not use a Global API Key or an Account API Token: setup verifies USER tokens
at /user/tokens/verify. Do not send the token to chat, put it in the archive,
or paste it into command-line arguments. One token may be used on all three nodes;
separate equally scoped user tokens also work. IP restrictions, when used, must
cover the real egress IPs of A/B/C, not their private WireGuard addresses.

Zone Read is used to resolve the zone name. It can be omitted when the operator
has already configured the correct CLOUDFLARE_ZONE_ID in the existing environment.
No Load Balancing permission or paid LB product is used.

## 4. Install credentials: standbys first, Primary last
On B, then C, then A:

```bash
sudo bash ./cluster.sh cloudflare setup &&
sudo bash ./cluster.sh cloudflare preflight
```

Paste the token into the hidden prompt. Setup writes these root-only 0600 files:
/etc/taskforge/cloudflare-api-token
/etc/taskforge/cloudflare-failover.json

The same archive works on all three; C remains lite. Standby Agents do not write
DNS. Setup validates/installs configuration; the Primary's worker automatically
adopts it. Allow a worker cancellation/retry interval after changing credentials.
preflight is an API/DNS layout check, NOT evidence that public HTTPS is ready.

## 5. Wait on A (the current Primary)

```bash
sudo bash ./cluster.sh cloudflare wait --timeout 600 &&
bash ./cluster.sh status
```

With configured Cloudflare and TLS, require:
agent_revision=57, role=primary, leader=A,
edge_phase=ready, dns_synced=True, public_route_ready=True,
edge_tls_ready=True, traffic_ready=True.

The command reports pending phases and errors. If 600 seconds expires, inspect
status. Do not delete cooldown/lock files to force repeated CA attempts.
A successful migration or doctor=PASS alone is not this public acceptance test.


### r57 wait semantics

A background 60-second revalidation may show `phase=route-check` while an older
still-fresh successful proof remains valid for serving. r57 does not flap public
readiness merely because a periodic recheck started. However, `cloudflare wait`
and `primary ... --timeout` do not treat that in-progress recheck as a completed
new handoff: they return only on the current `phase=ready` receipt and all declared
route confirmations.
`cloudflare wait` also requires the installed stable r57 control and fails immediately on an older responding Agent; do not use an unpacked newer directory as a substitute for `migrate`.

## 6. Controlled A -> B -> A test
After all three nodes have passed credential preflight and A has passed wait,
choose a maintenance window: this is not a zero-downtime design for a node with
no local certificate yet. From any r57 node:

```bash
sudo bash ./cluster.sh primary B --timeout 600
```

Watch DNS -> settle -> route checks -> certificate -> HTTPS. Inspect B status and
the public site. Return A using the same command with target A. Do not immediately
chain both switches with no observation. C uses the same sequence but remains lite.
If public readiness times out, Patroni might already have made B Primary. A timeout
is not a rollback. Inspect current status before another deliberate switchover.

## Troubleshooting signals
edge-settle: minimum settling pause.
route-check: fresh HTTP-01 probes; all hosts must pass three times in a row.
http=3xx: this release's bootstrap proof needs a direct HTTP response on the ACME path.
wrong-node-or-wrong-body: old origin, a rewrite, wrong webroot or incompatible endpoint.
cloudflare-response-not-proven: CF-Ray missing; inspect proxy/routing configuration.
cached-response-rejected: remove inappropriate caching for the ACME and /ha/live paths.
certbot-running: local issuance/renewal, bounded; no manual parallel Certbot needed.
retry-wait: last_error explains the block; failed CA attempts retry after 15 minutes.
certificate-reused: no unnecessary CA reissue of a still-valid certificate.
ROOT_CHECK_REQUIRED in non-root doctor: not a missing-certificate assertion.

## Official references checked 2026-09-07
- https://developers.cloudflare.com/fundamentals/api/get-started/create-token/
- https://developers.cloudflare.com/api/typescript/resources/zones/methods/list/
- https://developers.cloudflare.com/api/python/resources/dns/subresources/records/methods/edit/
- https://developers.cloudflare.com/dns/manage-dns-records/how-to/batch-record-changes/
- https://developers.cloudflare.com/ssl/edge-certificates/additional-options/always-use-https/
- https://letsencrypt.org/docs/challenge-types/
- https://eff-certbot.readthedocs.io/en/stable/using.html
- https://patroni.readthedocs.io/en/latest/rest_api.html
- https://docs.docker.com/reference/cli/docker/compose/run/
