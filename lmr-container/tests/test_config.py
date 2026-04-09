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
    # 18 enabled: matches planetary_computer_downloader.py exactly
    assert len(enabled) >= 16
    names = {d.name for d in enabled}
    # MODIS
    assert "modis-ndvi" in names
    assert "modis-evi" in names
    assert "modis-lai" in names
    assert "modis-fpar" in names
    assert "modis-lst-day" in names
    assert "modis-lst-night" in names
    assert "modis-sr" in names
    assert "modis-fire" in names
    assert "modis-et" in names
    assert "modis-pet" in names
    # Sentinel
    assert "s2-red" in names
    assert "s1-vv" in names
    # Static
    assert "dem" in names
    assert "worldcover" in names


def test_disabled_datasets():
    config = load_config(CONFIG_PATH)
    disabled = {d.name for d in config.datasets if not d.enabled}
    assert "rainfall-chirps" in disabled
    assert "soil-moisture" in disabled


def test_dataset_fields():
    config = load_config(CONFIG_PATH)
    ndvi = next(d for d in config.datasets if d.name == "modis-ndvi")
    assert ndvi.collection == "modis-13Q1-061"
    assert "250m_16_days_NDVI" in ndvi.assets
    assert ndvi.processing.crs == "EPSG:4326"
    assert ndvi.temporal.lookback_days == 16


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


def test_external_buckets_config():
    config = load_config(CONFIG_PATH)
    assert config.external_buckets.sagemaker is not None
    assert config.external_buckets.sagemaker.name == "amazon-sagemaker-575108933641-us-east-1-c422b90ce861"
    assert config.external_buckets.sagemaker.region == "us-east-1"


def test_inference_config():
    config = load_config(CONFIG_PATH)
    assert config.inference.model_name == "livestock-mortality"
    assert config.inference.output_bucket == "lmr-data-cogs-dev"
    assert config.inference.ssm_prefix == "/lmr/model"
    assert config.inference.boundary_file == "boundaries/kenya_wards.geojson"
