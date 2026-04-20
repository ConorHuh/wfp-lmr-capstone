"""Tests for ingest COG operations: clip_to_bbox, ensure_cog."""

import tempfile
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_bounds

from lmr.ingest.cog import clip_to_bbox


def _create_test_raster(path: Path, width=100, height=100, bbox=(33.0, -5.0, 42.0, 6.0)):
    """Create a small synthetic GeoTIFF for testing."""
    west, south, east, north = bbox
    transform = from_bounds(west, south, east, north, width, height)
    data = np.random.rand(1, height, width).astype(np.float32)

    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=width,
        height=height,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=transform,
    ) as dst:
        dst.write(data)

    return data


def test_clip_to_bbox_clips_correctly():
    with tempfile.TemporaryDirectory() as tmp:
        src_path = Path(tmp) / "src.tif"
        dst_path = Path(tmp) / "clipped.tif"

        # Full Kenya bbox
        _create_test_raster(src_path, bbox=(33.0, -5.0, 42.0, 6.0))

        # Clip to Marsabit area (smaller bbox)
        clip_bbox = [36.0, 0.0, 40.0, 4.0]
        result = clip_to_bbox(src_path, clip_bbox, dst_path)

        assert result == dst_path
        assert dst_path.exists()

        with rasterio.open(dst_path) as clipped:
            bounds = clipped.bounds
            # Clipped raster should be within the clip bbox (with pixel alignment tolerance)
            assert bounds.left >= 35.5
            assert bounds.right <= 40.5
            assert bounds.bottom >= -0.5
            assert bounds.top <= 4.5
            # Should be smaller than original
            assert clipped.width < 100
            assert clipped.height < 100


def test_clip_to_bbox_no_overlap_copies_original():
    with tempfile.TemporaryDirectory() as tmp:
        src_path = Path(tmp) / "src.tif"
        dst_path = Path(tmp) / "clipped.tif"

        # Raster covering Kenya
        _create_test_raster(src_path, bbox=(33.0, -5.0, 42.0, 6.0))

        # Clip to somewhere completely outside (e.g., South America)
        clip_bbox = [-80.0, -40.0, -60.0, -20.0]
        result = clip_to_bbox(src_path, clip_bbox, dst_path)

        assert result == dst_path
        assert dst_path.exists()

        # Should be same size as original since no overlap
        with rasterio.open(dst_path) as clipped, rasterio.open(src_path) as src:
            assert clipped.width == src.width
            assert clipped.height == src.height
