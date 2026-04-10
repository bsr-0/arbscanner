# syntax=docker/dockerfile:1.7

# ---------- Stage 1: Builder ----------
FROM python:3.12-slim-bookworm AS builder

# Install uv from the official distroless image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Install build tooling needed by some wheels (sentence-transformers, pyarrow, etc.)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        git \
    && rm -rf /var/lib/apt/lists/*

# Copy only dependency manifests first for maximal layer caching.
COPY pyproject.toml ./

# Prime the virtualenv with third-party deps only (no project source yet).
# This layer is cached until pyproject.toml changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /app/.venv \
    && uv pip install --python /app/.venv/bin/python \
        pmxt \
        sentence-transformers \
        anthropic \
        rich \
        python-dotenv \
        fastapi \
        "uvicorn[standard]" \
        jinja2 \
        httpx \
        stripe \
        pyarrow \
        pandas

# Now copy the project source and install the package itself.
COPY src/ ./src/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /app/.venv/bin/python --no-deps .


# ---------- Stage 2: Runtime ----------
FROM python:3.12-slim-bookworm AS runtime

LABEL org.opencontainers.image.source="https://github.com/arbscanner/arbscanner" \
      org.opencontainers.image.description="Cross-platform prediction market arbitrage scanner (Polymarket x Kalshi)"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/app/.venv/bin:/usr/local/bin:/usr/bin:/bin" \
    VIRTUAL_ENV=/app/.venv \
    NODE_ENV=production

# Install Node.js 20.x (required by the pmxtjs sidecar that pmxt shells out to),
# curl (used by HEALTHCHECK), plus ca-certificates for TLS.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get purge -y --auto-remove gnupg \
    && rm -rf /var/lib/apt/lists/*

# Install the pmxtjs sidecar globally so `pmxt` can locate it at runtime.
RUN npm install -g pmxtjs \
    && npm cache clean --force

# Create a non-root user with uid 1000.
RUN groupadd --system --gid 1000 arbscanner \
    && useradd --system --uid 1000 --gid 1000 --create-home --home-dir /home/arbscanner --shell /usr/sbin/nologin arbscanner

WORKDIR /app

# Copy the virtualenv from the builder stage.
COPY --from=builder --chown=arbscanner:arbscanner /app/.venv /app/.venv

# Copy project source and manifest into the runtime image so the installed
# package can locate any resource files shipped alongside it.
COPY --chown=arbscanner:arbscanner pyproject.toml ./
COPY --chown=arbscanner:arbscanner src/ ./src/

USER arbscanner:arbscanner

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl --fail --silent --show-error http://localhost:8000/api/stats || exit 1

CMD ["arbscanner", "serve", "--host", "0.0.0.0", "--port", "8000"]
