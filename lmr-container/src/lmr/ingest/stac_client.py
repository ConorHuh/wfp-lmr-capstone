from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import planetary_computer as pc
import pystac_client

from lmr.config import AppConfig, DatasetConfig

logger = logging.getLogger("lmr")


def build_stac_query_filters(dataset: DatasetConfig) -> dict:
    """Convert dataset query_filters into pystac-client compatible format."""
    query = {}
    for key, conditions in dataset.query_filters.items():
        query[key] = conditions
    return query


def search_stac(
    config: AppConfig,
    dataset: DatasetConfig,
    start_date: datetime | None = None,
) -> list:
    """Search STAC catalog for items matching the dataset configuration.

    Args:
        config: Application configuration.
        dataset: Dataset to search for.
        start_date: Override start date. If None, uses lookback_days from config.

    Returns:
        List of STAC items found.
    """
    now = datetime.now(timezone.utc)
    if start_date is None:
        start_date = now - timedelta(days=dataset.temporal.lookback_days)

    date_range = f"{start_date.strftime('%Y-%m-%dT%H:%M:%SZ')}/{now.strftime('%Y-%m-%dT%H:%M:%SZ')}"

    modifier = pc.sign_inplace if config.stac.requires_signing else None

    catalog = pystac_client.Client.open(
        config.stac.catalog_url,
        modifier=modifier,
    )

    query_filters = build_stac_query_filters(dataset)

    search = catalog.search(
        collections=[dataset.collection],
        bbox=config.aoi.bbox,
        datetime=date_range,
        query=query_filters if query_filters else None,
    )

    items = list(search.items())
    logger.info(
        "STAC search for %s found %d items (date range: %s)",
        dataset.name,
        len(items),
        date_range,
    )
    return items
