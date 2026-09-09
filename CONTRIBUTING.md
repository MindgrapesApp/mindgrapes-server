# Contributing to Mind Grapes

Thanks for your interest. This is a small, opinionated project — reading this
page first will save you a round trip.

## Before you build anything

**Open an issue first.** Features, bugs, and the roadmap live in GitHub
Issues; design discussion happens there before code. The architecture has
strong opinions — one generic entity/claim graph, no per-use-case tables, an
append-mostly supersede pattern — and the reasoning is written down. Read:

- `CLAUDE.md` — the repo guide (layout, house rules, where things live)
- `docs/design-overlay-architecture-2026-07-08.md` — why the data model is
  the way it is
- The GitHub issue/PR that shipped whatever you want to change — the
  alternatives were usually already weighed there

Two rules that surprise people:

- **The MCP tool surface is a forever API.** New tools need a missing
  affordance, not convenience. Every tool description in
  `web/openbrain/mcp/descriptions.py` follows a five-section template;
  changes to tools update that file in the same PR.
- **Schema changes are append-only.** Never edit an applied `init/NN-*.sql`
  file — add a new one, register it in the `SPINE` in
  `web/openbrain/mcp/boot.py` and the `init/14` self-seed (a unit test
  enforces the pairing).

## Dev setup

Prerequisites: Docker, [uv](https://docs.astral.sh/uv/), Python 3.12, and
[trufflehog](https://github.com/trufflesecurity/trufflehog) (`brew install
trufflehog`) for the secret-scan hook.

```sh
git clone https://github.com/MindgrapesApp/mindgrapes-server
cd mindgrapes-server
cd web && uv sync && cd ..            # Python deps (for running tests locally)
uv run --project web pre-commit install   # secret-scan hook (needs trufflehog on PATH)
cp .env.dev.example .env.dev          # dev-only placeholders, gitignored
make dev-up                           # isolated dev stack: postgres + web + mcp + consolidation + caddy
```

- `http://localhost:8080/` → Django
- `http://localhost:8080/mcp` → MCP server (401 gate when unauthenticated)
- `127.0.0.1:5433` → dev Postgres (`openbrain_dev`)

The dev stack is fully isolated from any live deployment on the same machine
(separate volumes, ports, project names — see `docs/deploy.md`). `make
dev-down` keeps volumes; `make dev-nuke` destroys them.

## Tests

```sh
cd web && uv run pytest               # unit suite — fast, sqlite, no Postgres
make dev-test-integration             # integration suite against the dev Postgres
```

Rules:

- Write tests in the layer the change touches: pure logic → a unit test in
  the relevant `web/openbrain/*/tests/unit/`; anything touching Postgres or
  HTTP → `*/tests/integration/`.
- The full unit suite must pass, with pristine output, before you open a PR.
- CI (`.github/workflows/prod-images.yml`) builds the prod image and runs
  Django's system check on every PR.

## Style

Match the surrounding code — it is the style guide. Python 3.12 / Django,
function-based views, raw parameterized SQL at the `brain.*` seam (no Django
models for `brain.*`), Pydantic schemas at the MCP boundary, very few
comments. Don't import external conventions, don't add speculative
abstractions.

## PRs

1. Branch from `main` (`feature/<issue>-<desc>` or `fix/<issue>-<desc>`).
2. Keep the diff scoped to one issue.
3. Reference the issue in the PR description.
4. Never commit secrets — `.env` and `.env.dev` are gitignored and must stay
   that way. A `pre-commit` trufflehog hook scans staged files for leaked keys
   and tokens; don't bypass it with `--no-verify`.

## Releases

Versions are date-based: `YYYY.M.Build`. `Build` starts at 1 and increments
within the calendar month, resetting when the month rolls over — `2026.8.1`,
`2026.8.2`, `2026.9.1`. The month is unpadded so the tag matches what PEP 440
normalizes `web/pyproject.toml` to.

There is no `CHANGELOG.md`. The commit history is the record of *what*
changed, and GitHub generates the release notes from merged PRs. What history
can't reconstruct is what an operator has to *do*, so every release body opens
with an `## Upgrading` block covering new `init/NN-*.sql` migrations, new
required `.env` keys, and any new compose services.

To cut one:

1. Branch `chore/release-<version>`.
2. Set the version in `web/pyproject.toml` and `SERVER_VERSION` in
   `web/openbrain/mcp/server.py` — the latter is what MCP clients see.
3. Open the PR, let CI pass, merge.
4. From `main`: `git tag -a v<version> -m "…"` and `git push origin v<version>`.
5. `gh release create v<version> --generate-notes`, then edit the body to put
   the `## Upgrading` block on top.

Tags go to `origin` only.

Security issues: do **not** open a public issue — see [SECURITY.md](SECURITY.md).
