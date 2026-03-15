from pathlib import Path

from lmr.config import load_config

CONFIG_PATH = Path(__file__).parent.parent / "config" / "datasets.yaml"


def test_load_config():
    config = load_config(CONFIG_PATH)
    assert config.global_.s3_bucket == "lmr-data-cogs"
    assert config.aoi.name == "kenya"
    assert len(config.aoi.bbox) == 4
    assert config.stac.requires_signing is True


def test_datasets_loaded():
    config = load_config(CONFIG_PATH)
    enabled = [d for d in config.datasets if d.enabled]
    assert len(enabled) >= 2
    names = {d.name for d in enabled}
    assert "ndvi-sentinel2" in names
    assert "rainfall-chirps" in names


def test_dataset_fields():
    config = load_config(CONFIG_PATH)
    ndvi = next(d for d in config.datasets if d.name == "ndvi-sentinel2")
    assert ndvi.collection == "sentinel-2-l2a"
    assert "B04" in ndvi.assets
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
