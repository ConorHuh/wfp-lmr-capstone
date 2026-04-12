from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from lmr.serve.app import create_app

CONFIG_PATH = str(Path(__file__).parent.parent / "config" / "datasets.yaml")


@pytest.fixture
def client():
    app = create_app(CONFIG_PATH)
    return TestClient(app)


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_raster_geotiff_not_found(client):
    with patch("lmr.serve.routes.s3_key_exists", return_value=False):
        response = client.get(
            "/raster_geotiff",
            params={"collection": "ndvi-sentinel2", "date": "2024-01-01", "asset": "B04"},
        )
        assert response.status_code == 404


def test_raster_geotiff_found(client):
    with (
        patch("lmr.serve.routes.s3_key_exists", return_value=True),
        patch(
            "lmr.serve.routes.generate_presigned_url",
            return_value="https://s3.amazonaws.com/signed-url",
        ),
    ):
        response = client.get(
            "/raster_geotiff",
            params={"collection": "ndvi-sentinel2", "date": "2024-01-01", "asset": "B04"},
        )
        assert response.status_code == 200
        data = response.json()
        assert "url" in data
        assert data["bucket"] == "lmr-data-cogs-dev"
        assert "ndvi-sentinel2" in data["key"]


def test_collections_endpoint(client):
    with patch("lmr.serve.routes.list_dataset_dates", return_value=["2024-01-01", "2024-01-09"]):
        response = client.get("/collections")
        assert response.status_code == 200
        data = response.json()
        assert "collections" in data
        names = {c["name"] for c in data["collections"]}
        # Only enabled datasets should appear
        assert "modis-ndvi" in names
        assert "modis-evi" in names
        # Disabled datasets should not appear
        assert "s2-red" not in names
        for collection in data["collections"]:
            assert collection["count"] == 2


def test_latest_no_predictions(client):
    with patch("lmr.serve.routes.list_dataset_dates", return_value=[]):
        response = client.get("/latest", params={"model": "livestock-mortality"})
        assert response.status_code == 404


def test_latest_with_predictions(client):
    with (
        patch("lmr.serve.routes.list_dataset_dates", return_value=["2024-01-01", "2024-02-01"]),
        patch("lmr.serve.routes.s3_key_exists", return_value=True),
        patch(
            "lmr.serve.routes.generate_presigned_url",
            return_value="https://s3.amazonaws.com/signed-url",
        ),
    ):
        response = client.get("/latest", params={"model": "livestock-mortality"})
        assert response.status_code == 200
        data = response.json()
        assert data["date"] == "2024-02-01"
        assert data["model"] == "livestock-mortality"


def test_tile_url(client):
    response = client.get(
        "/tile_url",
        params={"collection": "ndvi-sentinel2", "date": "2024-01-01", "asset": "B04"},
    )
    assert response.status_code == 200
    data = response.json()
    assert "tile_url" in data
    assert "{z}" in data["tile_url"]
    assert "s3://lmr-data-cogs-dev/" in data["s3_url"]


def test_titiler_routes_mounted(client):
    # Verify TiTiler COG routes are mounted under /cog
    response = client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    cog_paths = [p for p in paths if p.startswith("/cog")]
    assert len(cog_paths) > 0, "TiTiler COG routes should be mounted at /cog"


def test_cors_headers(client):
    response = client.options(
        "/health",
        headers={"Origin": "http://example.com", "Access-Control-Request-Method": "GET"},
    )
    # CORS middleware should respond to preflight
    assert response.status_code == 200
    assert "access-control-allow-origin" in response.headers
