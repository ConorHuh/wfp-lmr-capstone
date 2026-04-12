from pathlib import Path

from lmr.config import load_config

CONFIG_PATH = Path(__file__).parent.parent / "config" / "datasets.yaml"


def test_load_config():
    config = load_config(CONFIG_PATH)
    assert config.global_.s3_bucket == "lmr-data-cogs-dev"
    assert config.aoi.name == "kenya"
    assert len(config.aoi.bbox) == 4
    assert config.stac.requires_signing is True


def test_datasets_loaded():
    config = load_config(CONFIG_PATH)
    enabled = [d for d in config.datasets if d.enabled]
    assert len(enabled) >= 10
    names = {d.name for d in enabled}
    # MODIS core
    assert "modis-ndvi" in names
    assert "modis-evi" in names
    assert "modis-lai" in names
    assert "modis-lst-day" in names
    assert "modis-sr" in names
    assert "modis-et" in names


def test_disabled_datasets():
    config = load_config(CONFIG_PATH)
    disabled = {d.name for d in config.datasets if not d.enabled}
    # Core disabled datasets — Sentinel, SAR, static, fire
    assert "s1-vv" in disabled
    assert "s2-red" in disabled
    assert "dem" in disabled
    assert "modis-fire" in disabled


def test_dataset_fields():
    config = load_config(CONFIG_PATH)
    ndvi = next(d for d in config.datasets if d.name == "modis-ndvi")
    assert ndvi.collection == "modis-13Q1-061"
    assert "250m_16_days_NDVI" in ndvi.assets
    assert ndvi.processing.crs == "EPSG:4326"
    assert ndvi.temporal.lookback_days == 1100


def test_admin_levels():
    config = load_config(CONFIG_PATH)
    assert len(config.admin_levels) == 1
    admin3 = config.admin_levels[0]
    assert admin3.level == 3
    assert admin3.name == "wards"
    assert admin3.id_field == "pcode"
    assert admin3.name_field == "iebc_wards"
    assert admin3.filter is not None
    assert admin3.filter.field == "first_dist"
    assert "Marsabit" in admin3.filter.values


def test_aoi_is_full_kenya():
    config = load_config(CONFIG_PATH)
    west, south, east, north = config.aoi.bbox
    # Full Kenya bbox should span roughly 33-42 E, -5 to 5.5 N
    assert west < 34.5
    assert east > 41.0
    assert south < -4.0
    assert north > 5.0


def test_serve_config():
    config = load_config(CONFIG_PATH)
    assert config.serve.presigned_url_expiry_seconds == 3600
    assert "*" in config.serve.cors_origins
    assert config.serve.predictions_prefix == "predictions"


def test_inference_config():
    config = load_config(CONFIG_PATH)
    assert config.inference.enabled is True
    assert config.inference.model_name == "livestock-mortality"
    assert config.inference.model_s3_prefix == "s3://lmr-data-cogs-dev/models/inference_bundle"
    assert config.inference.ward_boundaries_s3_key == "models/geoBoundaries-KEN-ADM3.geojson"
    assert config.inference.output_bucket == "lmr-data-cogs-dev"
    assert config.inference.boundary_file == "boundaries/kenya_wards.geojson"
    assert config.inference.schemes == ["biannual", "quadseasonal", "monthly"]
    assert config.inference.feature_window_months == 36
    assert config.inference.n_sample_points == 9
