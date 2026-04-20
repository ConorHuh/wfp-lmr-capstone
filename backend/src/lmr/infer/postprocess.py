"""
Inference postprocessing — step 3 of 3.

Takes ward-level model predictions with metadata, joins to ADMIN3 ward
boundaries, assigns risk levels, computes confidence, and writes per-timepoint
output files (CSV, GeoJSON, GeoTIFF) to S3.

Ported from sagemaker-pipeline/postprocess.py with all MLflow dependencies
removed and season→date key mapping added for Prism compatibility.
"""

from __future__ import annotations

import calendar
import json
import os
import tempfile
from typing import Dict, List, Optional, Tuple

import boto3
import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.enums import Resampling
from rasterio.features import rasterize as rio_rasterize
from rasterio.transform import from_bounds

from lmr.common.logging import setup_logging

# Risk thresholds (fraction above training label mean).
DEFAULT_RISK_THRESHOLDS = {
    "Normal": (0.0, 0.05),
    "Concerning": (0.05, 0.10),
    "Critical": (0.10, float("inf")),
}

# Season → end-of-season date for S3 folder naming (Prism YYYY_MM_DD format)
SEASON_DATE_MAP = {
    "OND": (12, 31),
    "MAM": (5, 31),
    "LRLD": (9, 30),
    "SRSD": (2, 28),
    "LRS": (9, 30),
    "LRS_dry": (11, 30),
    "SRS": (2, 28),
    "SRS_dry": (4, 30),
}


