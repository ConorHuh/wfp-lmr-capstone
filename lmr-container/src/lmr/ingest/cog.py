from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import rasterio
from rasterio.crs import CRS
from rasterio.warp import calculate_default_transform, reproject, Resampling

from lmr.config import ProcessingConfig

logger = logging.getLogger("lmr")


def ensure_cog(src_path: Path, dst_path: Path, processing: ProcessingConfig) -> Path:
    """Reproject raster to target CRS and write as a Cloud Optimized GeoTIFF.

    Args:
        src_path: Input raster path.
        dst_path: Output COG path.
        processing: Processing configuration (CRS, resolution).

    Returns:
        Path to the output COG.
    """
    target_crs = CRS.from_string(processing.crs)

    with rasterio.open(src_path) as src:
        transform, width, height = calculate_default_transform(
            src.crs, target_crs, src.width, src.height, *src.bounds,
            resolution=_meters_to_degrees(processing.resolution_m),
        )

        profile = src.profile.copy()
        profile.update(
            driver="GTiff",
            crs=target_crs,
            transform=transform,
            width=width,
            height=height,
        )

        # COG creation options
        cog_profile = {
            "tiled": True,
            "blockxsize": 256,
            "blockysize": 256,
            "compress": "deflate",
        }
        profile.update(cog_profile)

        with rasterio.open(dst_path, "w", **profile) as dst:
            for band in range(1, src.count + 1):
                reproject(
                    source=rasterio.band(src, band),
                    destination=rasterio.band(dst, band),
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=transform,
                    dst_crs=target_crs,
                    resampling=Resampling.nearest,
                )

    # Build overviews for COG
    with rasterio.open(dst_path, "r+") as dst:
        overview_levels = [2, 4, 8, 16]
        dst.build_overviews(overview_levels, Resampling.nearest)
        dst.update_tags(ns="rio_overview", resampling="nearest")

    logger.info("Created COG: %s", dst_path)
    return dst_path


def clip_to_bbox(src_path: Path, bbox: list[float], dst_path: Path) -> Path:
    """Clip raster to bounding box [west, south, east, north].

    Args:
        src_path: Input raster path.
        bbox: Bounding box as [west, south, east, north].
        dst_path: Output path.

    Returns:
        Path to the clipped raster.
    """
    from rasterio.windows import from_bounds

    with rasterio.open(src_path) as src:
        window = from_bounds(*bbox, transform=src.transform)
        data = src.read(window=window)
        transform = src.window_transform(window)

        profile = src.profile.copy()
        profile.update(
            width=data.shape[2],
            height=data.shape[1],
            transform=transform,
        )

        with rasterio.open(dst_path, "w", **profile) as dst:
            dst.write(data)

    logger.info("Clipped raster to bbox %s: %s", bbox, dst_path)
    return dst_path


def download_asset(item, asset_key: str, work_dir: Path) -> Path:
    """Download a STAC item asset to a local file.

    Args:
        item: pystac Item with signed URLs.
        asset_key: Key of the asset to download.
        work_dir: Directory to write the downloaded file.

    Returns:
        Path to the downloaded file.
    """
    import urllib.request

    asset = item.assets[asset_key]
    href = asset.href
    dest = work_dir / f"{item.id}_{asset_key}.tif"

    logger.info("Downloading asset %s from %s", asset_key, href)
    urllib.request.urlretrieve(href, dest)
    return dest


def _meters_to_degrees(meters: int) -> float:
    """Rough conversion from meters to degrees at the equator."""
    return meters / 111_320.0
