# syntax=docker/dockerfile:1

FROM python:3.12-slim AS base

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Copy dependency files first (better layer caching — deps only rebuild if these change)
COPY pyproject.toml uv.lock ./

# Install dependencies into a project-local venv
RUN uv sync --frozen --no-dev

# Copy the rest of the source
COPY src/ ./src/

# working/ and config.toml are NOT copied — they're runtime data, mounted in at run time
# (config.toml holds secrets; working/ holds the SQLite db, tokens, and logs)

ENTRYPOINT ["uv", "run", "demo-student"]
CMD ["run"]