def run_postprocess(
    predictions_s3_path: str,
    admin3_shapefile_path: str,
    feature_names: List[str],
    output_s3_prefix: str,
    training_label_mean: float,
    season_scheme: str,
    prediction_column: str = "prediction",
    top_n_features: int = 5,
    risk_thresholds: Optional[Dict[str, Tuple[float, float]]] = None,
    geotiff_resolution: float = 0.01,
    compute_shap: bool = False,
    xgb_model=None,
) -> Tuple[str, List[str]]:
    """
    Postprocess ward-level predictions → per-timepoint GeoJSON + GeoTIFF + CSV.

    Parameters
    ----------
    predictions_s3_path : str
        S3 URI to predictions_with_metadata.parquet.
    admin3_shapefile_path : str
        Local path to ADMIN3 ward boundary GeoJSON.
    feature_names : list[str]
        Feature columns (for SHAP if compute_shap=True).
    output_s3_prefix : str
        Base S3 URI for output files.
    training_label_mean : float
        Global mean from training — anchors risk thresholds.
    season_scheme : str
        One of "biannual", "quadseasonal", "monthly".
    prediction_column : str
        Column containing ensemble prediction.
    top_n_features : int
        Number of top SHAP features per ward.
    risk_thresholds : dict, optional
        Override risk thresholds.
    geotiff_resolution : float
        GeoTIFF pixel size in degrees.
    compute_shap : bool
        Whether to compute SHAP values.
    xgb_model : optional
        Pre-loaded XGBoost model for SHAP computation.

    Returns
    -------
    base_s3_prefix, output_dirs
    """
    logger = setup_logging("INFO")
    thresholds = risk_thresholds or DEFAULT_RISK_THRESHOLDS
    global_mean = float(training_label_mean)
    logger.info("Using training_label_mean as global mean: %.6f", global_mean)

    # 1. Load predictions
    pred_df = pd.read_parquet(predictions_s3_path)
    logger.info("Loaded %d predictions from %s", len(pred_df), predictions_s3_path)

    # 2. Load ADMIN3 boundaries
    admin3 = _load_admin3_boundaries(admin3_shapefile_path)
    logger.info("Loaded %d ADMIN3 ward boundaries", len(admin3))

    # 3. Merge predictions with boundaries (ward-level: join on ward_name)
    if "ward_name" not in pred_df.columns:
        raise ValueError("Ward-level postprocess requires 'ward_name' column in predictions.")

    joined = pred_df.merge(
        admin3.rename(columns={"ADM3_EN": "ward_name"})[
            ["ward_name", "ADM3_PCODE", "ADM2_EN", "ADM1_EN"]
        ],
        on="ward_name",
        how="left",
    )
    unmatched = int(joined["ADM3_PCODE"].isna().sum())
    if unmatched > 0:
        logger.warning("%d ward(s) could not be matched to ADMIN3 boundaries", unmatched)

    # 4. Compute SHAP values (optional)
    shap_df = None
    if compute_shap and xgb_model is not None and feature_names:
        try:
            import shap
            # Need raw features — load from preprocessed_features.parquet
            features_s3 = predictions_s3_path.rsplit("/", 1)[0] + "/preprocessed_features.parquet"
            X_raw = pd.read_parquet(features_s3)
            explainer = shap.TreeExplainer(xgb_model)
            shap_vals = explainer.shap_values(X_raw[feature_names].values)
            shap_df = pd.DataFrame(shap_vals, columns=feature_names)
            shap_df["ward_name"] = joined["ward_name"].values[:len(shap_df)]
            ward_shap = shap_df.groupby("ward_name")[feature_names].apply(
                lambda g: g.abs().mean()
            )

            def _top_features(row, n=5):
                top = row.nlargest(n)
                return json.dumps([
                    {"feature": feat, "importance": round(float(v), 6)}
                    for feat, v in top.items()
                ])

            ward_top = ward_shap.apply(_top_features, axis=1).reset_index()
            ward_top.columns = ["ward_name", "top_features"]
            pred_df = pred_df.merge(ward_top, on="ward_name", how="left")
            pred_df["top_features"] = pred_df["top_features"].fillna("[]")
            # Re-merge joined with updated pred_df
            joined = pred_df.merge(
                admin3.rename(columns={"ADM3_EN": "ward_name"})[
                    ["ward_name", "ADM3_PCODE", "ADM2_EN", "ADM1_EN"]
                ],
                on="ward_name",
                how="left",
            )
            logger.info("SHAP top features computed for %d wards", len(ward_top))
        except Exception as e:
            logger.warning("SHAP computation failed: %s", e)

    # 5. Determine timepoints
    time_cols = _get_time_cols(season_scheme)
    available_time_cols = [c for c in time_cols if c in joined.columns]

    if available_time_cols:
        timepoint_iter = list(joined.groupby(available_time_cols))
    else:
        timepoint_iter = [("all", joined)]

    base_dir = output_s3_prefix.rstrip("/")
    output_dirs = []

    import s3fs
    fs = s3fs.S3FileSystem()

    # 6. Per-timepoint aggregation and output
    for time_key, time_grp in timepoint_iter:
        tp_label = (
            _timepoint_label(time_key, season_scheme)
            if available_time_cols else "all"
        )
        # Map season label to YYYY_MM_DD date key for Prism compatibility
        date_key = _season_to_date_key(time_key, season_scheme)
        tp_prefix = f"{base_dir}/{date_key}"
        output_dirs.append(tp_prefix)

        # Aggregate per ward
        ward_results = []
        model_pred_cols = [c for c in time_grp.columns if c.startswith("pred_")]

        for pcode, group in time_grp.groupby("ADM3_PCODE"):
            preds = group[prediction_column]
            mean_score = float(preds.mean())

            # Confidence from ensemble disagreement
            if model_pred_cols and len(preds) <= 1:
                ensemble_preds = group[model_pred_cols].iloc[0]
                confidence = _compute_ward_confidence(ensemble_preds)
            else:
                confidence = _compute_ward_confidence(preds)

            ward_info = {
                "ADM3_PCODE": pcode,
                "ADM3_EN": (
                    group["ADM3_EN"].iloc[0]
                    if "ADM3_EN" in group.columns
                    else group.get("ward_name", pd.Series([pcode])).iloc[0]
                ),
                "ADM2_EN": group["ADM2_EN"].iloc[0],
                "ADM1_EN": group["ADM1_EN"].iloc[0],
                "mean_predicted_loss_ratio": round(mean_score, 6),
                "median_predicted_loss_ratio": round(float(preds.median()), 6),
                "max_predicted_loss_ratio": round(float(preds.max()), 6),
                "n_observations": int(len(preds)),
                "risk_level": _assign_risk_level(mean_score, global_mean, thresholds),
                "confidence": confidence,
            }

            # Top features
            if "top_features" in group.columns:
                val = group["top_features"].iloc[0]
                ward_info["top_features"] = val if pd.notna(val) else "[]"
            else:
                ward_info["top_features"] = "[]"

            ward_results.append(ward_info)

        ward_df = pd.DataFrame(ward_results)

        # Merge geometry for GeoJSON / GeoTIFF
        ward_geo = admin3.merge(
            ward_df, on="ADM3_PCODE", how="inner", suffixes=("", "_agg")
        )
        for col in ["ADM3_EN_agg", "ADM2_EN_agg", "ADM1_EN_agg"]:
            if col in ward_geo.columns:
                ward_geo = ward_geo.drop(columns=[col])

        logger.info(
            "[%s → %s] %d wards — %s",
            tp_label, date_key, len(ward_df),
            ward_df["risk_level"].value_counts().to_dict(),
        )

        # Write files
        with tempfile.TemporaryDirectory() as tmp_dir:
            csv_path = os.path.join(tmp_dir, "ward_predictions.csv")
            geojson_path = os.path.join(tmp_dir, "ward_predictions.geojson")
            geotiff_path = os.path.join(tmp_dir, "ward_predictions.tif")

            ward_df.to_csv(csv_path, index=False)
            _write_ward_geojson(ward_geo, geojson_path)
            _rasterize_ward_predictions(
                ward_geo, output_path=geotiff_path, resolution=geotiff_resolution,
            )

            # Upload to S3
            fs.put(csv_path, f"{tp_prefix}/ward_predictions.csv")
            fs.put(geojson_path, f"{tp_prefix}/ward_predictions.geojson")
            fs.put(geotiff_path, f"{tp_prefix}/ward_predictions.tif")

        logger.info("  -> %s/", tp_prefix)

    logger.info("Written %d timepoint(s) to %s/", len(output_dirs), base_dir)
    return base_dir, output_dirs


