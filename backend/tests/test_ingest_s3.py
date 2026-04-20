"""Tests for ingest S3 operations: build_s3_key, pagination, manifest status."""

import json
from datetime import datetime, timezone

import boto3
import pytest
from moto import mock_aws

from lmr.ingest.s3 import (
    build_s3_key,
    get_existing_dates,
    get_last_ingested_date,
    write_manifest,
)

BUCKET = "test-bucket"
REGION = "us-east-1"


def test_build_s3_key():
    key = build_s3_key(
        template="{prefix}/{dataset}/{date}/{asset}.tif",
        prefix="ingested",
        dataset_name="modis-ndvi",
        date="2024_01_01",
        asset="250m_16_days_NDVI",
    )
    assert key == "ingested/modis-ndvi/2024_01_01/250m_16_days_NDVI.tif"


@mock_aws
def _setup_s3_with_dates(dates: list[str], prefix: str = "ingested", dataset: str = "modis-ndvi"):
    """Create an S3 bucket with date folders containing a dummy object."""
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=BUCKET)
    for date_str in dates:
        key = f"{prefix}/{dataset}/{date_str}/dummy.tif"
        s3.put_object(Bucket=BUCKET, Key=key, Body=b"data")
    return s3


@mock_aws
def test_get_last_ingested_date_returns_latest():
    _setup_s3_with_dates(["2024_01_01", "2024_06_15", "2024_03_10"])
    result = get_last_ingested_date(BUCKET, "ingested", "modis-ndvi", REGION)
    assert result == datetime(2024, 6, 15, tzinfo=timezone.utc)


@mock_aws
def test_get_last_ingested_date_empty_bucket():
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=BUCKET)
    result = get_last_ingested_date(BUCKET, "ingested", "modis-ndvi", REGION)
    assert result is None


@mock_aws
def test_get_existing_dates_returns_all():
    _setup_s3_with_dates(["2024_01_01", "2024_02_01", "2024_03_01"])
    result = get_existing_dates(BUCKET, "ingested", "modis-ndvi", REGION)
    assert result == {"2024_01_01", "2024_02_01", "2024_03_01"}


@mock_aws
def test_get_existing_dates_empty():
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=BUCKET)
    result = get_existing_dates(BUCKET, "ingested", "modis-ndvi", REGION)
    assert result == set()


@mock_aws
def test_write_manifest_success_status():
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=BUCKET)

    results = [
        {"name": "modis-ndvi", "items_ingested": 5, "s3_keys": [], "stac_items": []},
        {"name": "modis-evi", "items_ingested": 3, "s3_keys": [], "stac_items": []},
    ]
    uri = write_manifest(BUCKET, results, REGION)
    assert uri.startswith(f"s3://{BUCKET}/manifests/")

    # Read back and check status
    key = uri.replace(f"s3://{BUCKET}/", "")
    obj = s3.get_object(Bucket=BUCKET, Key=key)
    manifest = json.loads(obj["Body"].read())
    assert manifest["status"] == "success"


@mock_aws
def test_write_manifest_partial_status():
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=BUCKET)

    results = [
        {"name": "modis-ndvi", "items_ingested": 5, "s3_keys": [], "stac_items": []},
        {"name": "modis-evi", "items_ingested": 0, "s3_keys": [], "stac_items": []},
    ]
    uri = write_manifest(BUCKET, results, REGION)
    key = uri.replace(f"s3://{BUCKET}/", "")
    obj = s3.get_object(Bucket=BUCKET, Key=key)
    manifest = json.loads(obj["Body"].read())
    assert manifest["status"] == "partial"


@mock_aws
def test_write_manifest_failed_status():
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=BUCKET)

    results = [
        {"name": "modis-ndvi", "items_ingested": 0, "s3_keys": [], "stac_items": []},
        {"name": "modis-evi", "items_ingested": 0, "s3_keys": [], "stac_items": []},
    ]
    uri = write_manifest(BUCKET, results, REGION)
    key = uri.replace(f"s3://{BUCKET}/", "")
    obj = s3.get_object(Bucket=BUCKET, Key=key)
    manifest = json.loads(obj["Body"].read())
    assert manifest["status"] == "failed"
