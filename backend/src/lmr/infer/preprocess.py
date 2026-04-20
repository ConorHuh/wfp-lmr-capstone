"""
Inference preprocessing — step 1 of 3.

Downloads model artifacts (feature_names, train_medians, run_metadata, scaler)
from S3, loads the input ward+season parquet, imputes NaNs with training
medians, and produces Ridge-scaled and raw feature parquets.

Ported from sagemaker-pipeline/inference_preprocess.py with no SageMaker
dependencies.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Tuple

import boto3
import joblib
import numpy as np
import pandas as pd

from lmr.common.logging import setup_logging


def run_inference_preprocess(
    input_data_s3_path: str,
    model_s3_prefix: str,
    season_scheme: str,
    output_s3_base_uri: str,
) -> Tuple[str, str, str, float]:
    """
    Preprocess input ward+season data for ensemble inference.

    Parameters
    ----------
    input_data_s3_path : str
        S3 URI to pre-aggregated ward+season parquet.
    model_s3_prefix : str
        S3 prefix containing one subfolder per season scheme.
    season_scheme : str
        One of "biannual", "quadseasonal", "monthly".
    output_s3_base_uri : str
        Base S3 URI where preprocessed outputs will be written.

    Returns
    -------
    features_s3, features_ridge_s3, metadata_s3, label_mean
    """
    logger = setup_logging("INFO")

    scheme_prefix = _join_s3(model_s3_prefix, season_scheme)
    bucket, key_prefix = _parse_s3_uri(scheme_prefix)

    s3 = boto3.client("s3")

    with tempfile.TemporaryDirectory() as tmp:

        # 1. Download model artifacts
        feature_names_path = os.path.join(tmp, "feature_names.json")
        train_medians_path = os.path.join(tmp, "train_medians.json")
        run_metadata_path = os.path.join(tmp, "run_metadata.json")
        scaler_path = os.path.join(tmp, "feature_scaler.joblib")

        for filename, local_path in [
            ("feature_names.json", feature_names_path),
            ("train_medians.json", train_medians_path),
            ("run_metadata.json", run_metadata_path),
            ("feature_scaler.joblib", scaler_path),
        ]:
            s3.download_file(bucket, f"{key_prefix}/{filename}", local_path)
            logger.info("Downloaded s3://%s/%s/%s", bucket, key_prefix, filename)

        with open(feature_names_path) as f:
            feature_names = json.load(f)
        with open(train_medians_path) as f:
            train_medians = json.load(f)
        with open(run_metadata_path) as f:
            run_metadata = json.load(f)

        label_mean = float(run_metadata["label_mean"])
        logger.info("label_mean: %s, feature count: %d", label_mean, len(feature_names))

        scaler = joblib.load(scaler_path)

        # 2. Load input data
        df = pd.read_parquet(input_data_s3_path)
        logger.info("Loaded input data: %s from %s", df.shape, input_data_s3_path)

        # 3. Extract metadata columns
        meta_cols = _detect_metadata_columns(df, season_scheme)
        missing_meta = [c for c in meta_cols if c not in df.columns]
        if missing_meta:
            raise ValueError(
                f"Input parquet is missing metadata columns: {missing_meta}. "
                f"Available columns: {df.columns.tolist()}"
            )
        metadata_df = df[meta_cols].copy()

        # 4. Select and order feature columns
        missing_features = [f for f in feature_names if f not in df.columns]
        if missing_features:
            raise ValueError(
                f"Input parquet is missing {len(missing_features)} feature "
                f"column(s): {missing_features[:10]}"
                f"{'...' if len(missing_features) > 10 else ''}"
            )
        X = df[feature_names].copy()

        # Verify feature column order matches expected order exactly
        if list(X.columns) != feature_names:
            raise ValueError(
                f"Feature column order mismatch after selection: "
                f"expected {feature_names[:5]}..., got {list(X.columns)[:5]}..."
            )

        # 5. Impute NaNs using training medians
        nan_counts_before = X.isna().sum().sum()
        missing_medians = [f for f in feature_names if f not in train_medians]
        if missing_medians:
            raise ValueError(
                f"train_medians.json is missing {len(missing_medians)} feature(s) "
                f"listed in feature_names.json: {missing_medians[:10]}"
                f"{'...' if len(missing_medians) > 10 else ''}. "
                f"Model artifact bundle is inconsistent."
            )
        for feat in feature_names:
            X[feat] = X[feat].fillna(train_medians[feat])
        remaining = X.isna().sum().sum()
        if remaining > 0:
            raise ValueError(
                f"{remaining} NaN(s) remain after median imputation. "
                f"Input data likely contains non-numeric or unrecoverable values."
            )
        logger.info("Imputed %d NaN(s) using training medians", nan_counts_before)


        # 7. Write outputs to S3
        out_prefix = output_s3_base_uri.rstrip("/")
        features_s3 = f"{out_prefix}/preprocessed_features.parquet"
        features_ridge_s3 = f"{out_prefix}/preprocessed_features_ridge.parquet"
        metadata_s3 = f"{out_prefix}/inference_metadata.parquet"

        X.to_parquet(features_s3, index=False)
        X_ridge.to_parquet(features_ridge_s3, index=False)
        metadata_df.to_parquet(metadata_s3, index=False)

        logger.info("Features (raw): %s", features_s3)
        logger.info("Features (Ridge): %s", features_ridge_s3)
        logger.info("Metadata: %s", metadata_s3)

    return features_s3, features_ridge_s3, metadata_s3, label_mean


def _detect_metadata_columns(df: pd.DataFrame, season_scheme: str) -> list:
    base = ["ward_name"]
    if season_scheme == "monthly":
        time_cols = ["year", "month"]
    else:
        time_cols = ["season", "season_year"]
    return base + time_cols


def _parse_s3_uri(s3_uri: str):
    assert s3_uri.startswith("s3://"), f"Expected s3:// URI, got: {s3_uri}"
    parts = s3_uri[5:].split("/", 1)
    bucket = parts[0]
    key = parts[1] if len(parts) > 1 else ""
    return bucket, key


def _join_s3(*parts: str) -> str:
    base = parts[0].rstrip("/")
    for p in parts[1:]:
        base = base + "/" + p.strip("/")
    return base
