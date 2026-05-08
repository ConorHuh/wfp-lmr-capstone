"""
Build wide-format parquets the inference pipeline reads from.

In the production codebase, ingest only auto-builds parquets for datasets
that have a `parquet_bridge:` block in datasets.yaml — currently chirps and
era5 soil moisture. The model expects parquets for several other layers
(NDVI, LST, SR, JRC, WorldCover) that ingest writes only as COGs.

This script closes that gap for the local-offline branch by walking the COG
folders in MinIO and producing the missing parquets in the schema
ward_features.py expects:

  Temporal (NDVI, LST, SR_NIR, SR_SWIR1):
    columns = [lat, lon, variable, collection, YYYY-MM, YYYY-MM, ...]

  Static (JRC occurrence, WorldCover):
    columns = [lat, lon, variable, collection, value]

Run after `python -m lmr --mode ingest`. Idempotent.

Usage (inside the lmr container):
  python /app/infra/build_parquets.py [--config /app/config/datasets.yaml]

Required env vars (set by docker-compose.local.yml):
  AWS_ENDPOINT_URL, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
"""
from __future__ import annotations

import argparse
import io
import logging
import sys
import tempfile
from typing import Iterable

import boto3
import numpy as np
import pandas as pd
import rasterio

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("build_parquets")

# MODIS / JRC products store integer values that need a per-product scale_factor
# to recover physical units. The original SageMaker training pipeline applied
# these when writing parquets; the lmr ingest writes raw COGs with no scaling,
# so the inference parquets we build here must apply them or the Ridge model
# is fed values 50–10000× larger than it was trained on (and produces wildly
# out-of-range predictions even though X_ridge.shape and Ridge.coef_ look fine).
#
# Sources (per MODIS / JRC product user guides):
#   MOD13Q1 NDVI/EVI:    scale 0.0001  -> NDVI in [-1, 1], nodata = -3000
#   MOD11A2 LST_*_1km:   scale 0.02    -> Kelvin, nodata = 0
#   MOD09A1 sur_refl_b*: scale 0.0001  -> reflectance in [0, 1], nodata = -28672
#   JRC GSW occurrence:  scale 0.01    -> percentage 0-100 -> fraction 0-1, nodata = 255
#   ESA WorldCover:      no scale (categorical class map), nodata = 0
#
# nodata pixels are masked to NaN before scaling so they don't pollute the
# float-cast values.

# (dataset_name, output_stem, asset_stem, collection_label, scale_factor, nodata)
TEMPORAL_TARGETS = [
    ("modis-ndvi",    "ndvi_250m", "250m_16_days_NDVI", "MODIS-13Q1-061", 0.0001, -3000.0),
    ("modis-lst-day", "lst_day",   "LST_Day_1km",       "MODIS-11A2-061", 0.02,   0.0),
    ("modis-sr",      "sr_nir",    "sur_refl_b02",      "MODIS-09A1-061", 0.0001, -28672.0),
    ("modis-sr",      "sr_swir1",  "sur_refl_b06",      "MODIS-09A1-061", 0.0001, -28672.0),
]

# (dataset_name, output_stem, asset_stem, collection_label, scale_factor, nodata)
STATIC_TARGETS = [
    ("jrc-water",  "jrc_occurrence", "occurrence", "JRC-GSW",        0.01, 255.0),
    ("worldcover", "worldcover",     "map",        "ESA-WorldCover", 1.0,  0.0),
]

META_COLS = ["lat", "lon", "variable", "collection"]


def s3_client():
    return boto3.client("s3")


def list_date_folders(s3, bucket: str, prefix: str) -> list[str]:
    folders: set[str] = set()
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        for cp in page.get("CommonPrefixes") or []:
            name = cp["Prefix"].rstrip("/").split("/")[-1]
            if name:
                folders.add(name)
    return sorted(folders)


