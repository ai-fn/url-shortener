# URL Shortener

FastAPI URL shortener with Redis-cached redirects and ClickHouse click analytics.

The redirect path and the analytics path have opposite goals — redirects must do
almost nothing, analytics wants to record everything. The architecture keeps them
apart:

```
GET /{code}  →  Redis  →  302                          (synchronous, never blocks)
             →  bounded queue  →  Kafka  →  ClickHouse (asynchronous, may drop)
```

**Analytics is allowed to lose data. Redirects are not allowed to be slow.**

| Component | Role |
|---|---|
| Postgres | Source of truth for users and links |
| Redis | Redirect cache and rate limiter — never a source of truth |
| Kafka | Click event transport |
| ClickHouse | Analytics store, ingesting via its native Kafka table engine |

## Quick start

```bash
# .env configures host-side runs (pytest, scripts). The compose stack does NOT
# read it — every value the api container uses is set in docker-compose.yml — so
# editing .env and restarting compose changes nothing.
cp .env.example .env
docker compose up -d --wait

curl -i localhost:8000/healthz    # liveness — no dependencies
curl -i localhost:8000/readyz     # readiness — Postgres + Redis
open http://localhost:8000/docs
```

## Development

```bash
uv sync                                   # install (Python 3.12)
uv run pytest -m "not integration"        # fast unit tests, no containers
uv run pytest -m integration              # needs the compose stack up
uv run ruff check . && uv run ruff format .
uv run mypy app/
uv run python scripts/check_invariants.py # architectural invariants
```

## Status

Milestone 1 of 8 — skeleton, health endpoints, CI. See
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design.

Roadmap: links + Postgres → auth + ownership → Redis cache → ClickHouse/Kafka ingest
→ analytics API → observability → docs.

## Notes for contributors

`scripts/check_invariants.py` enforces the decisions that fail *silently* if broken —
each produces wrong data rather than an exception, which is why a linter and not a
test is the right instrument. The rules today:

| Rule |
|---|
| `blocking-producer-on-hot-path` |
| `rollup-not-idempotent` |
| `server-assigned-event-timestamp` |
| `select-star-in-materialized-view` |
| `permanent-redirect` |

Each rule's full rationale lives in its `message` in that file, which is the text CI
prints when it fires — deliberately not restated here. Four near-identical copies of
the same paragraphs is how the explanation and the rule drift apart, and the copy a
contributor reads then contradicts the one CI enforces.

These are decisions, not style. If you need to change one, change the linter rule in the
same commit and say why in the commit message — a rule that no longer matches the code
is worse than no rule, because the next reader trusts it.
