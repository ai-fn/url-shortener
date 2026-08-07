<p align="center">
  <img src="app/static/favicon.svg" width="64" height="64" alt="URL Shortener logo" />
</p>

<h1 align="center">URL Shortener</h1>

<p align="center">
  A FastAPI URL shortener built around one idea: the redirect path and the analytics
  path have opposite goals, so the architecture keeps them from ever touching.
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white" />
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white" />
  <a href="https://github.com/ai-fn/url-shortener/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/ai-fn/url-shortener/actions/workflows/ci.yml/badge.svg" /></a>
  <img alt="License" src="https://img.shields.io/badge/license-MIT-blue.svg" />
</p>

---

Redirects must do almost nothing; analytics wants to record everything. Every
structural decision in this repo follows from keeping those two apart:

```
GET /{code}  →  Redis  →  302                          (synchronous, must never block)
             →  bounded queue  →  Kafka  →  ClickHouse (asynchronous, may drop)
```

**Analytics is allowed to lose data. Redirects are not allowed to be slow.** When
those two conflict, redirects win — every time, without discussion. The full design
lives in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Features

What's live today:

- **Click analytics pipeline** — every redirect enriches an event in-process (HMAC'd
  /24-truncated IP, UA parse, GeoIP country, bot detection) and hands it to a bounded
  queue with `put_nowait`. A background drain task owns the Kafka producer and is the
  only place a send is ever awaited, so a broker outage costs counted drops rather
  than slow redirects. ClickHouse consumes the topic through a Kafka engine table
  split into two materialized views — good rows to `clicks_raw`, unparseable ones to
  `clicks_dlq` — and rolls up hourly and daily with `uniqState(event_id)`, which stays
  correct under the at-least-once redelivery the Kafka engine guarantees.
