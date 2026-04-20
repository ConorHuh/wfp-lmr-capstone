"""
Inference pipeline orchestrator — chains preprocess → ensemble → postprocess
for a single season scheme.

Called by CLI: lmr --mode infer --scheme biannual
"""

from __future__ import annotations

import json
import os
import tempfile

import boto3

from lmr.common.logging import setup_logging
from lmr.config import AppConfig
from lmr.infer.preprocess import run_inference_preprocess
from lmr.infer.ensemble import run_inference
from lmr.infer.postprocess import run_postprocess


def run_inference_pipeline(config: AppConfig, scheme: str) -> None:
    """
    Run the full 3-step inference pipeline for one season scheme.

    1. Preprocess: impute NaNs, scale features for Ridge
    2. Ensemble: run 4 models + weighted avg (or monthly meta-learner)
    3. Postprocess: assign risk levels, write GeoJSON/GeoTIFF/CSV per timepoint
    """
    logger = setup_logging(config.global_.log_level)
    inference = config.inference
    bucket = inference.output_bucket

    logger.info("=" * 60)
    logger.info("Inference pipeline — scheme: %s", scheme)
    logger.info("Model: %s", inference.model_s3_prefix)
    logger.info("Output: s3://%s/%s", bucket, inference.output_prefix)
    logger.info("=" * 60)

    # Discover the latest ward features parquet for this scheme
    input_data_s3_path = _find_latest_ward_features(bucket, scheme, logger)
    model_s3_prefix = inference.model_s3_prefix
    output_s3_base = f"s3://{bucket}/inference/preprocessed/{scheme}"

    # Step 1: Preprocess
    logger.info("Step 1/3: Preprocessing")
    features_s3, features_ridge_s3, metadata_s3, label_mean = run_inference_preprocess(
        input_data_s3_path=input_data_s3_path,
        model_s3_prefix=model_s3_prefix,
        season_scheme=scheme,
        output_s3_base_uri=output_s3_base,
    )

    # Step 2: Ensemble inference
    logger.info("Step 2/3: Ensemble inference")
    predictions_s3 = run_inference(
        features_s3=features_s3,
        features_ridge_s3=features_ridge_s3,
        metadata_s3=metadata_s3,
        model_s3_prefix=model_s3_prefix,
        season_scheme=scheme,
        output_s3_base_uri=output_s3_base,
    )

    # Step 3: Postprocess
    logger.info("Step 3/3: Postprocessing")

    # Download ward boundaries to local temp
    s3 = boto3.client("s3")
    with tempfile.TemporaryDirectory() as tmp:
        boundaries_local = os.path.join(tmp, "boundaries.geojson")
        s3.download_file(bucket, inference.ward_boundaries_s3_key, boundaries_local)

        # Load feature names for SHAP
        scheme_prefix = f"{model_s3_prefix}/{scheme}".replace("s3://", "").split("/", 1)
        model_bucket = scheme_prefix[0]
        model_key_prefix = scheme_prefix[1]
        fn_local = os.path.join(tmp, "feature_names.json")
        s3.download_file(model_bucket, f"{model_key_prefix}/feature_names.json", fn_local)
        with open(fn_local) as f:
            feature_names = json.load(f)

        # Load XGBoost model for SHAP
        import joblib
        xgb_local = os.path.join(tmp, "xgboost_model.joblib")
        s3.download_file(model_bucket, f"{model_key_prefix}/xgboost_model.joblib", xgb_local)
        xgb_model = joblib.load(xgb_local)

        output_s3_prefix = f"s3://{bucket}/{inference.output_prefix}"

        base_dir, output_dirs = run_postprocess(
            predictions_s3_path=predictions_s3,
            admin3_shapefile_path=boundaries_local,
            feature_names=feature_names,
            output_s3_prefix=output_s3_prefix,
            training_label_mean=label_mean,
            season_scheme=scheme,
            compute_shap=True,
            xgb_model=xgb_model,
        )

    logger.info("=" * 60)
    logger.info("Inference complete for %s", scheme)
    logger.info("Outputs: %d timepoint(s) under %s", len(output_dirs), base_dir)
    logger.info("=" * 60)


def _find_latest_ward_features(bucket: str, scheme: str, logger) -> str:
    """Find the most recent ward_features_{scheme}.parquet in the inference/ prefix."""
    s3 = boto3.client("s3")
    prefix = "inference/"

    # List all ward_features directories
    paginator = s3.get_paginator("list_objects_v2")
    target_key = f"ward_features_{scheme}.parquet"
    candidates = []

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        for common_prefix in page.get("CommonPrefixes", []):
            dir_prefix = common_prefix["Prefix"]
            if "ward_features_" in dir_prefix:
                full_key = f"{dir_prefix}{target_key}"
                try:
                    s3.head_object(Bucket=bucket, Key=full_key)
                    candidates.append(full_key)
                except s3.exceptions.ClientError:
                    continue

    if not candidates:
        raise FileNotFoundError(
            f"No ward_features_{scheme}.parquet found under s3://{bucket}/{prefix}. "
            f"Run feature extraction first (--mode feature-extract)."
        )

    # Sort by name (which contains the date range) — latest is last
    candidates.sort()
    chosen = candidates[-1]
    s3_uri = f"s3://{bucket}/{chosen}"
    logger.info("Using ward features: %s", s3_uri)
    return s3_uri
