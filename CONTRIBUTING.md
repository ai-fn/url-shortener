# Contributing

Thanks for considering a contribution! This project welcomes bug reports, feature requests, documentation improvements, and code — all contributions are appreciated. See the [README](./README.md) for a project overview and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full system design.

## Ways to contribute

- **Bug reports** — found something broken? Open an issue.
- **Feature requests** — have an idea? Open an issue to discuss it before submitting a large PR.
- **Code** — bug fixes, new features, performance improvements.
- **Docs** — typo fixes, clarifications, missing examples.
- **Triage** — helping label issues, reproduce bugs, or review open PRs is just as valuable as code.

## Getting started

```bash
git clone https://github.com/ai-fn/url-shortener.git
cd url-shortener

uv sync                     # installs deps against the pinned Python 3.12
cp .env.example .env        # host-side config: pytest, alembic, scripts
docker compose up -d --wait # Postgres, Redis, Kafka, ClickHouse

curl -i localhost:8000/healthz   # liveness
curl -i localhost:8000/readyz    # readiness — Postgres + Redis
```

Notes:

- The compose stack does **not** read `.env` — every value the `api` container uses is set directly in `docker-compose.yml`. Editing `.env` and restarting compose changes nothing there; `.env` only affects host-side runs (pytest, Alembic, scripts).
- `SECRET_KEY` and `IP_HASH_KEY` in `.env` must be ≥32 characters. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

## Branching & workflow

- Fork the repo (or branch directly if you have write access).
- Branch naming: `feature/<short-description>`, `fix/<short-description>`, `docs/<short-description>`.
- Keep PRs focused — one logical change per PR is easier to review and merge.
- Rebase or merge `main` into your branch before opening a PR if it's fallen behind.

## Code style

- `uv run ruff check .` and `uv run ruff format .` — lint and format; CI enforces both (`ruff format --check`).
- `uv run mypy app/` — type check in strict mode.
- `uv run python scripts/check_invariants.py` — the architectural gate. It statically checks the structural rules that would otherwise fail silently (e.g. nothing on the redirect path may await Kafka, redirects must stay 302, `GET /{code}` must stay auth-free). Run it before any PR touching `app/api/redirect.py`, `app/cache/`, or `app/events/` — each violation message explains the rule inline.
- Business logic belongs in `app/services/`, not in route handlers — keep routes thin.
- Follow existing patterns where the linters don't cover a case.

## Testing

- Add or update tests for any new functionality or bug fix.
- `uv run pytest -m "not integration"` — unit tests, no containers, must stay fast (under ~10s total).
- `uv run pytest -m integration` — integration tests; needs the compose stack up (`docker compose up -d`).
- Three behaviors are always covered because nothing else catches their failure: a duplicate click event must not inflate ClickHouse rollups, a malformed Kafka message must land in `clicks_dlq` without wedging the consumer, and redirects must keep working with the Kafka broker stopped. Don't remove or weaken these.
- CI (`.github/workflows/ci.yml`) runs three jobs — lint, test (unit + integration), and build (full compose boot + smoke test). All three must pass before a PR merges.

## Submitting a pull request

1. Open a PR against `main`.
2. Describe **what** changed and **why**.
3. Link any related issue(s) with `Closes #123` or `Relates to #123`.
4. If the change touches one of the structural rules enforced by `scripts/check_invariants.py`, call that out explicitly in the description — those get extra review care.
5. A maintainer may request changes — that's normal, not a rejection. A PR is ready to merge once it has the required approval and passing CI.

## Reporting issues

When filing a bug report, please include:

- **Steps to reproduce**
- **Expected behavior**
- **Actual behavior**
- **Environment** (OS, Python version, relevant config)

Feature requests should describe the problem you're trying to solve, not just the solution you have in mind — it makes discussion easier.

## Code of Conduct

There's no formal Code of Conduct document yet — be respectful and constructive in issues, PRs, and reviews.

## Questions

Not sure where to start, or have a question that isn't a bug or feature request? Open an issue.
