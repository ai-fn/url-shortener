# Architecture

## The one idea

The redirect path and the analytics path have opposite goals. Redirects must do almost
nothing; analytics wants to record everything. Every structural decision in this repo
follows from keeping those two apart:

```
GET /{code}  →  Redis  →  302                          (synchronous, must never block)
             →  bounded queue  →  Kafka  →  ClickHouse (asynchronous, may drop)
```

**Analytics is allowed to lose data. Redirects are not allowed to be slow.**
When those two conflict, redirects win — every time, without discussion.

That sentence is the whole design. If a change makes the redirect depend on anything
that can be slow or down, it is wrong regardless of how much analytics fidelity it buys.

## Components

| Component | Role | May the redirect block on it? |
|---|---|---|
| Postgres | Source of truth for users and links | Only on a cache miss |
| Redis | Redirect cache and rate limiter — never a source of truth | Yes, it is the fast path |
| Kafka | Click event transport | **No** |
| ClickHouse | Analytics store, ingesting via its native Kafka table engine | **No** |

Kafka and ClickHouse are deliberately excluded from `/readyz` for the same reason: a
redirect still succeeds with both down (clicks are dropped and counted), so failing
readiness on them would take the service offline to protect analytics — exactly
backwards.

## The redirect path

1. `GET /{code}` looks up the code in Redis.
2. Hit → 302 to the destination with `Cache-Control: no-store`.
3. Miss → read Postgres, populate Redis, then 302. A miss for a code that does not
   exist writes a negative sentinel with a short TTL, so code enumeration cannot turn
   into a Postgres load test.
4. Regardless of hit or miss, a click event is built and handed to a **bounded**
   in-process queue with `put_nowait`. A full queue is a counted drop, never a wait.

Step 4 is where the two paths separate, and it is the single most load-bearing line in
the service.

Redis is a cache and never a source of truth: every value in it is reconstructible from
Postgres. Invalidation is `DEL`, never `SET` — a `SET` races an in-flight reader holding
a stale row and can install the stale value permanently.

## The analytics path

A background drain task owns the Kafka producer. It is the consumer of the queue the
request path feeds, and it is the one place in the codebase where awaiting
`producer.send()` is correct — it is not on the request path.

ClickHouse ingests with its native Kafka table engine:

```
kafka_clicks_queue (Kafka engine)  →  MV  →  clicks_raw (ReplacingMergeTree)
                                          →  MV  →  rollups (AggregatingMergeTree)
```

The Kafka engine is **at-least-once**. Materialized views fire per inserted block, and
`ReplacingMergeTree` deduplication happens later, during merges. Two consequences drive
the schema:

- Rollups aggregate with `uniqState(event_id)`, never `countState()`, because nothing
  rewinds an aggregate that has already counted a redelivery.
- `event_id` and `ts` are generated **once, in the handler** — never `DEFAULT now()`,
  never the Kafka `_timestamp` virtual column. A `ts` that varies per delivery silently
  disables dedup, because the dedup key is the full `ORDER BY` tuple.

Rollup tables get no TTL. Outliving `clicks_raw` is the entire point of them.

## Time

All times are UTC. Columns are `DateTime64(3, 'UTC')` and rollups call
`toDate(ts, 'UTC')` explicitly. Without the explicit zone, the container's `TZ` shifts
bucket boundaries and the daily numbers quietly stop lining up.

## Safety

We are running a public open redirect, so every user-supplied URL is validated before
storage: scheme allowlist, resolved-IP checks, and a self-domain loop guard comparing
against `Settings.public_host`. That setting is typed `AnyHttpUrl` rather than `str`
specifically so a scheme-less value fails at startup instead of yielding an empty host
and disarming the guard.

## Operational shape

- `/healthz` is liveness and touches nothing. A Redis blip must not restart every pod.
- `/readyz` is readiness and checks Postgres and Redis, concurrently and with a timeout.
  Probe orchestration lives in `app/services/health.py`; the route only maps the report
  onto a status code.