# ── Helpers ──────────────────────────────────────────────────────────────────


def _load_admin3_boundaries(shapefile_path: str) -> gpd.GeoDataFrame:
    gdf = gpd.read_file(shapefile_path)

    required = ["ADM3_EN", "ADM3_PCODE", "ADM2_EN", "ADM1_EN", "geometry"]
    missing = [c for c in required if c not in gdf.columns]

    if missing:
        col_map = {c.upper(): c for c in gdf.columns}
        for req in missing[:]:
            if req.upper() in col_map:
                gdf = gdf.rename(columns={col_map[req.upper()]: req})

        geo_boundaries_map = {"ADM3_EN": "shapeName", "ADM3_PCODE": "shapeID"}
        still_missing = [c for c in required if c not in gdf.columns]
        rename = {
            geo_boundaries_map[req]: req
            for req in still_missing
            if geo_boundaries_map.get(req) in gdf.columns
        }
        if rename:
            gdf = gdf.rename(columns=rename)

        iebc_col_map = {
            "ADM3_EN": "IEBC_WARDS",
            "ADM3_PCODE": "PCODE",
            "ADM2_EN": "FIRST_DIST",
            "ADM1_EN": "FIRST_PROV",
        }
        still_missing = [c for c in required if c not in gdf.columns]
        rename = {
            iebc_col_map[req]: req
            for req in still_missing
            if iebc_col_map.get(req) in gdf.columns
        }
        if rename:
            gdf = gdf.rename(columns=rename)

        for col in ["ADM2_EN", "ADM1_EN"]:
            if col not in gdf.columns:
                gdf[col] = "Unknown"

    still_missing = [c for c in required if c not in gdf.columns]
    if still_missing:
        raise ValueError(
            f"ADMIN3 shapefile is missing required columns: {still_missing}. "
            f"Available columns: {gdf.columns.tolist()}"
        )

    if gdf.crs is None:
        gdf = gdf.set_crs(epsg=4326)
    else:
        gdf = gdf.to_crs(epsg=4326)
    return gdf[required]


def _assign_risk_level(
    ward_mean: float,
    global_mean: float,
    thresholds: Dict[str, Tuple[float, float]],
) -> str:
    if global_mean <= 0:
        return "Normal"
    pct_above = max((ward_mean - global_mean) / global_mean, 0.0)
    for level, (lo, hi) in thresholds.items():
        if lo <= pct_above < hi:
            return level
    return "Critical"


def _compute_ward_confidence(predictions: pd.Series) -> float:
    if len(predictions) <= 1:
        return 1.0
    std = predictions.std()
    confidence = max(0.0, 1.0 - (std / 0.5))
    return round(confidence, 4)


def _get_time_cols(season_scheme: str) -> List[str]:
    if season_scheme == "monthly":
        return ["year", "month"]
    return ["season_year", "season"]


def _timepoint_label(time_key, season_scheme: str) -> str:
    if season_scheme == "monthly":
        year, month = time_key
        return f"{int(year)}{calendar.month_abbr[int(month)]}"
    season_year, season = time_key
    return f"{int(season_year)}{season}"


