from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import boto3
import geopandas as gpd
import joblib
import numpy as np
import pandas as pd
import rasterio
from rasterio.features import rasterize
from rasterio.transform import from_bounds
import xgboost as xgb

from lmr.common.logging import setup_logging
from lmr.common.s3 import get_s3_client
from lmr.config import AppConfig


def get_ssm_parameter(ssm_prefix: str, name: str, region: str) -> str:
    ssm = boto3.client("ssm", region_name=region)
    param = ssm.get_parameter(Name=f"{ssm_prefix}/{name}")
    return param["Parameter"]["Value"]


def download_s3_uri(s3_uri: str, local_path: Path, region: str) -> Path:
    parts = s3_uri.replace("s3://", "").split("/", 1)
    bucket, key = parts[0], parts[1]
    client = get_s3_client(region)
    client.download_file(bucket, key, str(local_path))
    return local_path


def run_inference(config: AppConfig, config_dir: Path) -> str | None:
    logger = setup_logging(config.global_.log_level)
    region = config.global_.region
    inference = config.inference
    ssm_prefix = inference.ssm_prefix

    logger.info("Starting inference for model: %s", inference.model_name)

    # Fetch model artifact paths from SSM Parameter Store
    artifact_uri = get_ssm_parameter(ssm_prefix, "artifact-s3-uri", region)
    scaler_uri = get_ssm_parameter(ssm_prefix, "scaler-s3-uri", region)
    medians_uri = get_ssm_parameter(ssm_prefix, "medians-s3-uri", region)
    features_uri = get_ssm_parameter(ssm_prefix, "features-s3-uri", region)

    logger.info("Model artifact: %s", artifact_uri)

    with tempfile.TemporaryDirectory() as work_dir:
        work_path = Path(work_dir)

        # Download all artifacts
        model_path = download_s3_uri(artifact_uri, work_path / "model.xgb", region)
        scaler_path = download_s3_uri(scaler_uri, work_path / "scaler.joblib", region)
        medians_path = download_s3_uri(medians_uri, work_path / "medians.json", region)
        features_path = download_s3_uri(features_uri, work_path / "features.parquet", region)

        # Load model and preprocessing artifacts
        model = xgb.Booster()
        model.load_model(str(model_path))
        scaler = joblib.load(scaler_path)
        with open(medians_path) as f:
            medians = json.load(f)
        features_df = pd.read_parquet(features_path)

        logger.info("Loaded features: %d rows, %d columns", *features_df.shape)

        # Apply median imputation for missing values
        for col, median_val in medians.items():
            if col in features_df.columns:
                features_df[col] = features_df[col].fillna(median_val)

        # Identify feature columns (exclude non-feature columns)
        non_feature_cols = {"pcode", "ward", "date", "geometry", "target", "label"}
        feature_cols = [c for c in features_df.columns if c not in non_feature_cols]

        # Scale features
        features_scaled = scaler.transform(features_df[feature_cols])

        # Predict
        dmatrix = xgb.DMatrix(features_scaled, feature_names=feature_cols)
        predictions = model.predict(dmatrix)
        features_df["prediction"] = predictions

        logger.info("Predictions complete: min=%.3f, max=%.3f", predictions.min(), predictions.max())

        # Rasterize ward-level predictions into a COG
        boundary_path = config_dir / inference.boundary_file
        wards_gdf = gpd.read_file(boundary_path)

        # Merge predictions onto ward geometries
        # Use the latest prediction per ward if multiple dates
        if "pcode" in features_df.columns:
            latest = features_df.sort_values("date").groupby("pcode").last().reset_index()
            wards_gdf = wards_gdf.merge(latest[["pcode", "prediction"]], on="pcode", how="left")
        else:
            wards_gdf["prediction"] = np.nan

        wards_gdf["prediction"] = wards_gdf["prediction"].fillna(0.0)

        # Create output COG
        bounds = wards_gdf.total_bounds  # [minx, miny, maxx, maxy]
        res = 0.01  # ~1km resolution
        width = int((bounds[2] - bounds[0]) / res)
        height = int((bounds[3] - bounds[1]) / res)
        transform = from_bounds(bounds[0], bounds[1], bounds[2], bounds[3], width, height)

        shapes = list(zip(wards_gdf.geometry, wards_gdf["prediction"]))
        raster = rasterize(
            shapes,
            out_shape=(height, width),
            transform=transform,
            fill=np.nan,
            dtype="float32",
        )

        cog_path = work_path / "prediction.tif"
        with rasterio.open(
            cog_path,
            "w",
            driver="GTiff",
            height=height,
            width=width,
            count=1,
            dtype="float32",
            crs="EPSG:4326",
            transform=transform,
        ) as dst:
            dst.write(raster, 1)

        # Upload to S3
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        s3_key = f"{inference.output_prefix}/{today}/prediction.tif"
        client = get_s3_client(region)
        client.upload_file(str(cog_path), inference.output_bucket, s3_key)

        output_uri = f"s3://{inference.output_bucket}/{s3_key}"
        logger.info("Prediction COG uploaded: %s", output_uri)
        return output_uri
