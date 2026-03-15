from __future__ import annotations

import json
import logging
from pathlib import Path

import geopandas as gpd
from rasterstats import zonal_stats

from lmr.config import AdminLevelConfig

logger = logging.getLogger("lmr")


def load_boundaries(admin_level: AdminLevelConfig, config_dir: Path) -> gpd.GeoDataFrame:
    """Load and filter admin boundary geometries.

    Args:
        admin_level: Admin level configuration with boundary file and optional filter.
        config_dir: Base directory where boundary files are stored (e.g. config/).

    Returns:
        GeoDataFrame of filtered boundaries.
    """
    boundary_path = config_dir / admin_level.boundary_file
    gdf = gpd.read_file(boundary_path)

    # Lowercase columns for consistent access
    gdf.columns = [c.lower() for c in gdf.columns]

    if admin_level.filter:
        field = admin_level.filter.field.lower()
        values = [v.lower() for v in admin_level.filter.values]
        gdf = gdf[gdf[field].str.lower().isin(values)]

    logger.info(
        "Loaded %d boundaries for admin level %d (%s)",
        len(gdf),
        admin_level.level,
        admin_level.name,
    )
    return gdf


def compute_zonal_stats(
    raster_path: Path,
    boundaries: gpd.GeoDataFrame,
    admin_level: AdminLevelConfig,
) -> list[dict]:
    """Compute zonal statistics for each boundary polygon against a raster.

    Args:
        raster_path: Path to the raster file (COG/GeoTIFF).
        boundaries: GeoDataFrame of boundary polygons.
        admin_level: Admin level config for id/name field lookup.

    Returns:
        List of dicts, one per boundary, with id, name, and stats.
    """
    id_field = admin_level.id_field.lower()
    name_field = admin_level.name_field.lower()

    stats = zonal_stats(
        boundaries,
        str(raster_path),
        stats=["count", "min", "max", "mean", "median", "std"],
        geojson_out=False,
    )

    results = []
    for i, row in enumerate(boundaries.itertuples()):
        stat = stats[i]
        results.append({
            "id": getattr(row, id_field, None),
            "name": getattr(row, name_field, None),
            "admin_level": admin_level.level,
            "stats": {
                "count": stat.get("count"),
                "min": stat.get("min"),
                "max": stat.get("max"),
                "mean": stat.get("mean"),
                "median": stat.get("median"),
                "std": stat.get("std"),
            },
        })

    logger.info(
        "Computed zonal stats for %d boundaries against %s",
        len(results),
        raster_path.name,
    )
    return results


def write_zonal_stats_json(stats: list[dict], output_path: Path) -> Path:
    """Write zonal stats results to a JSON file.

    Args:
        stats: List of per-boundary stat dicts.
        output_path: Local path to write the JSON.

    Returns:
        Path to the written file.
    """
    with output_path.open("w") as f:
        json.dump(stats, f, indent=2)
    return output_path
