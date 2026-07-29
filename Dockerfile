# syntax=docker/dockerfile:1
# Python 3.12, not the host's 3.14 — aiokafka/asyncpg wheel availability.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local

WORKDIR /srv

# ---------------------------------------------------------------------------
# deps — keyed only on the manifests, so app edits do not reinstall the tree.
# ---------------------------------------------------------------------------
FROM base AS deps

# Pinned to the version CI installs; bump with UV_VERSION in ci.yml. Installed to
# /usr/bin so it stays out of the runtime image, which copies deps' /usr/local and
# runs no uv command.
COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /usr/bin/uv

# No `uv.lock*` glob, no `|| uv sync` fallback, no `2>/dev/null`: a missing or stale
# lock must stop the build rather than resolve something CI never tested.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# ---------------------------------------------------------------------------
# runtime
# ---------------------------------------------------------------------------
FROM base AS runtime

RUN groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --no-create-home app

COPY --from=deps /usr/local /usr/local
COPY --chown=app:app app/ ./app/
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app migrations/ ./migrations/

USER app
EXPOSE 8000

# Liveness only — /readyz needs Postgres and Redis, which is the orchestrator's concern.
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status==200 else 1)"

# --factory, so importing app.main has no side effects: a module-level `app =
# create_app()` parses Settings and reconfigures the root logger on import.
CMD ["uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "8000"]
