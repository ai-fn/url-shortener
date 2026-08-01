"""Tests for the invariant linter itself: violations are caught, and legitimate
code is not."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.check_invariants import check

# (relative path, file contents, should_be_flagged)
CASES: list[tuple[str, str, bool]] = [
    # --- must be flagged ---
    (
        "clickhouse/migrations/002_rollups.sql",
        "CREATE MATERIALIZED VIEW m TO t AS SELECT link_id, countState() FROM clicks_raw;",
        True,
    ),
    (
        # The mandated column form: a bound that cannot cross the comma never fires here.
        "clickhouse/migrations/001_raw.sql",
        "CREATE TABLE clicks_raw (ts DateTime64(3, 'UTC') DEFAULT now64(3));",
        True,
    ),
    (
        "clickhouse/migrations/003_kafka.sql",
        "CREATE MATERIALIZED VIEW ingest TO clicks_raw AS SELECT * FROM kafka_clicks_queue;",
        True,
    ),
    (
        "app/api/redirect.py",
        "async def r():\n    await request.app.state.producer.send('clicks', event)\n",
        True,
    ),
    (
        # Logic in a service, as "routes stay thin" encourages: follow the call path,
        # not one filename.
        "app/services/redirect_service.py",
        "async def r():\n    await self.producer.send('clicks', event)\n",
        True,
    ),
    ("app/api/redirect.py", "return RedirectResponse(url=t, status_code=301)\n", True),
    ("app/api/redirect.py", "status_code=status.HTTP_301_MOVED_PERMANENTLY\n", True),
    ("app/api/redirect.py", "status_code=HTTPStatus.MOVED_PERMANENTLY\n", True),
    (
        # gethostbyname() returns one address; a host with both a public and a
        # loopback A record passes validation on whichever it happens to return.
        "app/core/url_validation.py",
        "def resolve(host):\n    return socket.gethostbyname(host)\n",
        True,
    ),
    (
        # The redirect path must never grow a per-request auth dependency.
        "app/api/redirect.py",
        "from app.api.deps import get_current_user\n",
        True,
    ),
    (
        # The vector the file-scoped rule above can't see: auth added at
        # registration time, not inside redirect.py itself.
        "app/main.py",
        "app.include_router(redirect.router, dependencies=[Depends(get_current_user)])\n",
        True,
    ),
    (
        "app/main.py",
        "app.include_router(\n"
        "    redirect.router,\n"
        "    dependencies=[Depends(get_current_user)],\n"
        ")\n",
        True,
    ),
    (
        # Auth-free today, but app.api.deps pulls in the whole auth stack transitively.
        "app/api/redirect.py",
        "from app.api.deps import client_ip\n",
        True,
    ),
    (
        # Neither auth-on-redirect rule can see a global middleware.
        "app/main.py",
        "app.add_middleware(AuthMiddleware)\n",
        True,
    ),
    (
        "app/main.py",
        '@app.middleware("http")\nasync def check_auth(request, call_next):\n    ...\n',
        True,
    ),
    # --- must NOT be flagged ---
    (
        "clickhouse/migrations/002_rollups.sql",
        "CREATE MATERIALIZED VIEW m TO t AS SELECT link_id, uniqState(event_id) FROM clicks_raw;",
        False,
    ),
    (
        # DLQ metadata legitimately needs a server-assigned timestamp.
        "clickhouse/migrations/004_dlq.sql",
        "CREATE TABLE clicks_dlq (raw String, ingested_at DateTime DEFAULT now());",
        False,
    ),
    (
        "clickhouse/migrations/005_backfill.sql",
        "CREATE MATERIALIZED VIEW m TO r AS SELECT a, b FROM q;\n"
        "INSERT INTO new SELECT * FROM clicks_raw;",
        False,
    ),
    (
        # Legal because `app/events/` is exempt, not per-file — see
        # test_drain_task_is_out_of_hot_path_scope for the actual reason.
        "app/events/producer.py",
        "async def drain():\n    fut = await producer.send('clicks', event)\n",
        False,
    ),
    ("app/api/redirect.py", "# 302, never 301 - browsers cache it forever\n", False),
    ("app/api/redirect.py", "return RedirectResponse(url=t, status_code=302)\n", False),
    (
        # The prescribed call: a gate that flags it rejects the fix it prescribes. What
        # keeps it clean is the `await\s+` prefix, not the `put(` spelling, so this row
        # catches dropping the await requirement rather than a `put\w*` widening.
        "app/api/redirect.py",
        "async def r():\n    click_q.put_nowait(event)\n",
        False,
    ),
    (
        # The prescribed fix: resolve every address, not just the first.
        "app/core/url_validation.py",
        "async def resolve(host):\n"
        "    infos = await loop.getaddrinfo(host, None)\n"
        "    return [i[4][0] for i in infos]\n",
        False,
    ),
    (
        # Auth belongs on link mutations, not the redirect — but the rule scopes
        # narrowly to app/api/redirect.py, so this must stay clean.
        "app/api/links.py",
        "from app.api.deps import get_current_user\n",
        False,
    ),
    (
        # The real registration: no dependencies=, so nothing in [^)]* reaches
        # `get_current_user` before the call's own closing paren.
        "app/main.py",
        "app.include_router(redirect.router)\n",
        False,
    ),
    (
        # get_current_user used elsewhere in main.py, on a *different* router's
        # registration, must not trip the rule scoped to redirect.router's own call.
        "app/main.py",
        "app.include_router(links.router, dependencies=[Depends(get_current_user)])\n"
        "app.include_router(redirect.router)\n",
        False,
    ),
    (
        # Same call, but scoped outside app/ — a test fixture wiring up its own ASGI
        # app for an unrelated check must not trip a rule meant for the real app.
        "tests/conftest.py",
        "app.add_middleware(AuthMiddleware)\n",
        False,
    ),
]


@pytest.mark.parametrize(("rel", "content", "should_flag"), CASES)
def test_invariant_rules(tmp_path: Path, rel: str, content: str, *, should_flag: bool) -> None:
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)

    violations = check(tmp_path)

    assert bool(violations) is should_flag, (
        f"{rel} -> expected flagged={should_flag}, got {violations}"
    )


def test_repository_itself_is_clean() -> None:
    assert check(Path(__file__).resolve().parents[1]) == []


@pytest.mark.parametrize("enclosing", ["docs", "scripts", "node_modules", ".claude", ".github"])
def test_skip_dirs_are_relative_to_root_not_absolute(tmp_path: Path, enclosing: str) -> None:
    """`~/docs/url-shortener` is an ordinary place to clone; matching SKIP_DIRS against
    the absolute path made the whole linter pass vacuously there."""
    root = tmp_path / enclosing / "url-shortener"
    target = root / "clickhouse/migrations/002_rollups.sql"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("CREATE MATERIALIZED VIEW m TO t AS SELECT link_id, countState() FROM r;")

    violations = check(root)

    assert len(violations) == 1, f"expected the countState() violation, got {violations}"
    assert "rollup-not-idempotent" in violations[0]


def test_skip_dirs_still_apply_inside_the_tree(tmp_path: Path) -> None:
    """Docs quote `countState()` as a counter-example and must not trip the linter."""
    target = tmp_path / "docs/adr/0001-rollup-idempotency.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("Never use countState() in a rollup.")

    assert check(tmp_path) == []


def test_drain_task_is_out_of_hot_path_scope(tmp_path: Path) -> None:
    """Asserted on the rules themselves, not scanned output — an exemption that
    stopped being applied would otherwise look identical to one that works. Defaults
    to requiring HOT_PATH_EXEMPT on every app/-scoped rule; an unrelated one opts
    out explicitly, by name, with a reason on file."""
    from scripts.check_invariants import HOT_PATH, HOT_PATH_EXEMPT, RULES

    assert any("app/events/".startswith(p) for p in HOT_PATH), (
        "app/events/ must be in scope, so the exemption is what protects it"
    )
    not_about_the_drain_task = {
        "single-address-dns-resolution": "flags unsafe DNS resolution, unrelated to "
        "awaiting Kafka/queue calls on the request path",
        "global-middleware-registration": "flags middleware registration, unrelated to "
        "awaiting Kafka/queue calls on the request path — the drain task registers no "
        "middleware",
    }
    hot_path_rules = [
        r for r in RULES if r.prefixes == HOT_PATH and r.name not in not_about_the_drain_task
    ]
    assert hot_path_rules, "expected rules scoped to the hot path"
    for rule in hot_path_rules:
        assert rule.exempt_prefixes == HOT_PATH_EXEMPT, (
            f"{rule.name} scans the hot path without exempting the drain task"
        )

    target = tmp_path / "app/events/producer.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("async def drain():\n    await producer.send('clicks', event)\n")

    assert check(tmp_path) == []


@pytest.mark.parametrize("path", ["app/cache/link_cache.py", "app/core/rate_limit.py"])
def test_cache_and_core_are_on_the_hot_path(tmp_path: Path, path: str) -> None:
    """The redirect blocks on the link cache and the rate limiter, so rules must reach them."""
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "async def get(code):\n"
        "    await producer.send('clicks', e)\n"
        "    return RedirectResponse(url=t, status_code=301)\n"
    )

    names = {v.split("[")[1].split("]")[0] for v in check(tmp_path)}

    assert names == {"blocking-producer-on-hot-path", "permanent-redirect"}


def test_every_match_is_reported_not_just_the_first(tmp_path: Path) -> None:
    """One violation per file per rule meant N CI round trips to clear N violations."""
    target = tmp_path / "clickhouse/migrations/003_rollups.sql"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "CREATE MATERIALIZED VIEW a AS SELECT countState() FROM r;\n"
        "CREATE MATERIALIZED VIEW b AS SELECT countState() FROM r;\n"
        "CREATE MATERIALIZED VIEW c AS SELECT countState() FROM r;\n"
    )

    violations = check(tmp_path)

    assert len(violations) == 3, violations
    assert [v.split(":")[1].split(" ")[0] for v in violations] == ["1", "2", "3"]