- **Redis-cached redirects** — `GET /{code}` resolves from a Redis cache-aside layer
  first; only a miss touches Postgres. Unknown codes get a short-TTL negative
  sentinel, so enumeration can't turn into a Postgres load test. Cache reads fail open
  (Redis down degrades to a Postgres-backed redirect, never a failure). Cache
  invalidation fails closed on `PATCH`/`DELETE` (a mutation whose invalidation can't
  be confirmed returns `503` rather than silently reporting success on a link that
  isn't live yet) but fails open on `POST` — retrying a create with the same
  `custom_alias` would collide with the row the first request already made, so a
  cache hiccup there is logged instead of turned into a `503` that invites exactly
  that retry. The reasoning split is in
  [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
- **`/metrics`** — `link_cache_lookups_total{result}`, `redirect_duration_seconds`,
  `clicks_produced_total`, `clicks_dropped_total{reason}`, `click_queue_depth` and
  `enrich_duration_seconds`, exposed via `prometheus_client` directly rather than a
  global middleware, so nothing extra runs on the redirect's hot path.
- **Auth + link ownership** — register/login issues a JWT bearer access token;
  every link mutation requires one, and links carry a `NOT NULL owner_id`.
  Cross-owner access to a link 404s, never 403s — the response never confirms
  a link exists before confirming it's yours.
- **Link CRUD** — create, read, update, and soft-delete short links via a REST API,
  with optional custom aliases, titles, descriptions, and expiry.
- **SSRF-hardened URL validation** — every target URL is resolved and every
  returned address classified before storage, closing the classic bypasses
  (encoded/octal/decimal IPs, IPv4-mapped IPv6, CGNAT, the cloud metadata
  endpoint) rather than pattern-matching the input string.
- **Atomic Redis rate limiting** — a Lua token bucket protects link creation,
  register/login, and the redirect's 404 budget, with an explicit
  fail-open/fail-closed policy per endpoint instead of one default for both.
- **Safe-by-construction redirects** — always `302` with `Cache-Control: no-store`,
  a reserved-path-aware catch-all route, and header-injection checks on the way out.
  The redirect path never imports auth — it stays public and fast regardless of
  what the auth surface does.
- **Schema managed by Alembic**, driven by the same `Settings` the app itself reads,
  so migrations and the running service can never target different databases.

Targeted by the architecture, not yet built: the analytics read API over the
ClickHouse rollups.

## Architecture

```mermaid
flowchart LR
    Client(["Client"])

    subgraph Sync["Redirect path — synchronous, never blocks"]
        Redirect["GET /{code}"]
        Redis[("Redis\ncache + rate limiter")]
        Postgres[("Postgres\nsource of truth")]
    end

    subgraph Async["Analytics path — asynchronous, may drop"]
        Queue["bounded queue"]
        Kafka[("Kafka")]
        ClickHouse[("ClickHouse")]
    end

    Client -->|"POST /api/v1/links"| Postgres
    Client --> Redirect
    Redirect -->|hit| Redis
    Redirect -.miss, populates cache.-> Postgres
    Redirect -.put_nowait, drops if full.-> Queue --> Kafka --> ClickHouse
```

| Component | Role | May the redirect block on it? |
|---|---|---|
| Postgres | Source of truth for links (and users, later) | Only on a cache miss |
| Redis | Redirect cache and rate limiter — never a source of truth | Yes, it's the fast path |
| Kafka | Click event transport | **No** |
| ClickHouse | Analytics store, ingesting via its native Kafka table engine | **No** |

Redis is invalidated with `DEL`, never `SET` — a `SET` races an in-flight reader
holding a stale row. Kafka and ClickHouse are deliberately excluded from `/readyz`:
a redirect still succeeds with both down, so failing readiness on them would take
the whole service offline to protect analytics — exactly backwards.

## Stack

Python 3.12 · FastAPI · SQLAlchemy (async) + asyncpg · Alembic · Redis · Kafka
(aiokafka) · ClickHouse · structlog · [uv](https://docs.astral.sh/uv/) ·
ruff + mypy (strict) · pytest + pytest-asyncio · Docker Compose.

## Prerequisites

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/)
- Docker, for the local dependency stack (Postgres, Redis, Kafka, ClickHouse)

## Getting started

```bash
git clone https://github.com/ai-fn/url-shortener.git
cd url-shortener

uv sync                    # install (Python 3.12)
```

```bash
# .env configures host-side runs (pytest, alembic, scripts). The compose stack does
# NOT read it — every value the api container uses is set in docker-compose.yml —
# so editing .env and restarting compose changes nothing.
cp .env.example .env
docker compose up -d --wait

curl -i localhost:8000/healthz    # liveness — no dependencies
curl -i localhost:8000/readyz     # readiness — Postgres + Redis
open http://localhost:8000/docs
```

## Usage

```bash
# Register, then log in for a bearer token
curl -X POST localhost:8000/api/v1/auth/register \
  -H 'content-type: application/json' \
  -d '{"email": "you@example.com", "password": "correct horse battery staple"}'

TOKEN=$(curl -sS -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email": "you@example.com", "password": "correct horse battery staple"}' \
  | jq -r .access_token)

# Create a short link — link mutations require the token
curl -X POST localhost:8000/api/v1/links \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"target_url": "https://example.com/", "title": "demo"}'

# Follow it — 302, Cache-Control: no-store. No token needed; the redirect is public.
curl -i localhost:8000/<short_code>
```

| Command | Description |
|---|---|
| `uv run pytest -m "not integration"` | Unit tests — no containers, under ~10s |
| `uv run pytest -m integration` | Integration tests — needs the compose stack up |
| `uv run ruff check . && uv run ruff format .` | Lint and format |
| `uv run mypy app/` | Type check (strict) |
| `uv run python scripts/check_invariants.py` | Architectural invariant gate |
| `uv run alembic upgrade head` | Apply migrations (host-side, against `.env`) |
| `docker compose up -d --wait` | Start the full local stack, migrations included |

## Configuration

Every setting is env-overridable through `app/config.py`'s `Settings`. Copy
[`.env.example`](.env.example) to `.env` for host-side runs (pytest, Alembic); the
compose stack sets its own values directly in `docker-compose.yml`.

> [!IMPORTANT]
> `SECRET_KEY` and `IP_HASH_KEY` must be ≥32 characters and replaced before any
> non-local use. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

## Project structure

```
app/
  api/            # Route handlers — thin adapters, no business logic
                  #   redirect.py is the hot path; changes here need unusual care
  services/       # Business logic — routes delegate here
                  #   redirect.py is the cache-aside resolver the route delegates to
  cache/          # Redis link cache — cache-aside reads, DEL-only invalidation
  core/           # Short codes, URL validation, rate limiting, auth (hashing + JWT),
                  #   Prometheus collectors
  events/         # Click event schema, enrichment, Kafka producer + drain task
  models/         # SQLAlchemy ORM models
clickhouse/       # Server config for the compose container, plus numbered SQL
                  #   migrations: Kafka engine table, ingest + DLQ MVs, raw, rollups
migrations/       # Alembic, Postgres only
scripts/          # check_invariants.py — the committed architectural gate
                  # apply_clickhouse_migrations.py — idempotent ClickHouse runner
tests/            # Unit (no containers) + integration (needs the compose stack)
docs/
  ARCHITECTURE.md # The full design
```

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — the full system design: the
  redirect path, the analytics path, and why they're built the way they are.

## License

MIT — see [`LICENSE`](LICENSE) for the full text.
