"""
Tests for lmr.infer.postprocess — risk levels, confidence, season→date mapping.

Tests pure functions only (no S3 or file I/O).
"""

import numpy as np
import pandas as pd
import pytest

from lmr.infer.postprocess import (
    _assign_risk_level,
    _compute_ward_confidence,
    _get_time_cols,
    _timepoint_label,
    _season_to_date_key,
    DEFAULT_RISK_THRESHOLDS,
)


# ── Risk level assignment ────────────────────────────────────────────────────


class TestAssignRiskLevel:
    def test_normal_below_mean(self):
        """Ward mean below global mean → Normal."""
        assert _assign_risk_level(0.08, 0.10, DEFAULT_RISK_THRESHOLDS) == "Normal"

    def test_normal_at_mean(self):
        """Ward mean equal to global mean → Normal (0% above)."""
        assert _assign_risk_level(0.10, 0.10, DEFAULT_RISK_THRESHOLDS) == "Normal"

    def test_normal_just_below_threshold(self):
        """Ward mean 4.9% above global → Normal (threshold is 5%)."""
        global_mean = 0.10
        ward_mean = global_mean * 1.049
        assert _assign_risk_level(ward_mean, global_mean, DEFAULT_RISK_THRESHOLDS) == "Normal"

    def test_concerning_at_threshold(self):
        """Ward mean exactly 5% above global → Concerning."""
        global_mean = 0.10
        ward_mean = global_mean * 1.05
        assert _assign_risk_level(ward_mean, global_mean, DEFAULT_RISK_THRESHOLDS) == "Concerning"

    def test_concerning_in_range(self):
        """Ward mean 7% above global → Concerning."""
        global_mean = 0.10
        ward_mean = global_mean * 1.07
        assert _assign_risk_level(ward_mean, global_mean, DEFAULT_RISK_THRESHOLDS) == "Concerning"

    def test_critical_at_threshold(self):
        """Ward mean exactly 10% above global → Critical."""
        global_mean = 0.10
        ward_mean = global_mean * 1.10
        assert _assign_risk_level(ward_mean, global_mean, DEFAULT_RISK_THRESHOLDS) == "Critical"

    def test_critical_far_above(self):
        """Ward mean 50% above global → Critical."""
        global_mean = 0.10
        ward_mean = global_mean * 1.50
        assert _assign_risk_level(ward_mean, global_mean, DEFAULT_RISK_THRESHOLDS) == "Critical"

    def test_zero_global_mean(self):
        """If global mean is 0, always return Normal."""
        assert _assign_risk_level(0.5, 0.0, DEFAULT_RISK_THRESHOLDS) == "Normal"

    def test_negative_global_mean(self):
        """Negative global mean → Normal."""
        assert _assign_risk_level(0.5, -0.1, DEFAULT_RISK_THRESHOLDS) == "Normal"


# ── Confidence computation ───────────────────────────────────────────────────


class TestComputeWardConfidence:
    def test_single_prediction(self):
        """Single prediction → confidence 1.0."""
        assert _compute_ward_confidence(pd.Series([0.5])) == 1.0

    def test_identical_predictions(self):
        """All predictions the same → std=0 → confidence 1.0."""
        assert _compute_ward_confidence(pd.Series([0.3, 0.3, 0.3, 0.3])) == 1.0

    def test_moderate_disagreement(self):
        """Some spread → confidence between 0 and 1."""
        preds = pd.Series([0.1, 0.2, 0.3, 0.4])
        conf = _compute_ward_confidence(preds)
        assert 0.0 < conf < 1.0

    def test_high_disagreement(self):
        """Maximal spread → confidence near 0."""
        preds = pd.Series([0.0, 0.5])
        conf = _compute_ward_confidence(preds)
        # std = 0.3536, confidence = 1 - 0.3536/0.5 = 0.2929
        assert conf < 0.5

    def test_confidence_clamped_to_zero(self):
        """Extreme spread → confidence clamped to 0, not negative."""
        preds = pd.Series([0.0, 1.0])
        conf = _compute_ward_confidence(preds)
        assert conf >= 0.0

    def test_confidence_is_rounded(self):
        """Confidence should be rounded to 4 decimal places."""
        preds = pd.Series([0.1, 0.15, 0.2, 0.25])
        conf = _compute_ward_confidence(preds)
        assert conf == round(conf, 4)


# ── Time column detection ────────────────────────────────────────────────────


class TestGetTimeCols:
    def test_monthly(self):
        assert _get_time_cols("monthly") == ["year", "month"]

    def test_biannual(self):
        assert _get_time_cols("biannual") == ["season_year", "season"]

    def test_quadseasonal(self):
        assert _get_time_cols("quadseasonal") == ["season_year", "season"]


# ── Timepoint label formatting ───────────────────────────────────────────────


class TestTimepointLabel:
    def test_monthly(self):
        assert _timepoint_label((2019, 1), "monthly") == "2019Jan"
        assert _timepoint_label((2020, 12), "monthly") == "2020Dec"

    def test_biannual(self):
        assert _timepoint_label((2019, "OND"), "biannual") == "2019OND"
        assert _timepoint_label((2020, "MAM"), "biannual") == "2020MAM"

    def test_quadseasonal(self):
        assert _timepoint_label((2019, "LRLD"), "quadseasonal") == "2019LRLD"


# ── Season → date key mapping (Prism compatibility) ──────────────────────────


class TestSeasonToDateKey:
    def test_monthly_format(self):
        assert _season_to_date_key((2019, 1), "monthly") == "2019_01_01"
        assert _season_to_date_key((2020, 12), "monthly") == "2020_12_01"

    def test_biannual_ond(self):
        assert _season_to_date_key((2019, "OND"), "biannual") == "2019_12_31"

    def test_biannual_mam(self):
        assert _season_to_date_key((2020, "MAM"), "biannual") == "2020_05_31"

    def test_quadseasonal_lrld(self):
        assert _season_to_date_key((2019, "LRLD"), "quadseasonal") == "2019_09_30"

    def test_quadseasonal_srsd(self):
        assert _season_to_date_key((2019, "SRSD"), "quadseasonal") == "2019_02_28"

    def test_unknown_season_fallback(self):
        """Unknown season label → fallback to Dec 31."""
        assert _season_to_date_key((2019, "UNKNOWN"), "biannual") == "2019_12_31"

    def test_all_known_seasons_mapped(self):
        """Every season in SEASON_DATE_MAP produces a valid date key."""
        from lmr.infer.postprocess import SEASON_DATE_MAP
        for season in SEASON_DATE_MAP:
            key = _season_to_date_key((2020, season), "biannual")
            parts = key.split("_")
            assert len(parts) == 3, f"Bad format for {season}: {key}"
            assert parts[0] == "2020"
            assert 1 <= int(parts[1]) <= 12
            assert 1 <= int(parts[2]) <= 31