def read_cog_pixels(
    s3, bucket: str, key: str,
    max_pixels: int | None = None,
    scale_factor: float = 1.0,
    nodata: float | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Download a COG and return flat (lats, lons, values) arrays.

    If max_pixels is set and the native resolution exceeds it, reads at a
    downsampled output shape (rasterio handles the decimation). Used for
    static layers where native 10–30 m resolution would blow up memory.

    nodata pixels are masked to NaN before scaling (so the scale doesn't
    move the sentinel value into a valid range). scale_factor is then
    applied to recover physical units from raw integer storage.
    """
    with tempfile.NamedTemporaryFile(suffix=".tif") as tmp:
        s3.download_file(bucket, key, tmp.name)
        with rasterio.open(tmp.name) as src:
            native_h, native_w = src.height, src.width
            native_pixels = native_h * native_w
            if max_pixels and native_pixels > max_pixels:
                factor = (native_pixels / max_pixels) ** 0.5
                out_h = max(1, int(native_h / factor))
                out_w = max(1, int(native_w / factor))
                log.info("    Downsample %dx%d -> %dx%d (target %d max pixels)",
                         native_w, native_h, out_w, out_h, max_pixels)
                data = src.read(1, out_shape=(out_h, out_w)).astype("float32")
                stride_x = native_w / out_w
                stride_y = native_h / out_h
                lons = np.array([src.xy(0, int((col + 0.5) * stride_x))[0]
                                 for col in range(out_w)])
                lats = np.array([src.xy(int((row + 0.5) * stride_y), 0)[1]
                                 for row in range(out_h)])
            else:
                data = src.read(1).astype("float32")
                lons = np.array([src.xy(0, col)[0] for col in range(data.shape[1])])
                lats = np.array([src.xy(row, 0)[1] for row in range(data.shape[0])])
            # Use file's declared nodata if caller didn't override
            file_nodata = src.nodata
    effective_nodata = nodata if nodata is not None else file_nodata
    if effective_nodata is not None:
        data[data == effective_nodata] = np.nan
    if scale_factor != 1.0:
        data = data * scale_factor
    lat_2d, lon_2d = np.meshgrid(lats, lons, indexing="ij")
    return lat_2d.flatten(), lon_2d.flatten(), data.flatten()


def date_folder_to_month(date_str: str) -> str:
    """COG folders are YYYY_MM_DD; column names are YYYY-MM."""
    parts = date_str.split("_")
    if len(parts) < 2:
        return date_str
    return f"{parts[0]}-{parts[1]}"


def upload_parquet(s3, df: pd.DataFrame, bucket: str, key: str) -> None:
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    buf.seek(0)
    s3.put_object(Bucket=bucket, Key=key, Body=buf.getvalue())


def build_temporal(
    s3, ingest_bucket: str, ingest_prefix: str, parquets_bucket: str, parquets_prefix: str,
    dataset_name: str, output_stem: str, asset_stem: str, collection_label: str,
    scale_factor: float = 1.0, nodata: float | None = None,
) -> bool:
    """Build a wide-format parquet keyed by YYYY-MM month columns."""
    cog_prefix = f"{ingest_prefix}/{dataset_name}/"
    date_folders = list_date_folders(s3, ingest_bucket, cog_prefix)
    if not date_folders:
        log.warning("No date folders for %s under s3://%s/%s — skipping",
                    dataset_name, ingest_bucket, cog_prefix)
        return False

    log.info("Building %s.parquet from %d date folders (scale=%g, nodata=%s)",
             output_stem, len(date_folders), scale_factor, nodata)

    # Collapse multiple dates within the same month into mean
    month_to_values: dict[str, list[np.ndarray]] = {}
    coords: tuple[np.ndarray, np.ndarray] | None = None

    for date_str in date_folders:
        cog_key = f"{cog_prefix}{date_str}/{asset_stem}.tif"
        try:
            s3.head_object(Bucket=ingest_bucket, Key=cog_key)
        except s3.exceptions.ClientError:
            continue
        try:
            lats, lons, values = read_cog_pixels(
                s3, ingest_bucket, cog_key,
                scale_factor=scale_factor, nodata=nodata,
            )
        except Exception as e:
            log.warning("  Failed to read %s: %s", cog_key, e)
            continue

        if coords is None:
            coords = (lats, lons)
        month_col = date_folder_to_month(date_str)
        month_to_values.setdefault(month_col, []).append(values)

    if coords is None or not month_to_values:
        log.warning("No COG data read for %s — skipping", dataset_name)
        return False

    log.info("  %d unique months collected", len(month_to_values))

    df_data: dict = {
        "lat": coords[0],
        "lon": coords[1],
        "variable": output_stem,
        "collection": collection_label,
    }
    for month_col in sorted(month_to_values):
        stack = np.vstack(month_to_values[month_col])
        df_data[month_col] = np.nanmean(stack, axis=0)

    df = pd.DataFrame(df_data)

    out_key = f"{parquets_prefix}/{output_stem}.parquet"
    upload_parquet(s3, df, parquets_bucket, out_key)
    log.info("  wrote s3://%s/%s shape=%s", parquets_bucket, out_key, df.shape)
    return True


def build_static(
    s3, ingest_bucket: str, ingest_prefix: str, parquets_bucket: str, parquets_prefix: str,
    dataset_name: str, output_stem: str, asset_stem: str, collection_label: str,
    scale_factor: float = 1.0, nodata: float | None = None,
) -> bool:
    """Build a flat parquet with [lat, lon, variable, collection, value]."""
    cog_prefix = f"{ingest_prefix}/{dataset_name}/"
    date_folders = list_date_folders(s3, ingest_bucket, cog_prefix)
    if not date_folders:
        log.warning("No date folders for %s under s3://%s/%s — skipping",
                    dataset_name, ingest_bucket, cog_prefix)
        return False

    # Use most recent folder for static layers (they don't change)
    date_str = sorted(date_folders)[-1]
    cog_key = f"{cog_prefix}{date_str}/{asset_stem}.tif"

    log.info("Building %s.parquet from %s (scale=%g, nodata=%s)",
             output_stem, cog_key, scale_factor, nodata)

    # Cap static layers at ~5M pixels to keep the flat dataframe under 1 GB.
    # Model uses these features in 20km windows where 100–500 m is plenty.
    try:
        lats, lons, values = read_cog_pixels(
            s3, ingest_bucket, cog_key,
            max_pixels=5_000_000,
            scale_factor=scale_factor, nodata=nodata,
        )
    except Exception as e:
        log.error("Failed to read static COG %s: %s", cog_key, e)
        return False

    df = pd.DataFrame({
        "lat": lats,
        "lon": lons,
        "variable": output_stem,
        "collection": collection_label,
        "value": values,
    })

    out_key = f"{parquets_prefix}/{output_stem}.parquet"
    upload_parquet(s3, df, parquets_bucket, out_key)
    log.info("  wrote s3://%s/%s shape=%s", parquets_bucket, out_key, df.shape)
    return True


def main():
    ap = argparse.ArgumentParser(description="Build missing parquets for inference")
    ap.add_argument("--config", default="/app/config/datasets.yaml")
    args = ap.parse_args()

    # Late import so the script also runs outside the container against a venv
    sys.path.insert(0, "/app/src")
    from lmr.config import load_config

    config = load_config(args.config)
    ingest_bucket = config.global_.s3_bucket
    ingest_prefix = config.global_.s3_prefix
    parquets_bucket = config.inference.source_data_bucket
    parquets_prefix = config.inference.source_data_prefix

    log.info("Ingest source : s3://%s/%s", ingest_bucket, ingest_prefix)
    log.info("Parquet target: s3://%s/%s", parquets_bucket, parquets_prefix)

    s3 = s3_client()

    failures = 0
    for ds_name, stem, asset, label, scale, nodata in TEMPORAL_TARGETS:
        ok = build_temporal(s3, ingest_bucket, ingest_prefix,
                            parquets_bucket, parquets_prefix,
                            ds_name, stem, asset, label,
                            scale_factor=scale, nodata=nodata)
        if not ok:
            failures += 1

    for ds_name, stem, asset, label, scale, nodata in STATIC_TARGETS:
        ok = build_static(s3, ingest_bucket, ingest_prefix,
                          parquets_bucket, parquets_prefix,
                          ds_name, stem, asset, label,
                          scale_factor=scale, nodata=nodata)
        if not ok:
            failures += 1

    if failures:
        log.warning("%d parquet(s) failed to build", failures)
        sys.exit(1)
    log.info("All parquets built")


if __name__ == "__main__":
    main()
