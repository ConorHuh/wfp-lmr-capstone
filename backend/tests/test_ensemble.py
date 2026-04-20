"""
Tests for lmr.infer.ensemble — weighted ensemble and monthly stacked meta-learner.
"""

import json
import os
import tempfile

import joblib
import numpy as np
import pandas as pd
import pytest
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler

from lmr.infer.ensemble import _run_weighted_ensemble, _run_stacked_inference


class _MockLogger:
    def info(self, *a, **kw): pass
    def warning(self, *a, **kw): pass


logger = _MockLogger()


# ── Weighted ensemble tests ──────────────────────────────────────────────────


class TestWeightedEnsemble:
    def test_uniform_weights(self):
        """Equal weights → simple average."""
        preds = {
            "xgboost": np.array([0.1, 0.2, 0.3]),
            "lgbm": np.array([0.2, 0.3, 0.4]),
            "rf": np.array([0.3, 0.4, 0.5]),
            "ridge": np.array([0.4, 0.5, 0.6]),
        }
        weights = {"xgboost": 1.0, "lgbm": 1.0, "rf": 1.0, "ridge": 1.0}
        result = _run_weighted_ensemble(preds, weights, logger)
        expected = np.array([0.25, 0.35, 0.45])
        np.testing.assert_allclose(result, expected, atol=1e-10)

    def test_single_model_weight(self):
        """Only one model has weight → output equals that model."""
        preds = {
            "xgboost": np.array([0.5, 0.6]),
            "lgbm": np.array([0.1, 0.1]),
            "rf": np.array([0.9, 0.9]),
            "ridge": np.array([0.0, 0.0]),
        }
        weights = {"xgboost": 0.0, "lgbm": 0.0, "rf": 1.0, "ridge": 0.0}
        result = _run_weighted_ensemble(preds, weights, logger)
        np.testing.assert_allclose(result, np.array([0.9, 0.9]))

    def test_weight_normalization(self):
        """Weights that don't sum to 1 are normalized."""
        preds = {
            "xgboost": np.array([1.0]),
            "lgbm": np.array([0.0]),
            "rf": np.array([0.0]),
            "ridge": np.array([0.0]),
        }
        weights = {"xgboost": 2.0, "lgbm": 2.0, "rf": 0.0, "ridge": 0.0}
        result = _run_weighted_ensemble(preds, weights, logger)
        # xgb gets 0.5 weight, lgbm gets 0.5 weight → 0.5*1.0 + 0.5*0.0 = 0.5
        np.testing.assert_allclose(result, np.array([0.5]))

    def test_zero_total_weight_raises(self):
        preds = {"xgboost": np.array([0.5])}
        weights = {"xgboost": 0.0, "lgbm": 0.0, "rf": 0.0, "ridge": 0.0}
        with pytest.raises(ValueError, match="Sum of ensemble weights"):
            _run_weighted_ensemble(preds, weights, logger)

    def test_realistic_weights(self):
        """Weights from the actual biannual model."""
        preds = {
            "xgboost": np.array([0.12, 0.08, 0.15]),
            "lgbm": np.array([0.11, 0.09, 0.14]),
            "rf": np.array([0.10, 0.07, 0.13]),
            "ridge": np.array([0.09, 0.06, 0.12]),
        }
        weights = {"xgboost": 0.4, "lgbm": 0.3, "rf": 0.2, "ridge": 0.1}
        result = _run_weighted_ensemble(preds, weights, logger)
        # Manual: (0.4*0.12 + 0.3*0.11 + 0.2*0.10 + 0.1*0.09) = 0.110
        assert result.shape == (3,)
        np.testing.assert_allclose(result[0], 0.110, atol=1e-10)


# ── Stacked meta-learner tests ──────────────────────────────────────────────


