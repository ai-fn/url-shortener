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
