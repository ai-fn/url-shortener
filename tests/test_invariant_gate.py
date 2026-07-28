"""The gate's CLI surface, and the rule spellings that used to slip past it.

`check()` was the only thing under test, so the exit code, the stderr routing and the
root taken from argv — everything CI depends on — were unverified.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.check_invariants import check, main


def _write(root: Path, rel: str, body: str) -> Path:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


def test_main_exits_nonzero_and_writes_violations_to_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The exit code is the entire contract with CI; nothing else makes the job fail."""
    _write(tmp_path, "clickhouse/migrations/002_rollups.sql", "SELECT countState() FROM r;\n")

    exit_code = main(["check_invariants.py", str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "rollup-not-idempotent" in captured.err
    assert captured.out == "", "violations on stdout are invisible to a CI log scraper"


def test_main_exits_zero_on_a_clean_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path, "app/api/redirect.py", "def go():\n    return 302\n")

    assert main(["check_invariants.py", str(tmp_path)]) == 0
    assert "OK" in capsys.readouterr().out


def test_main_scans_the_root_it_is_given_not_the_cwd(tmp_path: Path) -> None:
    """A root argument that is ignored makes every CI invocation scan the wrong tree."""
    _write(tmp_path, "clickhouse/migrations/001.sql", "SELECT countState() FROM r;\n")
    empty = tmp_path / "elsewhere"
    empty.mkdir()

    assert main(["check_invariants.py", str(empty)]) == 0
    assert main(["check_invariants.py", str(tmp_path)]) == 1


@pytest.mark.parametrize(
    ("rel", "body", "rule"),
    [
        # Each reported OK before: the rule matched one spelling of a defect that has
        # several.
        (
            "app/api/redirect.py",
            "return RedirectResponse(url=t, status_code=status.HTTP_308_PERMANENT_REDIRECT)\n",
            "permanent-redirect",
        ),
        ("app/api/redirect.py", "response.status_code = 301\n", "permanent-redirect"),
        ("app/api/redirect.py", "return RedirectResponse(t, 301)\n", "permanent-redirect"),
        (
            "app/services/redirect_service.py",
            "async def go():\n    await click_q.put(event)\n",
            "blocking-producer-on-hot-path",
        ),
        (
            "app/services/redirect_service.py",
            "async def go():\n    await self._kafka.send(topic, event)\n",
            "blocking-producer-on-hot-path",
        ),
        # send_and_wait() awaits the broker ack too — worse here than send().
        (
            "app/services/redirect_service.py",
            "async def go():\n    await producer.send_and_wait(topic, event)\n",
            "blocking-producer-on-hot-path",
        ),
        (
            "app/services/redirect_service.py",
            "async def go():\n    await self._producer.send_batch(batch, topic)\n",
            "blocking-producer-on-hot-path",
        ),
        (
            "app/services/redirect_service.py",
            "async def go():\n    await asyncio.wait_for(producer.send_and_wait(t, v), 1)\n",
            "blocking-producer-on-hot-path",
        ),
        (
            "clickhouse/migrations/002_rollups.sql",
            "CREATE MATERIALIZED VIEW m TO t AS SELECT link_id, countIfState(ok) FROM r;\n",
            "rollup-not-idempotent",
        ),
        (
            "clickhouse/migrations/003_kafka.sql",
            "CREATE MATERIALIZED VIEW i TO raw AS SELECT event_id, _timestamp AS ts FROM q;\n",
            "server-assigned-event-timestamp",
        ),
        (
            "clickhouse/migrations/001_raw.sql",
            "CREATE TABLE raw (ts DateTime64(3, 'UTC') MATERIALIZED now64(3));\n",
            "server-assigned-event-timestamp",
        ),
        (
            "clickhouse/migrations/003_kafka.sql",
            "CREATE MATERIALIZED VIEW m TO raw AS SELECT q.* FROM kafka_queue q;\n",
            "select-star-in-materialized-view",
        ),
    ],
)
def test_defect_spellings_that_used_to_pass_are_flagged(
    tmp_path: Path, rel: str, body: str, rule: str
) -> None:
    _write(tmp_path, rel, body)

    names = {v.split("[")[1].split("]")[0] for v in check(tmp_path)}

    assert rule in names, f"{rule} missed {body.strip()!r}"


def test_a_new_app_package_is_enforced_without_being_listed(tmp_path: Path) -> None:
    """New code is in scope by default. As an allowlist, every package a later
    milestone adds started life unenforced, and the omission could not fail a test."""
    _write(tmp_path, "app/links/service.py", "async def f():\n    await q.put(e)\n")

    names = {v.split("[")[1].split("]")[0] for v in check(tmp_path)}

    assert "blocking-producer-on-hot-path" in names
