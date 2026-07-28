#!/usr/bin/env python3
"""Enforce the invariants that fail silently rather than loudly.

Each rule carries its own rationale in its `message`, so a CI failure explains itself.

Usage:  python scripts/check_invariants.py [root]
Exit:   0 clean, 1 violations found.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

# Nothing to enforce, or contents that legitimately *discuss* the forbidden patterns.
SKIP_DIRS = {
    ".git",
    ".venv",
    ".ruff_cache",
    ".mypy_cache",
    ".pytest_cache",
    "__pycache__",
    "node_modules",
    "docs",
    ".claude",
    ".github",
}

# All of `app/` minus a named exemption, not an allowlist of the packages that exist
# today: an omission is silent by construction, so new code is enforced by default and
# a blind spot has to be written down to exist.
HOT_PATH = ("app/",)

# The drain task is the one place where awaiting `send()` is correct: it consumes the
# queue the request path feeds. Any further exemption needs its own test.
HOT_PATH_EXEMPT = ("app/events/",)


@dataclass(frozen=True)
class Rule:
    name: str
    prefixes: tuple[str, ...]
    suffixes: tuple[str, ...]
    pattern: re.Pattern[str]
    message: str
    exempt_prefixes: tuple[str, ...] = ()


RULES: tuple[Rule, ...] = (
    Rule(
        name="rollup-not-idempotent",
        prefixes=("clickhouse/",),
        suffixes=(".sql",),
        # countIfState over-counts on redelivery the same way as the bare name.
        pattern=re.compile(r"\bcount(?:If)?State\s*\(", re.IGNORECASE),
        message=(
            "countState()/countIfState() in a rollup over-counts permanently. The Kafka engine is "
            "at-least-once, MVs fire per inserted block, and ReplacingMergeTree dedup "
            "only happens later during merges — nothing rewinds the aggregate. "
            "Use uniqState(event_id)."
        ),
    ),
    Rule(
        name="server-assigned-event-timestamp",
        prefixes=("clickhouse/",),
        suffixes=(".sql",),
        # Type args matched as a unit so the real form `ts DateTime64(3, 'UTC')` is
        # reached, while a legitimate DEFAULT now() on DLQ metadata is not. MATERIALIZED
        # now64() and the Kafka `_timestamp` column are the same defect, other spellings.
        pattern=re.compile(
            r"\bts\s+DateTime\d*\s*(?:\([^)]*\))?\s*(?:DEFAULT|MATERIALIZED)\s+now"
            r"|\b_timestamp\b",
            re.IGNORECASE,
        ),
        message=(
            "DEFAULT now() on the event timestamp breaks deduplication. Dedup keys on "
            "the full ORDER BY tuple including ts, so a server-assigned timestamp "
            "differs between deliveries of the same event. Assign event_id and ts once, "
            "in the redirect handler."
        ),
    ),
    Rule(
        name="select-star-in-materialized-view",
        prefixes=("clickhouse/",),
        suffixes=(".sql",),
        # Bounded at `;` so a backfill INSERT ... SELECT * later in the file is not
        # matched. `SELECT q.*` carries the identical hazard.
        pattern=re.compile(
            r"CREATE\s+MATERIALIZED\s+VIEW[^;]*?\bSELECT\s+(?:\w+\s*\.\s*)?\*", re.IGNORECASE
        ),
        message=(
            "SELECT * in a materialized view misaligns data silently. The MV's SELECT "
            "is frozen at creation; once column order drifts, values land in the wrong "
            "columns with no error. Enumerate columns explicitly."
        ),
    ),
    Rule(
        name="blocking-producer-on-hot-path",
        prefixes=HOT_PATH,
        exempt_prefixes=HOT_PATH_EXEMPT,
        suffixes=(".py",),
        # `send\w*`, the whole family: this rule has missed a spelling three times, each
        # time because it pinned an exact call. Any attribute, across a line break (ruff
        # wraps long calls), plus `await <queue>.put(` — but not the prescribed
        # `put_nowait`.
        pattern=re.compile(
            r"await\s+[\w.\[\]()\s]*?\.\s*send\w*\s*\(|await\s+[\w.\[\]()\s]*?\.\s*put\s*\(",
            re.IGNORECASE,
        ),
        message=(
            "Nothing on the redirect path may await Kafka or a queue. aiokafka's send() "
            "awaits buffer space and metadata; with the broker down it raises only after "
            "request_timeout_ms (~40s), inside the handler. A bounded queue's put() "
            "blocks once full, which is worse. Use click_q.put_nowait(event) and count "
            "QueueFull as a drop. The drain task in app/events/ is the exemption."
        ),
    ),
    Rule(
        name="permanent-redirect",
        prefixes=HOT_PATH,
        exempt_prefixes=HOT_PATH_EXEMPT,
        suffixes=(".py",),
        # 308 too — browsers cache either the same way. `\b30[18]\b` also covers the
        # positional form `RedirectResponse(url, 301)`.
        pattern=re.compile(
            r"HTTP_30[18]\w*|MOVED_PERMANENTLY|PERMANENT_REDIRECT"
            r"|status(?:_code)?\s*=\s*30[18]\b"
            r"|RedirectResponse\([^)]*\b30[18]\b",
            re.IGNORECASE,
        ),
        message=(
            "301/308 destroys analytics. Browsers cache a permanent redirect effectively "
            "forever: no further click events, and the destination can never change. "
            "Use 302 with Cache-Control: no-store."
        ),
    ),
)


def iter_files(root: Path) -> Iterator[tuple[Path, str]]:
    """Yield (absolute path, root-relative posix path) for every file worth scanning.

    Skipping is decided on the path *relative to root*: a checkout under `~/docs/` must
    not skip its own tree. `scripts/` is deliberately not in SKIP_DIRS. Pruned during
    the walk, not filtered after it — `rglob("*")` descends .venv in full (~980ms vs
    ~8ms) on every pre-commit run.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        # In-place, so os.walk does not descend. Rebinding would not work.
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        base = Path(dirpath)
        for filename in filenames:
            path = base / filename
            yield path, path.relative_to(root).as_posix()


def check(root: Path) -> list[str]:
    violations: list[str] = []

    for path, rel in iter_files(root):
        applicable = [
            rule
            for rule in RULES
            if rel.endswith(rule.suffixes)
            and any(rel.startswith(p) for p in rule.prefixes)
            and not any(rel.startswith(p) for p in rule.exempt_prefixes)
        ]
        if not applicable:
            continue

        # Read once per file, not once per matching rule.
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        for rule in applicable:
            for match in rule.pattern.finditer(content):
                line = content[: match.start()].count("\n") + 1
                violations.append(f"{rel}:{line}  [{rule.name}]\n    {rule.message}")

    return violations


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else ".").resolve()
    violations = check(root)

    if not violations:
        print(f"check_invariants: OK ({len(RULES)} rules)")
        return 0

    print(f"check_invariants: {len(violations)} violation(s)\n", file=sys.stderr)
    for v in violations:
        print(v + "\n", file=sys.stderr)
    print(
        "These are silent-failure invariants. If a change is deliberate, change the "
        "rule in this file in the same commit and say why in the commit message.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
