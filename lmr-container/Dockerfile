FROM python:3.11-slim

# Install GDAL and rasterio system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gdal-bin \
    libgdal-dev \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy project files
COPY pyproject.toml uv.lock ./
COPY src/ src/
COPY config/ config/

# Install dependencies with uv
RUN uv sync --frozen --no-dev

ENTRYPOINT ["uv", "run", "--no-sync", "lmr"]
