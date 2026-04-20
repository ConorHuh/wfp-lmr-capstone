"""Tests for Copernicus CDS source backend."""

from datetime import datetime, timezone

from lmr.config import AppConfig, DatasetConfig, AOIConfig, STACConfig, GlobalConfig, TemporalConfig, ProcessingConfig
from lmr.ingest.sources import SyntheticItem, _search_cds


def _make_config():
    return AppConfig(
        **{
            "global": GlobalConfig(s3_bucket="test", s3_prefix="ingested"),
            "aoi": AOIConfig(name="kenya", bbox=[33.91, -4.80, 41.91, 5.41]),
            "stac": STACConfig(catalog_url="https://example.com"),
            "datasets": [],
        }
    )


def _make_dataset():
    return DatasetConfig(
        name="era5-soil-moisture",
        source="copernicus_cds",
        collection="reanalysis-era5-land-monthly-means",
        assets=["swvl1", "swvl2", "swvl3", "swvl4"],
        temporal=TemporalConfig(lookback_days=90),
        processing=ProcessingConfig(resolution_m=9000, crs="EPSG:4326"),
    )


def test_search_cds_generates_monthly_items():
    config = _make_config()
    dataset = _make_dataset()

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 3, 15, tzinfo=timezone.utc)

    items = _search_cds(config, dataset, start, end)

    assert len(items) == 3
    assert all(isinstance(i, SyntheticItem) for i in items)
    assert items[0].id == "era5-soil-2024-01"
    assert items[2].id == "era5-soil-2024-03"


def test_search_cds_item_metadata_has_area():
    config = _make_config()
    dataset = _make_dataset()

    start = datetime(2024, 6, 1, tzinfo=timezone.utc)
    end = datetime(2024, 6, 30, tzinfo=timezone.utc)

    items = _search_cds(config, dataset, start, end)
    assert len(items) == 1

    meta = items[0].metadata
    assert meta["year"] == 2024
    assert meta["month"] == 6
    # CDS area format: [N, W, S, E]
    assert meta["area"] == [5.41, 33.91, -4.80, 41.91]


def test_search_cds_crosses_year_boundary():
    config = _make_config()
    dataset = _make_dataset()

    start = datetime(2023, 11, 1, tzinfo=timezone.utc)
    end = datetime(2024, 2, 28, tzinfo=timezone.utc)

    items = _search_cds(config, dataset, start, end)
    assert len(items) == 4

    years = [i.metadata["year"] for i in items]
    months = [i.metadata["month"] for i in items]
    assert years == [2023, 2023, 2024, 2024]
    assert months == [11, 12, 1, 2]


def test_search_cds_item_datetime():
    config = _make_config()
    dataset = _make_dataset()

    start = datetime(2024, 7, 1, tzinfo=timezone.utc)
    end = datetime(2024, 7, 31, tzinfo=timezone.utc)

    items = _search_cds(config, dataset, start, end)
    assert items[0].datetime == datetime(2024, 7, 1, tzinfo=timezone.utc)
