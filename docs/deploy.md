# Deploying Mind Grapes

The same `docker-compose.yml` runs locally for dev and on a production host —
typically a small always-on machine (a Mac mini, a NUC, a home server) fronted
by Tailscale: reachable over your tailnet, plus a public `*.ts.net` Funnel URL
for clients that can't join it (e.g. claude.ai web). Differences are all in
`.env`. This guide covers both paths.

## Local development

```sh
cp .env.example .env
# Fill in POSTGRES_PASSWORD and OPENROUTER_API_KEY.
# Generate a SECRET_KEY (the web service refuses to boot on an empty or
# placeholder key):
python -c "import secrets; print(secrets.token_urlsafe(64))"
# Generate the OAuth signing key and paste it as OAUTH_JWT_PRIVATE_KEY; set the
# rest of the OAUTH_* block (ISSUER / AUDIENCE / JWKS_URL) per .env.example:
docker compose run --rm --entrypoint python web manage.py gen_jwt_key
docker compose up --build
# Seed the first passwordless admin + a passkey-enrollment link:
docker compose exec web python manage.py bootstrap_admin you@example.com
```

Caddy serves on `https://localhost` with a self-signed cert (`tls
internal`). Trust the cert once in your browser; curl can use `-k`.

`/mcp` is served by the MCP service (`run_mcp`, epic #117) and is OAuth-only
(#95): every request must carry a valid Django-issued bearer token, and
`OAUTH_JWKS_URL` must be set or it refuses to boot. There is no header-key
escape hatch — the Brain UI lives in the Django app (#101) behind passkey auth.

A new OAuth client (Claude Desktop, Codex, Claude Code, mcp-inspector)
registers itself the first time you add the connector — no operator step.
Dynamic Client Registration is served by the Django authorization server
at `/oauth/register` and is **open by design** (Claude relies on it), but
household-constrained: a registrant can only ever create a public client
doing the `authorization_code` + `refresh_token` flow, with at most five
redirect URIs (https anywhere, or http on loopback). There is no enable
toggle. The security boundary is the passkey-gated authorization step, not
registration: an anonymous registrant can create a constrained client row
but cannot obtain tokens or read brain data without logging in.

### Tailscale sidecar (optional, locally)

```sh
docker compose --profile tailscale up
```

Brings up the tailscale container alongside everything else. Useful
when you want to test the Funnel path before deploying.

## Isolated dev stack (Django product layer)

The stack is a Django + HTMX **`web`** service (epic #61) and an **`mcp`**
service built from the same image (epic #117). Both sit behind Caddy, which
path-splits human traffic from MCP:

```
                  ┌──────────── Caddy edge ────────────┐
  Browser   ────▶ │  / , /accounts/* , /admin/*  ──▶ Django (web) │
  AI client ────▶ │  /mcp* , /.well-known/*      ──▶ Python (mcp) │
                  └─────────────────────────────────────┘
                         one Postgres, two schema owners:
                  Django → public.*      MCP → brain.* (never written by Django)
```

Development uses a **dedicated `docker-compose.dev.yml` that shares the schema
(`./init`) but never the data** of the live stack. Bring it up:

```sh
cp .env.dev.example .env.dev          # one-time; dev-only placeholders, gitignored
make dev-up                           # docker compose -f docker-compose.dev.yml up -d --build
```

Then:

- `http://localhost:8080/` → Django landing page
- `http://localhost:8080/healthz` → `ok` (Django liveness)
- `http://localhost:8080/mcp` → the Python MCP server (401 auth gate when unauthenticated)
- `http://localhost:8025/` → mailpit (catches enrollment emails in later slices)
- `127.0.0.1:5433` → the dev Postgres (`openbrain_dev`)
- `http://localhost:9100` → minio S3 API (attachment blobs, #42)
- `http://localhost:9101` → minio console (`devminio` / `devminiopassword`)

The dev stack runs the **real** S3 backend, not the in-memory fake, so the image
path is exercised end to end locally. `S3_ENDPOINT` is `http://minio:9000` (what
the containers use) while `S3_PUBLIC_ENDPOINT` is `http://localhost:9100` (what
your browser fetches presigned URLs from) — they differ because SigV4 signs the
Host header. Both are set inline in `docker-compose.dev.yml`.

The `web` image runs `uvicorn --reload`; the source is bind-mounted, so editing
a view or template reflects without a rebuild. Migrations apply automatically on
container start (`web/entrypoint.dev.sh`); apply manually with `make dev-migrate`.

Make targets: `dev-up`, `dev-down` (keeps volumes), `dev-nuke` (`down -v`),
`dev-logs`, `dev-migrate`, `dev-test`.

### Live ⇄ dev isolation guarantee

The dev stack shares **zero** volumes, ports, container names, or env files
with live. Every isolation-critical value in `docker-compose.dev.yml` is a
hardcoded literal (not a `${VAR}` substitution), so the file is immune to
whatever the live `.env` contains.

| Axis | Live (`docker-compose.yml`) | Dev (`docker-compose.dev.yml`) |
|---|---|---|
| Compose project | `openbrain` | `openbrain-dev` |
| Postgres volume | `pgdata` | `pgdata_dev` |
| Blob volume | `miniodata` | `miniodata_dev` |
| DB name | `openbrain` | `openbrain_dev` |
| Host ports | `5432`, `80`, `443`, `9000` | `5433`, `8080`, `8025`, `9100`, `9101` |
| Container names | `openbrain-*` | `openbrain-dev-*` |
| Env file | `.env` | `.env.dev` |
| `./backups` mount | yes (rw) | none |
| Edge TLS | Caddy `tls internal` / ACME | Caddy plain HTTP (localhost) |

Because the two stacks use different compose project names and volume names,
**`docker compose -f docker-compose.dev.yml down -v` can only ever remove the
dev volumes.** Verified drill: with the live stack running, a dev `down -v`
removed `openbrain-dev_pgdata_dev` while `openbrain_pgdata` kept its original
`CreatedAt` and the live `openbrain-postgres` container stayed healthy. Re-up
on a fresh `pgdata_dev` re-runs `./init/*.sql` into an empty dev brain.

To verify isolation yourself:

```sh
docker volume ls | grep pgdata                          # note both volumes
docker compose -f docker-compose.dev.yml down -v        # tear down dev
docker volume ls | grep pgdata                          # only openbrain_pgdata remains
```

## Choosing an exposure path

The brain has to be reachable by whatever AI client you use. Nothing in the
design ties that reachability to Tailscale — the app is hostname-agnostic
(`ALLOWED_HOSTS`, `OAUTH_ISSUER`, and `BRAIN_MCP_URL` are all env-driven) and
Caddy can terminate TLS itself. Tailscale Funnel is the *default recipe*, not
a hard dependency. Pick by where the client runs:

| Path | Reachable by | TLS | Public DNS / firewall hole |
| --- | --- | --- | --- |
| **Tailnet-direct** | clients on your tailnet only | tailscale sidecar | none |
| **Tailscale Funnel** | anyone, incl. claude.ai web | tailscale (`*.ts.net`) | none |
| **Public host** | anyone, incl. claude.ai web | Caddy ACME (Let's Encrypt) | yes — you run it |

- **Tailnet-direct** — the client machine is on your tailnet (e.g. Claude
  Desktop on your own laptop). It reaches the brain by tailnet hostname; the
  tailscale sidecar terminates TLS and forwards plain HTTP to Caddy `:80`. No
  Funnel, no public exposure. **claude.ai web can never use this** — it runs in
  Anthropic's cloud, off your tailnet.
- **Tailscale Funnel** — the `*.ts.net` hostname is publicly reachable with a
  Tailscale-issued cert, no public VM or DNS record of your own. This is what
  off-tailnet clients (claude.ai web) and the OAuth callback use. The default,
  documented in full below.
- **Public host** — a real domain pointed at a box you expose on 80/443, with
  Caddy fetching a Let's Encrypt cert. More surface area and it's your DNS +
  firewall to run, but it's a first-class supported path (see *Public host*
  below).

Whichever you pick, `OAUTH_ISSUER` and `BRAIN_MCP_URL` **must be the exact
hostname the client connects to** — the OAuth redirect bounces through the
browser back to that URL, so mixing a tailnet hostname with a Funnel or public
hostname breaks the auth handshake.

> Other tunnels work too. A Cloudflare Tunnel (or any reverse proxy that
> terminates TLS and forwards to Caddy `:80` with `X-Forwarded-Proto: https`)
> is functionally the *Public host* path with someone else's edge — set the
> hostname env vars to the tunnel's public hostname and skip Caddy's own ACME.
> Not documented here; the config seam is the same, and it's left as an
> exercise for the reader.

## Production (an always-on host behind Tailscale)

Production runs on a small always-on machine on your home network — a Mac
mini, a NUC, any box that can run Docker. It is **not** a public VM: no public
DNS record, no inbound firewall hole, no Let's Encrypt. Reachability comes
entirely from Tailscale:

- **Tailnet** — the small group reaches the brain by its tailnet hostname. TLS
  is terminated by the tailscale sidecar, which forwards plain HTTP to the
  Caddy `:80` edge.
- **Funnel** — `tailscale/serve.json` enables Funnel, so the machine's
  `*.ts.net` hostname is publicly reachable with a Tailscale-issued cert. This
  is what off-tailnet clients (claude.ai web) and the OAuth callback use. No
  public VM required.

Host prerequisites (your job, not the app's):

- Docker running (Docker Desktop, colima, or plain dockerd on Linux)
- Tailscale installed and logged in, with Funnel allowed for this machine in
  the tailnet ACLs
- NTP on — TOTP and TLS depend on a correct clock
- The machine set to never sleep, so it keeps serving

Deploy:

```sh
# On the host, fresh checkout:
git clone https://github.com/MindgrapesApp/mindgrapes-server
cd mindgrapes-server

# Securely transfer the prod .env from your workstation. NEVER email/Slack:
#   scp ~/mindgrapes-prod.env <host>.<tailnet>.ts.net:mindgrapes-server/.env
# Consider sops/age for encryption-at-rest of the file.

# .env must have (the Funnel hostname is <machine>.<tailnet>.ts.net):
#   SECRET_KEY from `python -c "import secrets; print(secrets.token_urlsafe(64))"`
#     (the web service refuses to boot on an empty or django-insecure-* key)
#   BRAIN_HOSTNAME=localhost   # Caddy serves :80 behind the tailscale TLS terminator
#   SECURE_HSTS_SECONDS=15552000
#   OAUTH_JWKS_URL set (required since #95; /mcp is oauth-only and the
#     server refuses to boot without it)
#   OAUTH_ISSUER=https://<machine>.<tailnet>.ts.net
#   OAUTH_JWT_PRIVATE_KEY from `manage.py gen_jwt_key` (the AS signing key)
#   BRAIN_MCP_URL=https://<machine>.<tailnet>.ts.net/mcp
#   TS_AUTHKEY / TS_HOSTNAME for the tailscale sidecar
#   ... see .env.example for the full OAUTH_* block

docker compose up -d --build
docker compose --profile tailscale up -d   # bring up the Tailscale/Funnel edge

# Pre-flight: Django's own deployment checklist (flags weak SECRET_KEY,
# missing HSTS, insecure cookies, and similar misconfigurations):
docker compose exec web python manage.py check --deploy
```

Tailscale terminates TLS for both the tailnet hostname and the public
`*.ts.net` Funnel URL; there is no Let's Encrypt step. Check it worked from a
tailnet machine:

```sh
curl -I https://<machine>.<tailnet>.ts.net/health
# Expect: HTTP/2 200 (the MCP /health route).
# NOTE: the MCP service does not yet set app-layer security headers on
# /mcp + /health (tracked in #129). Verify those headers on a Django path
# instead — Django middleware sets them, and HSTS reaches browsers
# domain-wide from there:
curl -I https://<machine>.<tailnet>.ts.net/healthz
# Expect: HTTP/2 200 + strict-transport-security / x-frame-options / nosniff
```

Verify the MCP service booted with auth on and the brain schema in sync:

```sh
docker compose logs mcp | grep -E 'migrate|serving'
# Expect: [migrate] schema up to date
#         open-brain MCP (python) serving on 0.0.0.0:8000/mcp/ (auth=on)
```

Verify the Django authorization server is serving DCR (open by design).
Send an invalid body so the check doesn't create a real client row — the
endpoint validates and returns 400, which proves it's reachable and is the
Django AS (a 404 would mean the request fell through to the wrong service):

```sh
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  https://<machine>.<tailnet>.ts.net/oauth/register \
  -H 'content-type: application/json' \
  -d '{}'
# Expect: 400 (invalid_redirect_uri — redirect_uris is required)
```

Verify `?key=` is gone:

```sh
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  "https://<machine>.<tailnet>.ts.net/mcp?key=anything"
# Expect: 401
```

### Tailscale: the production edge

On the production host, Tailscale is the whole edge — both the tailnet path for the small
group and the public Funnel path for off-tailnet clients. It also gives
operators tailnet access to SSH, Postgres, and the Django admin by hostname.

```sh
docker compose --profile tailscale up -d
```

`tailscale/serve.json` roots `/` at the Caddy edge (`http://caddy:80`), which
path-splits to `web` and the Python `mcp` — so both the tailnet and Funnel
paths get the same routing, log scrubbing, and body cap. (Caddy's bare `:80`
block is that edge; the host-matched `{$BRAIN_HOSTNAME}` block is the
unused-in-prod local-TLS edge, left in place for `localhost` dev.) OAuth
enforcement lives in the mcp service; security headers come from Django
middleware on the human paths (the /mcp header gap is tracked in #129). Funnel
makes the `*.ts.net` URL publicly reachable — required for clients that aren't
on the tailnet (e.g. claude.ai web).

### Public host (your own DNS + Let's Encrypt)

If you'd rather expose the brain on a real domain than a `*.ts.net` Funnel URL,
the app supports it with no code changes — Caddy already knows how to fetch a
Let's Encrypt cert. The `{$BRAIN_HOSTNAME}` block in `caddy/Caddyfile` self-signs
for `localhost` dev, but a real public hostname there triggers ACME automatically.

The tradeoff versus Funnel: you run the DNS record and the inbound 80/443
firewall hole yourself, and the box is directly on the public internet rather
than behind Tailscale's edge. In exchange you get a hostname you control.

Differences from the Tailscale recipe above:

- **Don't** run the tailscale profile — this path doesn't use the sidecar. Bring
  up only `docker compose up -d --build`, and make sure Caddy's `443` (and `80`
  for the ACME challenge) are published to the host and reachable from the
  internet.
- Point a public **DNS A/AAAA record** at the host, and open **80 + 443** inbound.
  Port 80 must be reachable for the HTTP-01 ACME challenge.
- Set `ATTACHMENTS_BIND_ADDR=127.0.0.1`. This box is directly on the internet and
  there is no tailnet to scope minio's S3 API to, so bind it to loopback.
  Compose has no default for this and will refuse to start without it. Do not
  rely on the firewall here: Docker publishes ports by writing into the `DOCKER`
  iptables chain, which is evaluated **before** a `ufw` INPUT policy, so a
  published `0.0.0.0:9000` stays reachable even after you "open only 80 + 443".
  If you do want off-host attachment reads on this recipe, bind an interface you
  have actually firewalled and verify from outside with
  `nmap -Pn -p 9000 <host>` — expect `filtered`/`closed`.
- Set the hostname env vars to your real domain (Caddy binds its TLS edge to
  `BRAIN_HOSTNAME`, so it must be the public name, not `localhost`):

  ```sh
  #   BRAIN_HOSTNAME=brain.example.com          # Caddy's ACME edge binds here
  #   ALLOWED_HOSTS=brain.example.com           # Django host validation
  #   OAUTH_ISSUER=https://brain.example.com
  #   BRAIN_MCP_URL=https://brain.example.com/mcp
  #   SECURE_HSTS_SECONDS=15552000
  #   ... plus the same SECRET_KEY / OAUTH_* / POSTGRES_* block as the Tailscale recipe
  ```

  (No `TS_AUTHKEY` / `TS_HOSTNAME` — those are Tailscale-only.)

- Caddy fetches and renews the cert on its own; there's no Let's Encrypt step to
  run by hand. Verify:

  ```sh
  curl -I https://brain.example.com/healthz
  # Expect: HTTP/2 200 + strict-transport-security / x-frame-options / nosniff
  ```

Everything downstream — the OAuth authorization server, open DCR, the /mcp
path-split, the client-registration flow below — is hostname-agnostic and works
identically on a public host.

Two things matter *more* on the open internet than behind a Funnel, and both are
already tracked as open items rather than surprises:

- **DCR (`/oauth/register`) is open by design.** Behind Funnel that's low-risk;
  on a public domain it's a spam / anonymous-row-creation target. Registrations
  are inert without a passkey login, but the durable fix (gate + rate-limit +
  table hygiene) is #79.
- **`/mcp` + `/health` don't set app-layer security headers yet** (#129). Django
  paths do, so HSTS still reaches browsers domain-wide; the gap is on the MCP
  paths specifically.

### Registering a client in production

DCR is open and self-service — there is no gate to open or close. Adding
the connector (e.g. claude.ai web) is all that's needed: the client hits
`/.well-known/oauth-authorization-server`, then `POST /oauth/register`
automatically, and gets a constrained public-client row. Tokens still
require the household member to log in and approve at the passkey-gated
authorization step, so an unwanted registration is inert until someone
consents.

Verify the client landed. Clients live in the Django `OAuthClient` model
(table `oauth_oauthclient`), with RFC 7591 metadata in a `client_metadata`
JSONB column:

```sh
ssh <machine>           # the production host, by its tailnet hostname
cd mindgrapes-server
docker compose exec postgres psql -U $POSTGRES_USER -d $POSTGRES_DB \
  -c "select client_id,
             client_metadata->>'client_name'        as name,
             client_metadata->'redirect_uris'        as redirect_uris,
             to_timestamp(client_id_issued_at)       as registered
      from oauth_oauthclient
      order by client_id_issued_at desc limit 5;"
```

To pre-seed a known-static client without going through a connector flow,
create the row via the Django shell rather than raw SQL — the model's
`set_client_metadata` keeps the JSON shape correct:

```sh
docker compose exec web python manage.py shell -c "
from openbrain.oauth.models import OAuthClient
import secrets, time
c = OAuthClient(client_id=secrets.token_urlsafe(24),
                client_id_issued_at=int(time.time()))
c.set_client_metadata({
    'client_name': 'Claude Desktop',
    'redirect_uris': ['https://claude.ai/api/mcp/auth_callback'],
    'grant_types': ['authorization_code', 'refresh_token'],
    'response_types': ['code'],
    'token_endpoint_auth_method': 'none',
    'scope': 'brain:read brain:write',
})
c.save()
print(c.client_id)
"
```

If the open registration endpoint on the public Funnel becomes a spam /
anonymous-row-creation concern, the durable fix (gate + rate-limit + table
hygiene) is tracked in #79 — it is intentionally not closeable via an env
flag today.

## Attachments: images in the brain (#42)

Image blobs do **not** live in Postgres. A `minio` service holds them in its own
volume; Postgres holds only `brain.blobs` / `brain.attachments` rows pointing at
object keys. Two consequences to internalise before you deploy this: `pg_dump` is
no longer a whole-brain backup, and there is a second address the deployment has
to get right.

Two doors write images, one engine behind them
(`web/openbrain/brain/services/image_captures.py`):

- `POST /capture/image` — the app. Bearer-authed multipart, takes a
  full-resolution photo, rejects oversize on `Content-Length` before any decode.
- the `capture_image` MCP tool — an LLM client pasting a screenshot, base64
  capped at 256KB.

Both re-encode to a bounded WebP, content-address it by the sha256 of the
original bytes (so the same photo captured twice stores one object), promote EXIF
time/GPS, and link event/place entities.

### The one thing that breaks deployments: `S3_PUBLIC_ENDPOINT`

The server reaches minio over the compose network at `http://minio:9000`. A
client fetching a presigned URL reaches it at the tailnet host. **SigV4 signs the
Host header**, so a URL minted against the internal address fails with
`403 SignatureDoesNotMatch` the moment the app fetches it — the object is there,
the credentials are right, and it still 403s. That is why the endpoint is split:

- `S3_ENDPOINT` — in-network, set inline by compose. Used for put/get/head.
- `S3_PUBLIC_ENDPOINT` — the address the **client** uses. Presigned URLs only.

Set it to the tailnet host, including the port:

```sh
S3_PUBLIC_ENDPOINT=http://<machine>.<tailnet>.ts.net:9000
```

The same rule kills the tidier-looking topology, so don't reach for it: a Caddy
`handle_path /attachments/*` that strips the prefix invalidates every signature,
because SigV4 signs the request **path** as well as the host. Minio is therefore
served at the **root** of its own listener (the `:9000` block in
`caddy/Caddyfile`), not under a path prefix. If you want a prefix anyway, the
signature has to be computed over the same prefixed path the proxy forwards —
strip nothing.

How far that listener reaches is decided by `ATTACHMENTS_BIND_ADDR`, which
compose requires you to set (there is no default — an unset value fails the
`docker compose up` with a message). `tailscale/serve.json` publishes `:443`
(and Funnel) to `caddy:80` and does not publish `:9000`, which keeps attachments
off Funnel — but serve.json does not bind the host's own interfaces, so it is
not what keeps `:9000` off the public internet. Set the bind address to the
tailnet IP (`tailscale ip -4`) for tailnet clients, or `127.0.0.1` when nothing
off-host reads attachments. It also refuses anything but GET/HEAD — uploads go through the app,
which owns auth, the size ceiling, and the decode check. Authorization for a read
is the presigned URL itself: a short-TTL bearer minted only after the viewer
passed `can_viewer_read`. It cannot be revoked once minted (no clawback, per
#48); the TTL is the bound.

### Deploying it

```sh
# 1. .env keys (see .env.example for the annotated block):
#      S3_ACCESS_KEY / S3_SECRET_KEY   generate: openssl rand -hex 16
#                                      minio boots with these as its root user,
#                                      so changing them later is a minio-side
#                                      user change, not just an .env edit
#      S3_BUCKET=brain-attachments
#      S3_REGION=us-east-1
#      S3_PUBLIC_ENDPOINT=http://<machine>.<tailnet>.ts.net:9000
#      ATTACHMENTS_BIND_ADDR           REQUIRED, no default. The tailnet IP
#                                      (`tailscale ip -4`), or 127.0.0.1 when
#                                      nothing off-host reads attachments
#      MAX_IMAGE_UPLOAD_BYTES          optional, default 12582912 (12MB)
#    BLOBSTORE_BACKEND and S3_ENDPOINT are set inline by compose — leave them out.

# 2. Rebuild the image. #42 added Pillow, pillow-heif, and boto3; a stale image
#    boots fine and then fails at the first capture.
docker compose build web
docker compose up -d

# 3. Apply the brain schema delta (brain.blobs / brain.attachments live in
#    init/18 + init/19; an existing volume does not get them from init/).
docker compose exec mcp python manage.py brain_ledger migrate
# Expect: [migrate] applied 18-... , 19-...   (or "already up to date")

# 4. Confirm the bucket exists (minio-init creates it, then exits 0).
docker compose logs minio-init
# Expect: bucket brain-attachments ready
```

If you raise `MAX_IMAGE_UPLOAD_BYTES`, raise the matching cap in
`caddy/Caddyfile` too — the `/capture/image` handler caps bodies at 13MB so the
413 comes from Django (which can say what the limit is) rather than from Caddy.

### Success condition

The whole feature is working when this round-trips on the tailnet:

1. The app POSTs a photo to
   `https://<machine>.<tailnet>.ts.net/capture/image` with a bearer token and a
   multipart `image` part. Expect `200` and a JSON body with `experience_id`,
   `attachment_id`, `object_key`, `byte_len`.
2. The object is really in minio. The container's built-in `local` alias has no
   credentials (it only backs the healthcheck's `mc ready local`), so set an
   authenticated alias first — `mc ls local/...` alone returns *Access Denied*:

   ```sh
   docker compose exec minio sh -c \
     'mc alias set me http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
      mc ls --recursive me/brain-attachments | tail'
   # Expect: <account_id>/<sha256>.webp, byte size matching the POST response.
   ```

3. `get_experience` on that id returns an `attachment` block, and its
   `presigned_url` **renders in a browser on the tailnet**. A 403 here means
   `S3_PUBLIC_ENDPOINT` doesn't match the host you fetched from; a 404 means the
   put failed but the row was written (run the orphan reconciliation).

Curl version of step 1, from a tailnet machine:

```sh
curl -s -X POST https://<machine>.<tailnet>.ts.net/capture/image \
  -H "Authorization: Bearer $TOKEN" \
  -F "image=@/path/to/photo.jpg" \
  -F "description=the whiteboard after the design review" \
  -F "visibility=private"
# Expect: {"experience_id": "...", "attachment_id": "...", "object_key": "...", ...}
```

### Backing up the blobs

**`pg_dump` does not back up attachments.** It captures the rows that reference
objects, so a restore from Postgres alone gives you a brain whose images are all
broken links. The `miniodata` volume is a separate backup target:

Snapshot the volume, then let restic pick the tarball up alongside the `pg_dump`
(see the backup section below). The volume is named `<project>_miniodata`, and
the compose project name comes from the checkout directory — so look it up
rather than hardcoding it:

```sh
VOL=$(docker volume ls --format '{{.Name}}' | grep miniodata$)
docker run --rm -v "$VOL":/data:ro -v "$PWD/backups:/out" \
  alpine tar czf /out/miniodata-$(date +%F).tar.gz -C /data .
```

Restore is the same move in reverse, with the stack down:

```sh
docker compose stop minio
docker run --rm -v "$VOL":/data -v "$PWD/backups:/in" \
  alpine sh -c 'rm -rf /data/* && tar xzf /in/miniodata-<date>.tar.gz -C /data'
docker compose start minio
```

Restoring Postgres to an older snapshot than the blob store is safe (extra
objects are orphans, which the reconciliation in
`image_captures.orphan_blob_keys` reports). The reverse — blobs older than the
database — leaves dangling attachment rows. Snapshot the blobs **after** the
`pg_dump`, not before.

## Backups & restore (#90)

> **Scope:** this covers Postgres only. Attachment blobs live in the `miniodata`
> volume and need their own snapshot — see "Backing up the blobs" above.

`pg_dump` produces a consistent logical snapshot; **restic** wraps it
(encryption + dedup + retention) and pushes it offsite to **Backblaze B2**.
The local `./backups/*.dump` files are the fast on-box restore copy; the restic
repo on B2 is the disaster copy. `restic` is a host prerequisite on the production host
(`brew install restic`).

### One-time setup

1. Create a B2 bucket and an application key scoped to it.
2. Fill the backup block in `.env` (see `.env.example`): `RESTIC_REPOSITORY`,
   `RESTIC_PASSWORD`, `B2_ACCOUNT_ID`, `B2_ACCOUNT_KEY`.
3. Initialise the repo once: `set -a; source .env; set +a; restic init`.

> **`RESTIC_PASSWORD` is the crown jewel.** It encrypts the repo; lose it and
> every backup is permanently unrecoverable. Store it OFFLINE — a password
> manager entry plus a printed copy kept separate from the box. It is *not* in
> the repo and must survive total loss of the box.

### Automated schedule

> **Host mismatch:** the units under `systemd/` are Linux/systemd and do **not**
> run on macOS. A launchd port (a `launchd.plist` calling `backup.sh` daily) is
> an open follow-up. On a macOS host, schedule the backup with a launchd agent
> or `cron`, e.g. a daily `cron` line running `./backup.sh` from the checkout.
> The systemd recipe below works on any Linux host.

```sh
sudo cp systemd/openbrain-backup.* /etc/systemd/system/
# Adjust WorkingDirectory/ExecStart paths in the .service to the checkout.
sudo systemctl daemon-reload
sudo systemctl enable --now openbrain-backup.timer
systemctl list-timers openbrain-backup.timer   # confirm next run
```

The timer fires daily at 03:00 (`Persistent=true`, so a missed run catches up
after downtime). Logs: `journalctl -u openbrain-backup.service`. A failed run
leaves the unit in `failed` state; wire an `OnFailure=` alert unit to be paged.
Run one immediately: `sudo systemctl start openbrain-backup.service`.

Manual one-off (e.g. before a risky migration): `./backup.sh`. With
`RESTIC_REPOSITORY` unset it still writes a local dump and skips the offsite push.

### Restore drill (do this regularly — a backup you haven't restored isn't one)

The drill restores the latest backup into a throwaway scratch database and
checks row-count parity against the **counts recorded at dump time** (the
`<dump>.counts.tsv` sidecar `backup.sh` writes). It compares to those, not to
the live DB — live drifts as the brain is written to, which would otherwise
make the drill false-FAIL. It never touches the live data.

> The automated drill tool was retired with the old server implementation and
> its re-port is tracked in #182. Until then, drill manually:

```sh
DUMP=$(ls -1t backups/*.dump | head -1)
# Restore into a scratch DB inside the postgres container:
docker compose exec postgres createdb -U $POSTGRES_USER brain_restore_test
docker compose exec postgres pg_restore -U $POSTGRES_USER -d brain_restore_test \
  "/backups/$(basename "$DUMP")"
# Compare row counts against the sidecar recorded at dump time:
cat "${DUMP%.dump}.counts.tsv"
for t in brain.experiences brain.entities brain.claims auth.clients; do
  docker compose exec postgres psql -U $POSTGRES_USER -d brain_restore_test \
    -tAc "select '$t', count(*) from $t"
done
# Clean up:
docker compose exec postgres dropdb -U $POSTGRES_USER brain_restore_test
```

Counts must match the sidecar exactly; a mismatch means the backup is not
trustworthy — investigate before relying on it. `pg_restore` prints a benign
error about `CREATE EXTENSION pg_cron` (the cron schema is intentionally
excluded from the dump) — parity, not the pg_restore exit code, is the verdict.
To drill the **offsite** copy instead, `restic restore latest --target
/tmp/restore-drill` first (restic recreates the dump's original absolute path
under the target, so `find` the `.dump` there), stage it under `./backups/`,
and run the same steps.

### Real restore (disaster recovery)

To restore into the live database: stop the brain writers (`docker compose stop
mcp consolidation`), `restic restore latest --target /tmp/ob-restore` (then
`find /tmp/ob-restore -name '*.dump'` — restic recreates the original absolute
path under the target), then `pg_restore` that dump into a freshly-created
database (or `--clean` an existing one). Re-run `init/05` (or `docker compose up`
on a fresh volume) to re-establish the pg_cron schedules, then bring `mcp` and
`consolidation` back up. Take a fresh backup immediately afterwards.

## Schema migrations & boot gate (#91; ported to Python in #115)

The brain's Postgres schema (`brain.*`) is raw-`pg` by design (Django owns the
auth/app tables via its own `django_migrations`). `brain.schema_migrations` is the
brain's equivalent ledger: it records which `init/NN-*.sql` schema files this volume
actually got. On startup the `mcp` service (`run_mcp`) calls `assert_schema_up_to_date`
and **refuses to boot** (`[migrate] FATAL …`, exit 1) if the ledger disagrees with the
manifest (the `SPINE` in `web/openbrain/mcp/boot.py`) — a drifted memory store must
not serve.

It is **name/id-keyed, not checksummed**: migrations are append-only (you never edit
an applied `init/` file — you add a new one). "Drift" means the ledger is behind,
ahead, or has a gap relative to the manifest.

The operator CLI is the `brain_ledger` management command. The `mcp` service's
entrypoint is already `python manage.py`, so a one-off `run` passes just the
subcommand, while `exec` into a running container needs the full command:

```bash
# Running container — exec bypasses the entrypoint:
docker compose exec mcp python manage.py brain_ledger status   # applied vs pending; exit 1 on drift

# One-off (e.g. the gate is failing so the service won't stay up) — `run` uses
# the service entrypoint `python manage.py`, so pass only the subcommand:
docker compose run --rm mcp brain_ledger baseline   # stamp an at-HEAD volume (no SQL run)
docker compose run --rm mcp brain_ledger migrate    # apply pending migrations (each in one txn)
```

`migrate` reads `init/NN-*.sql`, so it needs the `init/` files — the `mcp`
service mounts `./init:/init:ro` for exactly this. `status` and `baseline` never
read the files and work regardless.

### Fresh volume

`docker compose up` runs every `init/*.sql`, and `init/14-schema-migrations.sql`
self-seeds the ledger to HEAD. Nothing else to do — the boot gate passes.

### Existing volume (one-time, e.g. a pre-#91 prod volume)

The ledger table is new, so a pre-#91 volume has no ledger and the gate would refuse
to boot. Stamp it once, **only when the volume is known to already be at HEAD**
(baseline trusts you — it records the full manifest without running any SQL):

```bash
docker compose run --rm mcp brain_ledger baseline
docker compose run --rm mcp brain_ledger status  # → schema up to date
# then restart mcp; boot log shows: [migrate] schema up to date
```

### Adding a new schema migration

Append-only, in one change: (1) add `init/NN-<name>.sql` (idempotent DDL); (2) add the
entry to `SPINE` in `web/openbrain/mcp/boot.py`; (3) add the matching row to the
`init/14` self-seed list (a unit test enforces SPINE ⇔ init files). Deploy:
`brain_ledger migrate` applies it on existing volumes; fresh volumes get it from `init/`.

A migration may carry **data** as well as DDL — `init/23-flatten-merge-chains.sql` is
pure DML, repairing rows a code change depends on. Same rules, two additions: it must be
idempotent against data it already fixed (resolve to the target state, don't assume a
starting one), and it should verify its own postcondition and `raise exception` if the
data can't be repaired. `migrate` runs one transaction per entry, so a raise rolls the
attempt back and leaves the ledger row unwritten. Where a data repair is the precondition
for a function change, ship them as adjacent entries: the boot gate refuses to serve
while any manifest entry is pending, so no traffic reaches the new function until both
are stamped.

### Known limitations / follow-ups

- **No content-checksum drift detection** — editing an already-applied `init/` file is
  not caught (it shouldn't happen under the append-only rule). Revisit if needed.
- **Django prod entrypoint** should run `manage.py migrate --check` to give the web
  service the same fail-on-drift behavior for the `public.*` schema
  (`web/entrypoint.dev.sh` runs `migrate` in dev). Tracked as a follow-up.

## Operational notes
- **Rate limits**: per-process in-memory today; they reset on container
  restart and under-count across workers. Moving to a shared cache is
  tracked in #178.
- **Secrets rotation**: rotating `OAUTH_JWT_PRIVATE_KEY` invalidates
  every issued access + refresh token; users must re-authorize. There
  is no JWKS rotation infrastructure (single key).
- **Updates**: pull main, `docker compose up -d --build`. Existing
  sessions survive an mcp container restart (cookies still valid; JWTs
  outlive the process); Caddy's cert cache lives in the `caddy-data`
  volume so a Caddy restart doesn't re-request a cert.

## Object storage for attachments (images, #42)

`capture_image` stores a bounded WebP derivative of each image in S3-compatible
object storage; the DB holds only `brain.attachments` (experience→blob links) and
`brain.blobs` (content-addressed object records). Config (`.env`, gitignored,
never committed):

- `BLOBSTORE_BACKEND=s3` (default `memory`, which the unit suite uses).
- `S3_ENDPOINT` (e.g. a minio or Hetzner Object Storage URL), `S3_BUCKET`,
  `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_REGION`. These are pinned to server
  config only — never derived from a request or from EXIF. The client uses
  path-style addressing so a minio endpoint works without per-bucket DNS.

### Presigned reads (bearer tokens, no clawback)

`get_experience` mints a short-TTL (~60s) presigned GET URL only after the viewer
read check passes. The signed URL is a **bearer token**: it is regenerated on
every call, is never logged or persisted (we log the `object_key`), and — per the
supersede/no-clawback rule (#48) — an already-minted URL keeps working until it
expires even if the item is later un-shared. The short TTL is the only bound.
`mime/width/height/byte_len` live on the row so display needs no S3 round-trip.

### Vision egress (cross-boundary, opt-in)

When an image has no caller description, a `'shared'` capture is described by a
third-party vision model (OpenRouter) — the derivative image bytes leave the
host. A `'private'`/default capture is **never** egressed: it fails closed to a
deterministic placeholder and a `description_pending` flag for a later
re-description pass. This is the only path that sends image bytes off-box.

### Backup (object bucket is a second backup surface)

`pg_dump` does NOT capture the bucket. A DB-only restore that lost the bucket
leaves every attachment row pointing at a missing object. Treat the bucket as a
first-class backup surface: mirror it (restic / `mc mirror`) on the same schedule
as the DB dump, add `brain.attachments` + `brain.blobs` to the restore-drill
counts, and HEAD a sample of `object_key`s in the drill so a lost bucket FAILS
the drill instead of passing silently. (Wiring the mirror into `backup.sh` + the
systemd unit is a tracked follow-up.)

### GC of orphan blobs (follow-up)

Deleting an experience cascades its `attachments` row but leaves the shared blob
(other experiences may reference it). A blob is reap-eligible only when zero live
attachments reference its `blob_id` AND its object is older than a grace horizon
(>=24h, safely past the longest capture). The reaper itself is a scoped
follow-up; the orphan-detection reconciliation (`image_captures.orphan_blob_keys`)
and its integration test ship now.
