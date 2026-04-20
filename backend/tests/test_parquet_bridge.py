"""Tests for parquet bridge — COG to wide-format parquet conversion."""

import io
import json
import tempfile
from pathlib import Path

import boto3
import numpy as np
import pandas as pd
import pytest
import rasterio
from moto import mock_aws
from rasterio.transform import from_bounds

from lmr.config import (
    AppConfig, DatasetConfig, AOIConfig, STACConfig, GlobalConfig,
    InferenceConfig, TemporalConfig, ProcessingConfig, ParquetBridgeConfig,
)
from lmr.ingest.parquet_bridge import update_collection_parquet

BUCKET = "test-bucket"
REGION = "us-east-1"


def _make_config():
    return AppConfig(
        **{
            "global": GlobalConfig(s3_bucket=BUCKET, s3_prefix="ingested", region=REGION),
            "aoi": AOIConfig(name="kenya", bbox=[33.91, -4.80, 41.91, 5.41]),
            "stac": STACConfig(catalog_url="https://example.com"),
            "datasets": [],
            "inference": InferenceConfig(
                source_data_bucket=BUCKET,
                source_data_prefix="parquets",
            ),
        }
    )


def _upload_synthetic_cog(s3, bucket, key, width=5, height=5, bbox=(36.0, 0.0, 38.0, 2.0)):
    """Create and upload a synthetic COG to mocked S3."""
    with tempfile.NamedTemporaryFile(suffix=".tif") as tmp:
        data = np.random.rand(height, width).astype("float32") * 0.5
        transform = from_bounds(*bbox, width, height)
        with rasterio.open(
            tmp.name, "w", driver="GTiff",
            width=width, height=height, count=1, dtype="float32",
            crs="EPSG:4326", transform=transform,
        ) as dst:
            dst.write(data, 1)

        s3.upload_file(tmp.name, bucket, key)
    return data


@mock_aws
def test_chirps_parquet_bridge():
    """CHIRPS COGs → wide parquet with YYYY-MM columns."""
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=BUCKET)

    # Upload 3 months of synthetic CHIRPS COGs
    for date in ["2024_01_01", "2024_02_01", "2024_03_01"]:
        _upload_synthetic_cog(s3, BUCKET, f"ingested/chirps-rainfall/{date}/ppt.tif")

    config = _make_config()
    dataset = DatasetConfig(
        name="chirps-rainfall",
        source="chirps_http",
        collection="chirps-v2.0",
        assets=["ppt"],
        temporal=TemporalConfig(lookback_days=90),
        processing=ProcessingConfig(resolution_m=5000, crs="EPSG:4326"),
        parquet_bridge=ParquetBridgeConfig(
            collection_key="chirps",
            variable_map={"ppt": "ppt"},
        ),
    )

    update_collection_parquet(config, dataset)

    # Read back the parquet
    obj = s3.get_object(Bucket=BUCKET, Key="parquets/chirps.parquet")
    df = pd.read_parquet(io.BytesIO(obj["Body"].read()))

    # Check structure
    assert "lat" in df.columns
    assert "lon" in df.columns
    assert "variable" in df.columns
    assert "collection" in df.columns
    assert "2024-01" in df.columns
    assert "2024-02" in df.columns
    assert "2024-03" in df.columns
    assert (df["variable"] == "ppt").all()
    assert (df["collection"] == "chirps-v2.0").all()

    # Month columns should be 7 chars (YYYY-MM format)
    meta = {"lat", "lon", "variable", "collection"}
    month_cols = [c for c in df.columns if c not in meta]
    assert all(len(c) == 7 for c in month_cols)


@mock_aws
def test_soil_moisture_parquet_bridge():
    """ERA5 COGs → wide parquet with swvl{N}_YYYY-MM prefixed columns."""
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=BUCKET)

    # Upload 2 months × 4 layers
    for date in ["2024_01_01", "2024_02_01"]:
        for layer in ["swvl1", "swvl2", "swvl3", "swvl4"]:
            _upload_synthetic_cog(s3, BUCKET, f"ingested/era5-soil-moisture/{date}/{layer}.tif")

    config = _make_config()
    dataset = DatasetConfig(
        name="era5-soil-moisture",
        source="copernicus_cds",
        collection="reanalysis-era5-land-monthly-means",
        assets=["swvl1", "swvl2", "swvl3", "swvl4"],
        temporal=TemporalConfig(lookback_days=90),
        processing=ProcessingConfig(resolution_m=9000, crs="EPSG:4326"),
        parquet_bridge=ParquetBridgeConfig(
            collection_key="soil_moisture",
            variable_map={
                "swvl1": "swvl1",
                "swvl2": "swvl2",
                "swvl3": "swvl3",
                "swvl4": "swvl4",
            },
        ),
    )

    update_collection_parquet(config, dataset)

    # Read back
    obj = s3.get_object(Bucket=BUCKET, Key="parquets/soil_moisture.parquet")
    df = pd.read_parquet(io.BytesIO(obj["Body"].read()))

    # Check 4 variable values present
    assert set(df["variable"].unique()) == {"swvl1", "swvl2", "swvl3", "swvl4"}

    # Check prefixed columns exist
    meta = {"lat", "lon", "variable", "collection"}
    month_cols = [c for c in df.columns if c not in meta]
    # Should have swvl1_2024-01, swvl1_2024-02, swvl2_2024-01, etc.
    expected_cols = {
        f"{layer}_{date}"
        for layer in ["swvl1", "swvl2", "swvl3", "swvl4"]
        for date in ["2024-01", "2024-02"]
    }
    assert set(month_cols) == expected_cols


@mock_aws
def test_parquet_bridge_incremental_update():
    """New months should be merged into an existing parquet."""
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=BUCKET)

    config = _make_config()
    dataset = DatasetConfig(
        name="chirps-rainfall",
        source="chirps_http",
        collection="chirps-v2.0",
        assets=["ppt"],
        temporal=TemporalConfig(lookback_days=90),
        processing=ProcessingConfig(resolution_m=5000, crs="EPSG:4326"),
        parquet_bridge=ParquetBridgeConfig(
            collection_key="chirps",
            variable_map={"ppt": "ppt"},
        ),
    )

    # First run: 2 months
    for date in ["2024_01_01", "2024_02_01"]:
        _upload_synthetic_cog(s3, BUCKET, f"ingested/chirps-rainfall/{date}/ppt.tif")
    update_collection_parquet(config, dataset)

    # Second run: add 1 more month
    _upload_synthetic_cog(s3, BUCKET, "ingested/chirps-rainfall/2024_03_01/ppt.tif")
    update_collection_parquet(config, dataset)

    # Read back — should have all 3 months
    obj = s3.get_object(Bucket=BUCKET, Key="parquets/chirps.parquet")
    df = pd.read_parquet(io.BytesIO(obj["Body"].read()))

    meta = {"lat", "lon", "variable", "collection"}
    month_cols = [c for c in df.columns if c not in meta]
    assert "2024-01" in month_cols
    assert "2024-02" in month_cols
    assert "2024-03" in month_cols
