FROM python:3.12-slim

# uv: fast Python package manager + venv bootstrap
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    LIVOLTEK_HEADLESS=true

WORKDIR /app

# Install Python dependencies first for layer caching.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Install Playwright Chromium + OS-level browser dependencies. This is the
# heavy layer (~400 MB) — keep it stable so changes to source don't rebuild it.
# Call the venv binary directly so uv doesn't try to install our package
# (the source hasn't been COPYed yet — that happens after, to preserve caching).
RUN .venv/bin/playwright install --with-deps chromium

# Install our package on top.
COPY src/ ./src/
COPY README.md ./
RUN uv sync --frozen --no-dev

# The production command lives HERE, versioned with the code. Railway's
# Config as Code (railway.json) stops being read on 2026-12-01, so nothing in
# the dashboard overrides this; only the cron schedule (0 18,19 * * *) is set
# in Railway -> Service -> Settings -> Cron Schedule.
# --riga-hour 21 makes every run outside 21:xx Riga exit at once, which also
# makes an unscheduled start (a deploy, a stray "Run Once") harmless.
# For a dry run, override the command with `uv run livoltek-trader`.
CMD ["uv", "run", "livoltek-trader", "--execute", "--riga-hour", "21"]
