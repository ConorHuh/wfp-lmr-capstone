# ── Stage 1: build ──────────────────────────────────────────────
FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    gdal-bin \
    libgdal-dev \
    libhdf4-alt-dev \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src/ src/
COPY config/ config/

RUN uv sync --frozen --no-dev

# ── Stage 2: runtime ───────────────────────────────────────────
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    gdal-bin \
    libgdal-dev \
    libhdf4-alt-dev \
    && rm -rf /var/lib/apt/lists/*

# GDAL environment variables for optimal S3 COG performance (TiTiler)
ENV GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR \
    VSI_CACHE=TRUE \
    VSI_CACHE_SIZE=5000000 \
    GDAL_HTTP_MERGE_CONSECUTIVE_RANGES=YES \
    GDAL_HTTP_MULTIPLEX=YES

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY --from=builder /app /app

ENTRYPOINT ["uv", "run", "--no-sync", "lmr"]