class TestStackedInference:
    @pytest.fixture
    def meta_artifacts(self):
        """Create realistic meta-learner artifacts in a temp dir and mock S3.

        Yields (artifacts_dir, download_dir, model, scaler, ward_encoding).
        artifacts_dir has the source files; download_dir is where
        _run_stacked_inference writes downloaded copies.
        """
        with tempfile.TemporaryDirectory() as artifacts_dir, \
             tempfile.TemporaryDirectory() as download_dir:
            # Train a simple Ridge meta-learner on 3 features
            np.random.seed(42)
            X_train = np.random.rand(50, 3)
            y_train = X_train @ np.array([0.3, 0.5, 0.2]) + 0.01

            scaler = StandardScaler()
            X_scaled = scaler.fit_transform(X_train)
            model = Ridge(alpha=1.0)
            model.fit(X_scaled, y_train)

            joblib.dump(model, os.path.join(artifacts_dir, "meta_model.joblib"))
            joblib.dump(scaler, os.path.join(artifacts_dir, "meta_scaler.joblib"))

            with open(os.path.join(artifacts_dir, "meta_feature_names.json"), "w") as f:
                json.dump(["lgbm", "ridge", "ward_enc"], f)

            ward_encoding = {
                "Dukana": 0.15,
                "Turbi": 0.10,
                "Karare": 0.08,
                "_global_mean": 0.11,
            }
            with open(os.path.join(artifacts_dir, "ward_encoding.json"), "w") as f:
                json.dump(ward_encoding, f)

            yield artifacts_dir, download_dir, model, scaler, ward_encoding

    def _mock_s3(self, artifacts_dir):
        """Return a mock S3 client that copies from artifacts_dir."""
        class MockS3:
            def download_file(self, bucket, key, local_path):
                import shutil
                src = os.path.join(artifacts_dir, key.split("/")[-1])
                shutil.copy2(src, local_path)
        return MockS3()

    def test_stacked_inference_produces_correct_shape(self, meta_artifacts):
        artifacts_dir, download_dir, model, scaler, ward_encoding = meta_artifacts

        predictions = {
            "xgboost": np.array([0.12, 0.08, 0.15]),
            "lgbm": np.array([0.11, 0.09, 0.14]),
            "rf": np.array([0.10, 0.07, 0.13]),
            "ridge": np.array([0.09, 0.06, 0.12]),
        }
        metadata_df = pd.DataFrame({
            "ward_name": ["Dukana", "Turbi", "Karare"],
            "year": [2020, 2020, 2020],
            "month": [1, 1, 1],
        })

        result = _run_stacked_inference(
            predictions, metadata_df,
            bucket="test-bucket",
            key_prefix="models/monthly",
            s3=self._mock_s3(artifacts_dir),
            tmp=download_dir,
            logger=logger,
        )

        assert result.shape == (3,)
        assert all(result >= 0), "Predictions should be clipped to >= 0"

    def test_stacked_inference_matches_manual_computation(self, meta_artifacts):
        """Verify the stacking produces the same result as manual computation."""
        artifacts_dir, download_dir, model, scaler, ward_encoding = meta_artifacts

        lgbm_preds = np.array([0.11, 0.09])
        ridge_preds = np.array([0.09, 0.06])
        ward_names = ["Dukana", "Turbi"]

        predictions = {
            "xgboost": np.array([0.12, 0.08]),
            "lgbm": lgbm_preds,
            "rf": np.array([0.10, 0.07]),
            "ridge": ridge_preds,
        }
        metadata_df = pd.DataFrame({
            "ward_name": ward_names,
            "year": [2020, 2020],
            "month": [1, 1],
        })

        result = _run_stacked_inference(
            predictions, metadata_df,
            bucket="b", key_prefix="p",
            s3=self._mock_s3(artifacts_dir),
            tmp=download_dir, logger=logger,
        )

        # Manual computation: meta_X = [lgbm, ridge, ward_enc]
        ward_enc = np.array([ward_encoding["Dukana"], ward_encoding["Turbi"]])
        meta_X = np.column_stack([lgbm_preds, ridge_preds, ward_enc])
        meta_X_scaled = scaler.transform(meta_X)
        expected = np.clip(model.predict(meta_X_scaled), 0, None)

        np.testing.assert_allclose(result, expected, atol=1e-10)

    def test_unseen_ward_uses_global_mean(self, meta_artifacts):
        """Wards not in ward_encoding fall back to _global_mean."""
        artifacts_dir, download_dir, model, scaler, ward_encoding = meta_artifacts

        predictions = {
            "xgboost": np.array([0.1]),
            "lgbm": np.array([0.1]),
            "rf": np.array([0.1]),
            "ridge": np.array([0.1]),
        }
        metadata_df = pd.DataFrame({
            "ward_name": ["NewWard_NotInTraining"],
            "year": [2020],
            "month": [1],
        })

        result = _run_stacked_inference(
            predictions, metadata_df,
            bucket="b", key_prefix="p",
            s3=self._mock_s3(artifacts_dir),
            tmp=download_dir, logger=logger,
        )

        # Manual: unseen ward → _global_mean = 0.11
        meta_X = np.array([[0.1, 0.1, 0.11]])
        meta_X_scaled = scaler.transform(meta_X)
        expected = np.clip(model.predict(meta_X_scaled), 0, None)

        np.testing.assert_allclose(result, expected, atol=1e-10)

    def test_negative_predictions_clipped_to_zero(self, meta_artifacts):
        """Meta-learner output that goes negative should be clipped to 0."""
        artifacts_dir, download_dir, model, scaler, ward_encoding = meta_artifacts

        # Feed very negative base predictions to push meta-learner output negative
        predictions = {
            "xgboost": np.array([-5.0]),
            "lgbm": np.array([-5.0]),
            "rf": np.array([-5.0]),
            "ridge": np.array([-5.0]),
        }
        metadata_df = pd.DataFrame({
            "ward_name": ["Dukana"],
            "year": [2020],
            "month": [1],
        })

        result = _run_stacked_inference(
            predictions, metadata_df,
            bucket="b", key_prefix="p",
            s3=self._mock_s3(artifacts_dir),
            tmp=download_dir, logger=logger,
        )

        assert all(result >= 0), f"Expected non-negative, got {result}"
