# Mind Grapes

A self-hosted second brain any AI tool can plug into. One Postgres database,
one MCP server, one persistent memory layer — shared by claude.ai, Claude
Desktop, Claude Code, and any other MCP client you use.

Capture thoughts from anywhere; Mind Grapes extracts entities and
subject-predicate-object claims, embeds everything for hybrid semantic +
lexical search, and keeps full provenance so you can always ask "where did I
learn that?" — and get an honest answer, including "that was inferred, not
stated."

<img width="400" height="226" alt="mind_grapes" src="https://github.com/user-attachments/assets/8dcd7f7c-f609-4f29-bd42-31f25dd495ca" />

## Status

This started as a personal system: it has run since June 2026 as the author's
private, daily-driver memory layer — every schema migration, capture, and
search here was exercised against a real second brain before the repo was
packaged for public release. The public repo starts with a fresh git history;
issue numbers referenced in code comments (#41, #95, …) point at the original
private tracker and are kept as archaeology.

It is a single-household system by design: one deployment, a few trusted
members, passkey auth. It is not multi-tenant SaaS and doesn't try to be.

## Why one generic graph instead of per-use-case schemas

This project owes a debt to Nate B. Jones's
[OB1](https://github.com/NateBJones-Projects/OB1), which proved the core
premise — a personal database + MCP is the right substrate for cross-AI
memory — and ships a marketplace of installable schema packs for specific use
cases: a CRM (`professional_contacts`, `contact_interactions`,
`opportunities`), meal planning, job hunting, family calendars.

Mind Grapes deliberately inverts that design. There are no per-use-case
tables. The substrate is three generic structures:

- **experiences** — episodic memory: the things you captured, verbatim
- **entities** — one identity graph: every person, org, project, place
- **claims** — semantic memory: subject-predicate-object facts with
  provenance, confidence, and polarity

A "concept" like CRM is a *lens* over that substrate — a predicate vocabulary
plus a query plus a synthesis prompt, applied at read time. A "contact" is a
`person` entity, the experiences that mention it, and the claims about it.

Why this matters, concretely — with per-vertical tables, the CRM's
`professional_contacts.name` is a different row from the memory layer's
person of the same name: two identity stores, nothing reconciling them, and
"what do I know about Sarah?" hits one silo or the other. With one graph,
adding a use case is adding a query, not a migration — and every new concept
automatically inherits unified retrieval, the single entity graph,
provenance, supersede/correction auditing, and viewer privacy, because it is
the same rows. It also keeps the MCP tool count flat: capabilities compose
over one graph instead of each vertical shipping its own tool suite.

The full design record — including where the overlay stops and a typed
projection or an external app is the right escalation — is in
[`docs/design-overlay-architecture-2026-07-08.md`](docs/design-overlay-architecture-2026-07-08.md).

## Architecture

```
                  ┌──────────── Caddy edge ─────────────────────┐
  Browser   ────▶ │  / , /accounts/* , /admin/*  ──▶ Django (web) │
  AI client ────▶ │  /mcp* , /.well-known/*      ──▶ MCP (FastMCP)│
                  └──────────────────────────────────────────────┘
                         one Postgres, two schema owners:
                  Django → public.*      MCP → brain.* (raw SQL seam)
```

- **Postgres** with pgvector: episodic + semantic schema in `init/*.sql`,
  hybrid RRF search (`brain.match_brain_hybrid`), pg_cron consolidation
- **Python 3.12 / Django**: web UI, passkey (WebAuthn) auth, OAuth 2.1
  authorization server with Dynamic Client Registration — adding a connector
  in claude.ai "just works," no operator step
- **FastMCP server** (same codebase): the tool surface AI clients see —
  capture, search, recall, entity resolution, claim corrections, review queue
- **Consolidation worker**: extracts entities and claims from captures in the
  background via OpenRouter
- **Caddy + Tailscale**: TLS at the edge; reachable on your tailnet plus a
  public `*.ts.net` Funnel URL for clients that can't join it — no public VM,
  no DNS, no Let's Encrypt. Prefer your own domain? A public host with Caddy
  ACME is a supported path too (see [`docs/deploy.md`](docs/deploy.md))
- **Backups**: `pg_dump` + restic, encrypted offsite (e.g. Backblaze B2)

Mutations are append-mostly: edits and deletes flow through a supersede +
correction-event pattern, so the brain keeps an audit trail instead of
silently rewriting history.

## Quickstart

Prerequisites: Docker, an [OpenRouter](https://openrouter.ai) API key (for
embeddings + extraction).

```sh
git clone https://github.com/MindgrapesApp/mindgrapes-server
cd mindgrapes-server
cp .env.example .env
# Fill in: POSTGRES_PASSWORD, OPENROUTER_API_KEY,
#   SECRET_KEY   — python -c "import secrets; print(secrets.token_urlsafe(64))"
#   OAUTH_JWT_PRIVATE_KEY — docker compose run --rm --entrypoint python web manage.py gen_jwt_key

docker compose up --build
# Seed the first admin + a passkey-enrollment link:
docker compose exec web python manage.py bootstrap_admin you@example.com
```

Then open `https://localhost` (self-signed dev cert), enroll your passkey,
and paste the MCP URL from the `/connect` page into your AI client.

Production deployment — an always-on box, reachable over your tailnet, via
Tailscale Funnel for off-tailnet clients, or on your own public domain — is
documented in [`docs/deploy.md`](docs/deploy.md), including how to choose an
exposure path, backups, and the schema-migration boot gate.

For hacking on the code (isolated dev stack, test suites), see
[CONTRIBUTING.md](CONTRIBUTING.md).

## The MCP surface

Reads: `search_thoughts` (hybrid semantic+lexical), `recall_recent`
(time-anchored), `list_thoughts` (deterministic filters), `get_experience`,
`resolve_entity`, `relationships_to`, `who_was_at`, `thought_stats`.

Writes: `capture_thought`, `update_experience`, `propose_correction` /
`resolve_correction`, `retract_claim`, entity maintenance (`merge_entities`,
`split_entity`, `rename_entity`, `unmerge_entity`), and a `review_queue` for
merge candidates and low-confidence claims.

Every tool description follows a strict template (capability, use-when,
don't-use-when, on-empty, cost/idempotency) — the catalog lives in
[`web/openbrain/mcp/descriptions.py`](web/openbrain/mcp/descriptions.py).

## Client apps

Mind Grapes is a server — capture surfaces live in their own repos:

- [mindgrapes-extension](https://github.com/MindgrapesApp/mindgrapes-extension) —
  Chrome MV3 extension that bookmarks pages into Mind Grapes, summarizing the
  current page and storing it as an experience
- [mindgrapes-ios](https://github.com/MindgrapesApp/mindgrapes-ios) — iOS,
  watchOS, and Siri capture surface

## Roadmap & issues

Features, bugs, and the roadmap live in
[GitHub Issues](https://github.com/MindgrapesApp/mindgrapes-server/issues).
Open a discussion issue before building a feature — the architecture has
opinions (see the design record above), and the issue is where they get
weighed.

## Security

Security reports: see [SECURITY.md](SECURITY.md). Deployment hardening notes
are in [`docs/deploy.md`](docs/deploy.md); known open items are labeled in
the issue tracker.

## Credits

- [Nate B. Jones](https://github.com/NateBJones-Projects)'s OB1 — the proof
  that self-hosted, database-first, MCP-connected personal memory is the
  right shape, and the productive foil for this project's data-model
  decisions.
- Tracy Jordan, for the name.

## License

[MIT](LICENSE) © 2026 Joe Cotellese