def _season_to_date_key(time_key, season_scheme: str) -> str:
    """Convert a season timepoint to Prism-compatible YYYY_MM_DD date key."""
    if season_scheme == "monthly":
        year, month = time_key
        return f"{int(year)}_{int(month):02d}_01"
    season_year, season = time_key
    if season in SEASON_DATE_MAP:
        month, day = SEASON_DATE_MAP[season]
        return f"{int(season_year)}_{month:02d}_{day:02d}"
    # Fallback: use December 31
    return f"{int(season_year)}_12_31"


def _write_ward_geojson(ward_geo: gpd.GeoDataFrame, output_path: str) -> None:
    raw = json.loads(ward_geo.to_json())

    features = []
    for feat in raw["features"]:
        props = feat["properties"]

        top_features = props.get("top_features", "[]")
        if isinstance(top_features, str):
            try:
                top_features = json.loads(top_features)
            except Exception:
                top_features = []

        new_props = {
            "ADM3_EN": props.get("ADM3_EN"),
            "pcode": props.get("ADM3_PCODE"),
            "mean_predicted_loss_ratio": props.get("mean_predicted_loss_ratio"),
            "median_predicted_loss_ratio": props.get("median_predicted_loss_ratio"),
            "max_predicted_loss_ratio": props.get("max_predicted_loss_ratio"),
            "confidence": props.get("confidence"),
            "risk_level": props.get("risk_level"),
            "n_observations": props.get("n_observations"),
            "top_features": top_features,
        }

        features.append({
            "type": "Feature",
            "properties": new_props,
            "geometry": feat["geometry"],
        })

    geojson = {
        "type": "FeatureCollection",
        "name": "ward_predictions",
        "features": features,
    }

    with open(output_path, "w") as f:
        json.dump(geojson, f)


def _rasterize_ward_predictions(
    ward_geo: gpd.GeoDataFrame,
    output_path: str,
    resolution: float = 0.01,
    risk_encoding: Optional[Dict[str, int]] = None,
) -> None:
    if risk_encoding is None:
        risk_encoding = {"Normal": 0, "Concerning": 1, "Critical": 2}

    nodata = -9999.0

    minx, miny, maxx, maxy = ward_geo.total_bounds
    width = max(1, int(np.ceil((maxx - minx) / resolution)))
    height = max(1, int(np.ceil((maxy - miny) / resolution)))
    transform = from_bounds(minx, miny, maxx, maxy, width, height)

    def _shapes(values):
        for geom, val in zip(ward_geo.geometry, values):
            if pd.isna(val):
                continue
            yield geom, float(val)

    # Band 1: risk level encoded as 0/1/2
    risk_encoded = ward_geo["risk_level"].map(risk_encoding)
    band1 = rio_rasterize(
        _shapes(risk_encoded),
        out_shape=(height, width),
        transform=transform,
        fill=nodata,
        dtype="float32",
    )

    # Band 2: confidence
    band2 = rio_rasterize(
        _shapes(ward_geo["confidence"]),
        out_shape=(height, width),
        transform=transform,
        fill=nodata,
        dtype="float32",
    )

    # Band 3: importance of the top SHAP feature per ward
    top_importances = []
    for val in ward_geo["top_features"]:
        try:
            features = json.loads(val) if isinstance(val, str) else val
            top_importances.append(features[0]["importance"] if features else np.nan)
        except Exception:
            top_importances.append(np.nan)

    band3 = rio_rasterize(
        _shapes(pd.Series(top_importances, index=ward_geo.index)),
        out_shape=(height, width),
        transform=transform,
        fill=nodata,
        dtype="float32",
    )

    band_tags = [
        {"name": "risk_level_encoded", "encoding": json.dumps(risk_encoding)},
        {"name": "confidence"},
        {"name": "top_feature_importance"},
    ]

    with rasterio.open(
        output_path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=3,
        dtype="float32",
        crs="EPSG:4326",
        transform=transform,
        nodata=nodata,
        compress="lzw",
        tiled=True,
        blockxsize=256,
        blockysize=256,
    ) as dst:
        for band_idx, (band_data, tags) in enumerate(
            zip([band1, band2, band3], band_tags), start=1
        ):
            dst.write(band_data, band_idx)
            dst.update_tags(band_idx, **tags)

    # Build overviews for COG compatibility with TiTiler
    with rasterio.open(output_path, "r+") as dst:
        dst.build_overviews([2, 4, 8, 16], Resampling.nearest)
        dst.update_tags(ns="rio_overview", resampling="nearest")
