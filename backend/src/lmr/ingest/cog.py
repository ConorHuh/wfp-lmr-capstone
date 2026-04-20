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

    If the source CRS is not geographic (e.g. MODIS sinusoidal), the bbox is
    reprojected into source CRS coordinates before clipping. If the source
    raster does not overlap the bbox at all, the file is copied unchanged
    and a warning is logged.

    Args:
        src_path: Input raster path.
        bbox: Bounding box as [west, south, east, north] in EPSG:4326.
        dst_path: Output path.

    Returns:
        Path to the clipped raster.
    """
    import shutil
    from rasterio.windows import from_bounds
    from rasterio.warp import transform_bounds

    with rasterio.open(src_path) as src:
        # Reproject bbox into source CRS if needed
        src_crs = src.crs
        if src_crs and not src_crs.is_geographic:
            clip_bounds = transform_bounds(CRS.from_epsg(4326), src_crs, *bbox)
        else:
            clip_bounds = bbox

        # Intersect clip bounds with raster bounds
        left = max(clip_bounds[0], src.bounds.left)
        bottom = max(clip_bounds[1], src.bounds.bottom)
        right = min(clip_bounds[2], src.bounds.right)
        top = min(clip_bounds[3], src.bounds.top)

        if left >= right or bottom >= top:
            logger.warning(
                "Raster %s does not overlap bbox %s, skipping clip", src_path.name, bbox
            )
            shutil.copy2(src_path, dst_path)
            return dst_path

        window = from_bounds(left, bottom, right, top, transform=src.transform)
        data = src.read(window=window)

        if data.shape[1] == 0 or data.shape[2] == 0:
            logger.warning("Clip produced empty raster for %s, skipping", src_path.name)
            shutil.copy2(src_path, dst_path)
            return dst_path

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


def download_asset(item, asset_key: str, work_dir: Path, sign: bool = True, max_retries: int = 3) -> Path:
    """Download a STAC item asset to a local file.

    Re-signs the item before each attempt to avoid SAS token expiry on long runs.

    Args:
        item: pystac Item.
        asset_key: Key of the asset to download.
        work_dir: Directory to write the downloaded file.
        sign: Whether to re-sign the item via Planetary Computer (default True).
        max_retries: Maximum number of retry attempts on HTTP errors.

    Returns:
        Path to the downloaded file.
    """
    import time
    import urllib.request
    import urllib.error

    dest = work_dir / f"{item.id}_{asset_key}.tif"

    for attempt in range(max_retries):
        if sign:
            import planetary_computer as pc
            item = pc.sign(item)

        asset = item.assets[asset_key]
        href = asset.href

        try:
            logger.info("Downloading asset %s (attempt %d)", asset_key, attempt + 1)
            urllib.request.urlretrieve(href, dest)
            return dest
        except urllib.error.HTTPError as e:
            if e.code in (403, 429, 500, 502, 503) and attempt < max_retries - 1:
                wait = 2 ** attempt * 5
                logger.warning("HTTP %d downloading %s, retrying in %ds", e.code, asset_key, wait)
                time.sleep(wait)
            else:
                raise

    return dest


def merge_cogs(cog_paths: list[Path], dst_path: Path) -> Path:
    """Merge multiple COGs covering different areas into a single mosaic COG.

    Used when multiple STAC items (e.g. MODIS sinusoidal tiles) cover the same
    date but different spatial extents. The merged result preserves the profile
    of the first input and adds COG overviews.

    Args:
        cog_paths: List of COG file paths to merge.
        dst_path: Output merged COG path.

    Returns:
        Path to the merged COG.
    """
    from rasterio.merge import merge

    if len(cog_paths) == 1:
        import shutil
        shutil.copy2(cog_paths[0], dst_path)
        return dst_path

    datasets = [rasterio.open(p) for p in cog_paths]
    try:
        mosaic, out_transform = merge(datasets)
    finally:
        for ds in datasets:
            ds.close()

    with rasterio.open(cog_paths[0]) as ref:
        profile = ref.profile.copy()

    profile.update(
        width=mosaic.shape[2],
        height=mosaic.shape[1],
        transform=out_transform,
        tiled=True,
        blockxsize=256,
        blockysize=256,
        compress="deflate",
    )

    with rasterio.open(dst_path, "w", **profile) as dst:
        dst.write(mosaic)

    with rasterio.open(dst_path, "r+") as dst:
        dst.build_overviews([2, 4, 8, 16], Resampling.nearest)
        dst.update_tags(ns="rio_overview", resampling="nearest")

    logger.info("Merged %d COGs into: %s", len(cog_paths), dst_path)
    return dst_path


def _meters_to_degrees(meters: int) -> float:
    """Rough conversion from meters to degrees at the equator."""
    return meters / 111_320.0