- Logging is configured once, in `app/core/logging.py`: structlog rendering both its own
  and stdlib records, JSON everywhere except `local`. uvicorn's default config declares
  no root logger, so without this an application record is dropped or escapes through
  `logging.lastResort` with its `extra` fields discarded.
- `/metrics` exports Prometheus text via `prometheus_client` directly, not
  `prometheus-fastapi-instrumentator` — that library's `.instrument()` installs a
  global middleware that would run on every redirect for no benefit the hot path
  needs, and would need its own exemption from the middleware-registration invariant.
  `link_cache_lookups_total{result}` and `redirect_duration_seconds` cover the cache
  and the response; `clicks_produced_total`, `clicks_dropped_total{reason}`,
  `click_queue_depth` and `enrich_duration_seconds` cover the click pipeline. A
  sustained non-zero drop rate means the drain task or the broker needs attention —
  it is the only outward sign, since nothing on the redirect path fails when Kafka is
  gone.
- ClickHouse migrations are numbered SQL under `clickhouse/migrations/`, one statement
  per file, applied by `scripts/apply_clickhouse_migrations.py` against a
  `schema_migrations` table. It runs as the one-shot `migrate-clickhouse` compose
  service, for the same reason `migrate` exists for Alembic: N replicas starting
  together must not race the same DDL. Every statement is idempotent — `IF NOT EXISTS`
  on creates, `MODIFY`/`REPLACE` on alters — so a crash between executing and
  recording self-heals on the next run.

### Cache failure policy: reads fail open, invalidation fails closed except on create

Redis being unreachable means different things depending on where it's seen, so the
policy isn't one rule but three, laid out below — including why creation isn't grouped
with the other two mutations.

- **`GET /{code}`**: a `LinkCacheUnavailable` from the lookup is counted
  (`link_cache_lookups_total{result="error"}`) and treated as a miss — the redirect
  falls through to Postgres rather than failing. A failure to populate the cache
  afterward is logged and swallowed for the same reason: a slow redirect beats a
  failed one.
- **`PATCH/DELETE /api/v1/links/{id}`**: invalidation runs after `commit()`, and if the
  `DEL` raises, the route returns `503` instead of reporting success. The Postgres row
  is already correct at that point; the response is telling the caller the change
  isn't guaranteed live yet, not that it failed to save. Both are safe to retry — same
  `link_id`, and `DEL` on an absent key is a no-op.
- **`POST /api/v1/links`**: invalidation also runs after `commit()`, but a failure
  there is logged and swallowed rather than raised — a `503` here would invite a retry,
  and retrying a `POST` with the same `custom_alias` collides with the row the first
  request already created, turning a success into a spurious `409`. There's also no
  stale *old* value at risk on creation, only a possibly-stale negative sentinel that
  self-heals within `link_cache_negative_ttl_seconds` and is further guarded by the
  fencing generation `invalidate()` bumps (see `app/services/redirect.py`).

## Enforcement

Some tooling and notes in this working tree are deliberately untracked: editor aids,
hooks and longer-form decision records that reach no collaborator, no fresh clone and
no CI run. They are fast feedback and private depth, never a gate. Nothing committed
may depend on them being present, which is why each linter rule carries its own
rationale in its `message` rather than pointing at a document a reader may not have.

Real enforcement is committed:

- `scripts/check_invariants.py`, run in the CI `lint` job. Each rule carries its own
  rationale in the rule's `message`, so a CI failure explains itself. The rule count
  is printed by the script itself; it is not restated here, so it cannot drift.
- The test suite, in particular the three cases that cover failures nothing else
  catches: a duplicate `event_id` does not inflate rollups; a malformed Kafka message
  lands in `clicks_dlq` without wedging the consumer; redirects keep working with the
  broker stopped.

When an invariant changes, `scripts/check_invariants.py` is the artifact that must
change with it, in the same commit.
