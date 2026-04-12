"""
Ensemble inference — step 2 of 3.

Loads 4 pre-trained models (XGBoost, LightGBM, RF, Ridge) and ensemble
weights from S3. Predicts with each model and computes a weighted average.

For the monthly scheme, applies a stacked meta-learner (Ridge) on top of
base model predictions + ward encoding, per §7.3 of the LMR Technical Handoff.

Ported from sagemaker-pipeline/inference.py with monthly meta-learner added.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Dict

import boto3
import joblib
import numpy as np
import pandas as pd

from lmr.common.logging import setup_logging


MODEL_FILES = {
    "xgboost": "xgboost_model.joblib",
    "lgbm": "lgbm_model.joblib",
    "rf": "rf_model.joblib",
    "ridge": "ridge_model.joblib",
}


def run_inference(
    features_s3: str,
    features_ridge_s3: str,
    metadata_s3: str,
    model_s3_prefix: str,
    season_scheme: str,
    output_s3_base_uri: str,
) -> str:
    """
    Run weighted ensemble inference and write predictions with metadata.

    For biannual/quadseasonal: weighted average of 4 models.
    For monthly: stacked meta-learner (lgbm_pred, ridge_pred, ward_enc → Ridge meta).

    Returns S3 URI to predictions_with_metadata.parquet.
    """
    logger = setup_logging("INFO")

    scheme_prefix = _join_s3(model_s3_prefix, season_scheme)
    bucket, key_prefix = _parse_s3_uri(scheme_prefix)

    s3 = boto3.client("s3")

    with tempfile.TemporaryDirectory() as tmp:

        # 1. Load preprocessed features
        X_raw = pd.read_parquet(features_s3)
        X_ridge = pd.read_parquet(features_ridge_s3)
        metadata_df = pd.read_parquet(metadata_s3)

        logger.info(
            "Loaded features: %s, ridge: %s, metadata: %s",
            X_raw.shape, X_ridge.shape, metadata_df.shape,
        )

        # 2. Download models and ensemble weights
        weights_path = os.path.join(tmp, "ensemble_weights.json")
        s3.download_file(bucket, f"{key_prefix}/ensemble_weights.json", weights_path)
        with open(weights_path) as f:
            raw_weights: Dict[str, float] = json.load(f)
        logger.info("Raw ensemble weights: %s", raw_weights)

        models = {}
        for model_name, filename in MODEL_FILES.items():
            local_path = os.path.join(tmp, filename)
            s3.download_file(bucket, f"{key_prefix}/{filename}", local_path)
            models[model_name] = joblib.load(local_path)
            logger.info("Loaded %s", model_name)

        # 3. Predict with each model
        predictions: Dict[str, np.ndarray] = {}
        for model_name, model in models.items():
            X = X_ridge if model_name == "ridge" else X_raw
            preds = model.predict(X.values)
            predictions[model_name] = preds
            logger.info("%s: mean=%.6f, std=%.6f", model_name, preds.mean(), preds.std())

        # 4. Compute final predictions
        if season_scheme == "monthly" and _has_meta_learner(bucket, key_prefix, s3):
            ensemble_pred = _run_stacked_inference(
                predictions, metadata_df, bucket, key_prefix, s3, tmp, logger,
            )
        else:
            ensemble_pred = _run_weighted_ensemble(predictions, raw_weights, logger)

        # 5. Merge with metadata and write
        result_df = metadata_df.copy()
        result_df["prediction"] = ensemble_pred
        for model_name, preds in predictions.items():
            result_df[f"pred_{model_name}"] = preds

        out_prefix = output_s3_base_uri.rstrip("/")
        predictions_s3 = f"{out_prefix}/predictions_with_metadata.parquet"
        result_df.to_parquet(predictions_s3, index=False)

        logger.info("Predictions written to: %s (%s)", predictions_s3, result_df.shape)

    return predictions_s3


def _run_weighted_ensemble(
    predictions: Dict[str, np.ndarray],
    raw_weights: Dict[str, float],
    logger,
) -> np.ndarray:
    """Weighted average ensemble (biannual / quadseasonal)."""
    weight_sum = sum(raw_weights.values())
    if weight_sum <= 0:
        raise ValueError(f"Sum of ensemble weights is {weight_sum}; cannot normalize.")
    normalized = {k: v / weight_sum for k, v in raw_weights.items()}
    logger.info("Normalized weights: %s", normalized)

    n = len(next(iter(predictions.values())))
    ensemble_pred = np.zeros(n, dtype=np.float64)
    for model_name, weight in normalized.items():
        if weight > 0 and model_name in predictions:
            ensemble_pred += weight * predictions[model_name]

    logger.info("Ensemble: mean=%.6f, std=%.6f", ensemble_pred.mean(), ensemble_pred.std())
    return ensemble_pred


def _run_stacked_inference(
    predictions: Dict[str, np.ndarray],
    metadata_df: pd.DataFrame,
    bucket: str,
    key_prefix: str,
    s3,
    tmp: str,
    logger,
) -> np.ndarray:
    """
    Stacked meta-learner for monthly scheme.

    Step 1: base model predictions (already computed)
    Step 2: build meta-features [lgbm_pred, ridge_pred, ward_enc_value]
    Step 3: meta_model.predict(meta_scaler.transform(meta_X))
    """
    logger.info("Monthly scheme — using stacked meta-learner")

    # Download meta-learner artifacts
    meta_model_path = os.path.join(tmp, "meta_model.joblib")
    meta_scaler_path = os.path.join(tmp, "meta_scaler.joblib")
    meta_feature_names_path = os.path.join(tmp, "meta_feature_names.json")
    ward_encoding_path = os.path.join(tmp, "ward_encoding.json")

    for filename, local_path in [
        ("meta_model.joblib", meta_model_path),
        ("meta_scaler.joblib", meta_scaler_path),
        ("meta_feature_names.json", meta_feature_names_path),
        ("ward_encoding.json", ward_encoding_path),
    ]:
        s3.download_file(bucket, f"{key_prefix}/{filename}", local_path)

    meta_model = joblib.load(meta_model_path)
    meta_scaler = joblib.load(meta_scaler_path)
    with open(meta_feature_names_path) as f:
        meta_feature_names = json.load(f)
    with open(ward_encoding_path) as f:
        ward_encoding = json.load(f)

    logger.info("Meta-feature order: %s", meta_feature_names)

    # Build meta-feature matrix
    global_mean = ward_encoding.get("_global_mean", 0.0)

    # Map base model names to their predictions
    pred_map = {
        "lgbm": predictions["lgbm"],
        "ridge": predictions["ridge"],
        "xgboost": predictions["xgboost"],
        "rf": predictions["rf"],
    }

    # Ward encoding lookup
    ward_enc_values = metadata_df["ward_name"].map(
        lambda w: ward_encoding.get(w, global_mean)
    ).values

    # Build meta_X in the order specified by meta_feature_names.json
    meta_columns = {}
    for feat_name in meta_feature_names:
        if feat_name == "ward_enc":
            meta_columns[feat_name] = ward_enc_values
        elif feat_name in pred_map:
            meta_columns[feat_name] = pred_map[feat_name]
        else:
            raise ValueError(
                f"Unknown meta-feature '{feat_name}' in meta_feature_names.json. "
                f"Expected one of: lgbm, ridge, xgboost, rf, ward_enc"
            )

    meta_X = np.column_stack([meta_columns[f] for f in meta_feature_names])

    # Scale and predict
    meta_X_scaled = meta_scaler.transform(meta_X)
    final = meta_model.predict(meta_X_scaled)
    final = np.clip(final, 0, None)

    logger.info("Stacked meta-learner: mean=%.6f, std=%.6f", final.mean(), final.std())
    return final


def _has_meta_learner(bucket: str, key_prefix: str, s3) -> bool:
    """Check if meta-learner artifacts exist for this scheme."""
    try:
        s3.head_object(Bucket=bucket, Key=f"{key_prefix}/meta_model.joblib")
        return True
    except s3.exceptions.ClientError:
        return False


def _parse_s3_uri(s3_uri: str):
    assert s3_uri.startswith("s3://"), f"Expected s3:// URI, got: {s3_uri}"
    parts = s3_uri[5:].split("/", 1)
    return parts[0], parts[1] if len(parts) > 1 else ""


def _join_s3(*parts: str) -> str:
    base = parts[0].rstrip("/")
    for p in parts[1:]:
        base = base + "/" + p.strip("/")
    return base
