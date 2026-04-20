"""Tests for inference preprocessing: feature validation logic."""

import json
import os
import tempfile
from unittest.mock import patch

import joblib
import numpy as np
import pandas as pd
import pytest
from sklearn.preprocessing import StandardScaler

from lmr.infer.preprocess import run_inference_preprocess


def _create_mock_artifacts(tmp_dir: str, feature_names: list[str]):
    """Create mock model artifacts in a temp directory."""
    # feature_names.json
    with open(os.path.join(tmp_dir, "feature_names.json"), "w") as f:
        json.dump(feature_names, f)

    # train_medians.json
    medians = {feat: 0.5 for feat in feature_names}
    with open(os.path.join(tmp_dir, "train_medians.json"), "w") as f:
        json.dump(medians, f)

    # run_metadata.json
    with open(os.path.join(tmp_dir, "run_metadata.json"), "w") as f:
        json.dump({"label_mean": 0.1}, f)

    # feature_scaler.joblib
    scaler = StandardScaler()
    scaler.fit(np.random.rand(10, len(feature_names)))
    joblib.dump(scaler, os.path.join(tmp_dir, "feature_scaler.joblib"))


def test_missing_features_raises_error():
    """Preprocessing should raise ValueError if input is missing required features."""
    feature_names = ["ndvi_mean", "evi_mean", "lst_day_mean"]

    with tempfile.TemporaryDirectory() as tmp:
        _create_mock_artifacts(tmp, feature_names)

        # Input data missing 'lst_day_mean'
        df = pd.DataFrame({
            "ward_name": ["Ward A", "Ward B"],
            "season": ["OND", "OND"],
            "season_year": [2024, 2024],
            "ndvi_mean": [0.5, 0.6],
            "evi_mean": [0.3, 0.4],
            # lst_day_mean is missing
        })

        input_path = os.path.join(tmp, "input.parquet")
        df.to_parquet(input_path, index=False)

        # Mock S3 download to just copy local files
        def mock_download(bucket, key, local_path):
            import shutil
            filename = key.split("/")[-1]
            src = os.path.join(tmp, filename)
            if os.path.exists(src):
                shutil.copy2(src, local_path)

        with patch("lmr.infer.preprocess.boto3") as mock_boto3:
            mock_s3 = mock_boto3.client.return_value
            mock_s3.download_file.side_effect = mock_download

            with pytest.raises(ValueError, match="missing.*feature"):
                run_inference_preprocess(
                    input_data_s3_path=input_path,
                    model_s3_prefix="s3://test-bucket/models",
                    season_scheme="biannual",
                    output_s3_base_uri=f"s3://test-bucket/output",
                )


def test_feature_order_preserved():
    """Preprocessing should select features in the exact order from feature_names.json."""
    feature_names = ["ndvi_mean", "evi_mean", "lst_day_mean"]

    with tempfile.TemporaryDirectory() as tmp:
        _create_mock_artifacts(tmp, feature_names)

        # Input data with columns in different order
        df = pd.DataFrame({
            "ward_name": ["Ward A", "Ward B"],
            "season": ["OND", "OND"],
            "season_year": [2024, 2024],
            "lst_day_mean": [30.0, 31.0],  # intentionally out of order
            "evi_mean": [0.3, 0.4],
            "ndvi_mean": [0.5, 0.6],
        })

        input_path = os.path.join(tmp, "input.parquet")
        output_dir = os.path.join(tmp, "output")
        os.makedirs(output_dir, exist_ok=True)
        df.to_parquet(input_path, index=False)

        def mock_download(bucket, key, local_path):
            import shutil
            filename = key.split("/")[-1]
            src = os.path.join(tmp, filename)
            if os.path.exists(src):
                shutil.copy2(src, local_path)

        with patch("lmr.infer.preprocess.boto3") as mock_boto3:
            mock_s3 = mock_boto3.client.return_value
            mock_s3.download_file.side_effect = mock_download

            features_s3, features_ridge_s3, metadata_s3, label_mean = run_inference_preprocess(
                input_data_s3_path=input_path,
                model_s3_prefix="s3://test-bucket/models",
                season_scheme="biannual",
                output_s3_base_uri=output_dir,
            )

            # Verify features are in correct order
            features_df = pd.read_parquet(features_s3)
            assert list(features_df.columns) == feature_names
            assert label_mean == 0.1
