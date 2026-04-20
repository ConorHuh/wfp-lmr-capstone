"""Parquet bridge — converts ingested COGs to wide-format parquets for ward_features.py.

After the ingest pipeline uploads COGs to S3, this module reads them back,
extracts pixel grids, and assembles the wide-format parquets that the feature
extraction pipeline (ward_features.py) expects.

Output parquet format:
  - CHIRPS:         [lat, lon, variable, collection, YYYY-MM, YYYY-MM, ...]
  - Soil moisture:  [lat, lon, variable, collection, swvl1_YYYY-MM, swvl2_YYYY-MM, ...]
"""

from __future__ import annotations

import io
import logging
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import rasterio

from lmr.common.s3 import get_s3_client
from lmr.config import AppConfig, DatasetConfig

logger = logging.getLogger("lmr")

META_COLS = ["lat", "lon", "variable", "collection"]


def update_collection_parquet(config: AppConfig, dataset: DatasetConfig) -> None:
    """Build/update wide-format parquets from ingested COGs for feature extraction.

    Reads all date folders for this dataset in S3, extracts pixel values from
    each COG, and writes one parquet per variable to the source_data_prefix
    that ward_features.py reads from.
    """
    bridge = dataset.parquet_bridge
    if bridge is None:
        return

    bucket = config.global_.s3_bucket
    prefix = config.global_.s3_prefix
    region = config.global_.region
    s3 = get_s3_client(region)

    # Target bucket/prefix for parquet output (where ward_features.py reads from)
    out_bucket = config.inference.source_data_bucket or bucket
    out_prefix = config.inference.source_data_prefix

    cog_prefix = f"{prefix}/{dataset.name}/"

    # List all date folders
    date_folders = _list_date_folders(s3, bucket, cog_prefix)
    if not date_folders:
        logger.warning("No date folders found under s3://%s/%s", bucket, cog_prefix)
        return

    logger.info(
        "Parquet bridge: building %s.parquet from %d dates",
        bridge.collection_key, len(date_folders),
    )

    # For soil moisture, all 4 variables go into one parquet with prefixed columns.
    # For CHIRPS, single variable with plain YYYY-MM columns.
    is_soil_moisture = bridge.collection_key == "soil_moisture"

    # Collect pixel data per asset
    asset_data: dict[str, dict[str, np.ndarray]] = {}  # {asset_key: {date_col: values}}
    coords = None

    for asset_key, var_name in bridge.variable_map.items():
        asset_data[asset_key] = {}

        for date_str in sorted(date_folders):
            cog_key = f"{prefix}/{dataset.name}/{date_str}/{asset_key}.tif"

            # Check if COG exists
            try:
                s3.head_object(Bucket=bucket, Key=cog_key)
            except s3.exceptions.ClientError:
                continue

            # Download COG to temp file and extract pixel grid
            with tempfile.NamedTemporaryFile(suffix=".tif") as tmp:
                s3.download_file(bucket, cog_key, tmp.name)

                with rasterio.open(tmp.name) as src:
                    data = src.read(1).astype("float32")
                    height, width = data.shape

                    # Build lat/lon arrays from transform
                    lons = np.array([src.xy(0, col)[0] for col in range(width)])
                    lats = np.array([src.xy(row, 0)[1] for row in range(height)])

            # Create coordinate grid on first successful read
            if coords is None:
                lat_2d, lon_2d = np.meshgrid(lats, lons, indexing="ij")
                coords = {"lat": lat_2d.flatten(), "lon": lon_2d.flatten()}

            # Convert date folder (YYYY_MM_DD) to YYYY-MM column name
            parts = date_str.split("_")
            month_col = f"{parts[0]}-{parts[1]}"

            # Prefix for soil moisture: swvl1_YYYY-MM
            if is_soil_moisture:
                col_name = f"{var_name}_{month_col}"
            else:
                col_name = month_col

            asset_data[asset_key][col_name] = data.flatten()

    if coords is None:
        logger.warning("No COG data read for %s — skipping parquet bridge", dataset.name)
        return

    # Assemble dataframe(s)
    if is_soil_moisture:
        # One parquet with all 4 layers, variable column distinguishes them
        dfs = []
        for asset_key, var_name in bridge.variable_map.items():
            cols = asset_data.get(asset_key, {})
            if not cols:
                continue
            df = pd.DataFrame({
                "lat": coords["lat"],
                "lon": coords["lon"],
                "variable": var_name,
                "collection": "ERA5-Land",
                **cols,
            })
            dfs.append(df)

        if not dfs:
            logger.warning("No data assembled for soil_moisture parquet")
            return
        result_df = pd.concat(dfs, ignore_index=True)
    else:
        # Single variable (e.g., CHIRPS)
        var_name = list(bridge.variable_map.values())[0]
        cols = asset_data.get(list(bridge.variable_map.keys())[0], {})
        if not cols:
            logger.warning("No data assembled for %s parquet", bridge.collection_key)
            return
        result_df = pd.DataFrame({
            "lat": coords["lat"],
            "lon": coords["lon"],
            "variable": var_name,
            "collection": dataset.collection,
            **cols,
        })

    # Sort columns: meta first, then month columns sorted
    month_cols = sorted(c for c in result_df.columns if c not in META_COLS)
    result_df = result_df[META_COLS + month_cols]

    # Try to merge with existing parquet (add new month columns)
    out_key = f"{out_prefix}/{bridge.collection_key}.parquet"
    existing_df = _load_existing_parquet(s3, out_bucket, out_key)

    if existing_df is not None:
        # Merge: keep all existing columns, add new ones
        existing_month_cols = set(existing_df.columns) - set(META_COLS)
        new_month_cols = set(month_cols) - existing_month_cols
        if new_month_cols:
            # Merge on lat/lon/variable/collection
            result_df = existing_df.merge(
                result_df[META_COLS + sorted(new_month_cols)],
                on=META_COLS,
                how="outer",
            )
            # Re-sort columns
            all_month_cols = sorted(c for c in result_df.columns if c not in META_COLS)
            result_df = result_df[META_COLS + all_month_cols]
            logger.info("Merged %d new month columns into existing parquet", len(new_month_cols))
        else:
            logger.info("No new month columns to add — parquet already up to date")
            return

    # Upload
    _upload_parquet(s3, result_df, out_bucket, out_key)
    logger.info(
        "Parquet bridge: wrote s3://%s/%s  shape=%s",
        out_bucket, out_key, result_df.shape,
    )


def _list_date_folders(s3, bucket: str, prefix: str) -> list[str]:
    """List date folder names under a prefix using pagination."""
    paginator = s3.get_paginator("list_objects_v2")
    dates = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            folder = cp["Prefix"].rstrip("/").split("/")[-1]
            dates.append(folder)
    return dates


def _load_existing_parquet(s3, bucket: str, key: str) -> pd.DataFrame | None:
    """Try to load an existing parquet from S3. Returns None if not found."""
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        return pd.read_parquet(io.BytesIO(obj["Body"].read()))
    except s3.exceptions.ClientError:
        return None
    except Exception:
        logger.warning("Could not read existing parquet at s3://%s/%s", bucket, key)
        return None


def _upload_parquet(s3, df: pd.DataFrame, bucket: str, key: str) -> None:
    """Upload a DataFrame as parquet to S3."""
    buf = io.BytesIO()
    df.to_parquet(buf, index=False, engine="pyarrow", compression="snappy")
    buf.seek(0)
    s3.put_object(Bucket=bucket, Key=key, Body=buf.getvalue(), ContentType="application/octet-stream")
