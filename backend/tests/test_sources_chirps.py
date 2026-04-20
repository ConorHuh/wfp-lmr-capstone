"""Tests for CHIRPS HTTP source backend."""

import gzip
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from lmr.config import AppConfig, DatasetConfig, AOIConfig, STACConfig, GlobalConfig, TemporalConfig, ProcessingConfig
from lmr.ingest.sources import SyntheticItem, _search_chirps, _download_chirps


def _make_config():
    return AppConfig(
        **{
            "global": GlobalConfig(s3_bucket="test", s3_prefix="ingested"),
            "aoi": AOIConfig(name="kenya", bbox=[33.91, -4.80, 41.91, 5.41]),
            "stac": STACConfig(catalog_url="https://example.com"),
            "datasets": [],
        }
    )


def _make_dataset():
    return DatasetConfig(
        name="chirps-rainfall",
        source="chirps_http",
        collection="chirps-v2.0",
        assets=["ppt"],
        temporal=TemporalConfig(lookback_days=90),
        processing=ProcessingConfig(resolution_m=5000, crs="EPSG:4326"),
    )


def test_search_chirps_generates_monthly_items():
    config = _make_config()
    dataset = _make_dataset()

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 3, 15, tzinfo=timezone.utc)

    items = _search_chirps(config, dataset, start, end)

    assert len(items) == 3  # Jan, Feb, Mar
    assert all(isinstance(i, SyntheticItem) for i in items)
    assert items[0].id == "chirps-2024-01"
    assert items[1].id == "chirps-2024-02"
    assert items[2].id == "chirps-2024-03"
    assert "chirps-v2.0.2024.01.tif.gz" in items[0].metadata["url"]


def test_search_chirps_single_month():
    config = _make_config()
    dataset = _make_dataset()

    start = datetime(2024, 6, 1, tzinfo=timezone.utc)
    end = datetime(2024, 6, 30, tzinfo=timezone.utc)

    items = _search_chirps(config, dataset, start, end)
    assert len(items) == 1
    assert items[0].datetime == datetime(2024, 6, 1, tzinfo=timezone.utc)


def test_search_chirps_crosses_year_boundary():
    config = _make_config()
    dataset = _make_dataset()

    start = datetime(2023, 11, 1, tzinfo=timezone.utc)
    end = datetime(2024, 2, 28, tzinfo=timezone.utc)

    items = _search_chirps(config, dataset, start, end)
    assert len(items) == 4  # Nov, Dec, Jan, Feb
    assert items[0].id == "chirps-2023-11"
    assert items[3].id == "chirps-2024-02"


def test_download_chirps_decompresses_geotiff():
    """Mock HTTP download with a gzipped synthetic GeoTIFF."""
    with tempfile.TemporaryDirectory() as tmp:
        work_dir = Path(tmp)

        # Create a small synthetic GeoTIFF
        tif_path = work_dir / "source.tif"
        data = np.random.rand(10, 10).astype("float32")
        transform = from_bounds(36.0, 0.0, 38.0, 2.0, 10, 10)
        with rasterio.open(
            tif_path, "w", driver="GTiff",
            width=10, height=10, count=1, dtype="float32",
            crs="EPSG:4326", transform=transform,
        ) as dst:
            dst.write(data, 1)

        # Gzip it
        gz_path = work_dir / "source.tif.gz"
        with open(tif_path, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
            f_out.write(f_in.read())

        # Mock urlretrieve to copy our gzipped file
        item = SyntheticItem(
            id="chirps-2024-01",
            datetime=datetime(2024, 1, 1, tzinfo=timezone.utc),
            metadata={"url": "https://example.com/chirps-v2.0.2024.01.tif.gz"},
        )

        def mock_urlretrieve(url, dest):
            import shutil
            shutil.copy2(gz_path, dest)

        with patch("urllib.request.urlretrieve", side_effect=mock_urlretrieve):
            result = _download_chirps(item, "ppt", work_dir)

        assert result.exists()
        assert result.suffix == ".tif"

        # Verify it's a valid GeoTIFF
        with rasterio.open(result) as src:
            assert src.width == 10
            assert src.height == 10
