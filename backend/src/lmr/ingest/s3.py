from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from lmr.common.s3 import get_s3_client
from lmr.config import AppConfig, DatasetConfig

logger = logging.getLogger("lmr")


def build_s3_key(
    template: str,
    prefix: str,
    dataset_name: str,
    date: str,
    asset: str,
) -> str:
    return template.format(
        prefix=prefix,
        dataset=dataset_name,
        date=date,
        asset=asset,
    )


def upload_file(local_path: Path, bucket: str, key: str, region: str = "us-east-1") -> str:
    s3 = get_s3_client(region)
    logger.info("Uploading %s -> s3://%s/%s", local_path, bucket, key)
    s3.upload_file(str(local_path), bucket, key)
    return f"s3://{bucket}/{key}"


def get_last_ingested_date(
    bucket: str,
    prefix: str,
    dataset_name: str,
    region: str = "us-east-1",
) -> datetime | None:
    """Check S3 for the latest ingested date for a dataset.

    Returns None if no data has been ingested yet.
    """
    s3 = get_s3_client(region)
    dataset_prefix = f"{prefix}/{dataset_name}/"

    try:
        paginator = s3.get_paginator("list_objects_v2")
    except Exception:
        logger.warning("Could not list objects in s3://%s/%s", bucket, dataset_prefix)
        return None

    # Date folders look like: ingested/ndvi-sentinel2/2026_03_01/
    dates = []
    try:
        for page in paginator.paginate(Bucket=bucket, Prefix=dataset_prefix, Delimiter="/"):
            for cp in page.get("CommonPrefixes", []):
                folder = cp["Prefix"].rstrip("/").split("/")[-1]
                try:
                    dates.append(datetime.strptime(folder, "%Y_%m_%d").replace(tzinfo=timezone.utc))
                except ValueError:
                    continue
    except Exception:
        logger.warning("Could not list objects in s3://%s/%s", bucket, dataset_prefix)
        return None

    if not dates:
        return None

    latest = max(dates)
    logger.info("Last ingested date for %s: %s", dataset_name, latest.date())
    return latest


def get_existing_dates(
    bucket: str,
    prefix: str,
    dataset_name: str,
    region: str = "us-east-1",
) -> set[str]:
    """Return the set of date strings already ingested for a dataset."""
    s3 = get_s3_client(region)
    dataset_prefix = f"{prefix}/{dataset_name}/"

    try:
        paginator = s3.get_paginator("list_objects_v2")
        dates = set()
        for page in paginator.paginate(Bucket=bucket, Prefix=dataset_prefix, Delimiter="/"):
            for cp in page.get("CommonPrefixes", []):
                folder = cp["Prefix"].rstrip("/").split("/")[-1]
                dates.add(folder)
        return dates
    except Exception:
        return set()


def write_manifest(
    bucket: str,
    datasets_results: list[dict],
    region: str = "us-east-1",
) -> str:
    """Write an ingest manifest JSON to S3.

    Args:
        bucket: S3 bucket name.
        datasets_results: List of per-dataset result dicts with keys:
            name, items_ingested, s3_keys, stac_items
        region: AWS region.

    Returns:
        S3 URI of the manifest file.
    """
    now = datetime.now(timezone.utc)
    run_id = f"ingest-{now.strftime('%Y-%m-%dT%H:%M:%SZ')}"

    # Derive overall status from per-dataset results
    total = len(datasets_results)
    succeeded = sum(1 for d in datasets_results if d.get("items_ingested", 0) > 0)
    if succeeded == 0:
        status = "failed"
    elif succeeded < total:
        status = "partial"
    else:
        status = "success"

    manifest = {
        "run_id": run_id,
        "timestamp": now.isoformat(),
        "datasets_processed": datasets_results,
        "status": status,
    }

    key = f"manifests/{run_id}.json"
    s3 = get_s3_client(region)
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(manifest, indent=2),
        ContentType="application/json",
    )

    uri = f"s3://{bucket}/{key}"
    logger.info("Wrote manifest: %s", uri)
    return uri
