from __future__ import annotations

from lmr.common.s3 import get_s3_client


def generate_presigned_url(bucket: str, key: str, region: str, expiry: int = 3600) -> str:
    client = get_s3_client(region)
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expiry,
    )


def list_dataset_dates(bucket: str, prefix: str, dataset_name: str, region: str) -> list[str]:
    client = get_s3_client(region)
    dataset_prefix = f"{prefix}/{dataset_name}/"
    paginator = client.get_paginator("list_objects_v2")
    dates: set[str] = set()
    for page in paginator.paginate(Bucket=bucket, Prefix=dataset_prefix, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            # Extract date folder name from "ingested/modis-ndvi/2024-01-01/"
            date_part = cp["Prefix"].rstrip("/").rsplit("/", 1)[-1]
            dates.add(date_part)
    return sorted(dates)


def resolve_s3_key(
    bucket: str, prefix: str, collection: str, date: str, asset: str
) -> tuple[str, str]:
    key = f"{prefix}/{collection}/{date}/{asset}.tif"
    return bucket, key


def s3_key_exists(bucket: str, key: str, region: str) -> bool:
    client = get_s3_client(region)
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except client.exceptions.ClientError as e:
        if e.response["Error"]["Code"] == "404":
            return False
        raise


def fetch_json_from_s3(bucket: str, key: str, region: str) -> dict | list | None:
    import json

    client = get_s3_client(region)
    try:
        response = client.get_object(Bucket=bucket, Key=key)
        return json.loads(response["Body"].read())
    except client.exceptions.NoSuchKey:
        return None
