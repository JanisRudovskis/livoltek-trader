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
RUN uv run playwright install --with-deps chromium

# Install our package on top.
COPY src/ ./src/
COPY README.md ./
RUN uv sync --frozen --no-dev

# Default command is dry-run for safety. Flip to add `--execute` once the
# first cron run produces sane ntfy output and you've verified the form fill.
CMD ["uv", "run", "livoltek-trader"]
