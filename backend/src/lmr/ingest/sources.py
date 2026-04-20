"""Data source dispatch — routes search/download to the correct backend.

Supported sources:
  - planetary_computer  (default) — STAC API via pystac-client
  - nasa_earthdata      — HDF granules via earthaccess
  - chirps_http         — CHIRPS v2.0 monthly rainfall GeoTIFFs via HTTP
  - copernicus_cds      — ERA5-Land soil moisture via CDS API
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lmr.config import AppConfig, DatasetConfig

logger = logging.getLogger("lmr")


@dataclass
class SyntheticItem:
    """Lightweight item returned by non-STAC search backends (CHIRPS, CDS)."""
    id: str
    datetime: datetime
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public dispatch API
# ---------------------------------------------------------------------------


def search_items(
    config: AppConfig,
    dataset: DatasetConfig,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> list:
    """Search for items using the correct backend for this dataset."""
    if dataset.source == "nasa_earthdata":
        return _search_nasa(config, dataset, start_date, end_date)
    if dataset.source == "chirps_http":
        return _search_chirps(config, dataset, start_date, end_date)
    if dataset.source == "copernicus_cds":
        return _search_cds(config, dataset, start_date, end_date)
    return _search_planetary_computer(config, dataset, start_date, end_date)


def download_item_asset(
    item,
    asset_key: str,
    work_dir: Path,
    dataset: DatasetConfig,
) -> Path:
    """Download an asset using the correct backend for this dataset."""
    if dataset.source == "nasa_earthdata":
        return _download_nasa(item, asset_key, work_dir, dataset)
    if dataset.source == "chirps_http":
        return _download_chirps(item, asset_key, work_dir)
    if dataset.source == "copernicus_cds":
        return _download_cds(item, asset_key, work_dir)
    return _download_pc(item, asset_key, work_dir)


def get_item_date(item) -> str:
    """Extract a YYYY_MM_DD date string from a pystac Item or earthaccess DataGranule."""
    # pystac Item
    if hasattr(item, "datetime") and item.datetime:
        return item.datetime.strftime("%Y_%m_%d")
    if hasattr(item, "common_metadata"):
        dt = item.common_metadata.start_datetime
        if dt:
            return dt.strftime("%Y_%m_%d")

    # earthaccess DataGranule — extract from UMM temporal metadata
    if hasattr(item, "__getitem__"):
        try:
            umm = item["umm"]
            temporal = umm["TemporalExtent"]["RangeDateTime"]
            begin = temporal["BeginningDateTime"]
            dt = datetime.fromisoformat(begin.replace("Z", "+00:00"))
            return dt.strftime("%Y_%m_%d")
        except (KeyError, TypeError, ValueError):
            pass

    return "unknown"


def has_asset(item, asset_key: str, dataset: DatasetConfig) -> bool:
    """Check if an item has the requested asset.

    For NASA Earthdata, CHIRPS, and CDS sources every item contains all
    expected assets, so we always return True.  For Planetary Computer
    we check item.assets.
    """
    if dataset.source in ("nasa_earthdata", "chirps_http", "copernicus_cds"):
        return True
    return asset_key in getattr(item, "assets", {})


# ---------------------------------------------------------------------------
# Planetary Computer backend (unchanged logic, extracted from stac_client.py)
# ---------------------------------------------------------------------------


def _search_planetary_computer(
    config: AppConfig,
    dataset: DatasetConfig,
    start_date: datetime | None,
    end_date: datetime | None,
) -> list:
    from lmr.ingest.stac_client import search_stac

    return search_stac(config, dataset, start_date=start_date, end_date=end_date)


def _download_pc(item, asset_key: str, work_dir: Path) -> Path:
    from lmr.ingest.cog import download_asset

    return download_asset(item, asset_key, work_dir)


# ---------------------------------------------------------------------------
# NASA Earthdata backend
# ---------------------------------------------------------------------------


def _ensure_earthdata_login() -> None:
    """Login to NASA Earthdata using env vars (EARTHDATA_USERNAME / EARTHDATA_PASSWORD)."""
    import earthaccess

    auth = earthaccess.login(strategy="environment")
    if not auth.authenticated:
        raise RuntimeError(
            "NASA Earthdata login failed. "
            "Set EARTHDATA_USERNAME and EARTHDATA_PASSWORD environment variables."
        )


def _search_nasa(
    config: AppConfig,
    dataset: DatasetConfig,
    start_date: datetime | None,
    end_date: datetime | None,
) -> list:
    import earthaccess

    _ensure_earthdata_login()

    now = datetime.now(timezone.utc)
    if start_date is None:
        start_date = now - timedelta(days=dataset.temporal.lookback_days)
    if end_date is None:
        end_date = now

    # earthaccess.search_data expects short_name (e.g. "MOD16A2GF") and version
    collection = dataset.collection
    version = "061"
    if "." in collection:
        collection, version = collection.rsplit(".", 1)

    bbox = tuple(config.aoi.bbox)  # (west, south, east, north)

    results = earthaccess.search_data(
        short_name=collection,
        version=version,
        bounding_box=bbox,
        temporal=(start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")),
    )
    logger.info(
        "NASA Earthdata search for %s found %d granules (date range: %s to %s)",
        dataset.name,
        len(results),
        start_date.strftime("%Y-%m-%d"),
        end_date.strftime("%Y-%m-%d"),
    )
    return results


def _download_nasa(
    granule,
    asset_key: str,
    work_dir: Path,
    dataset: DatasetConfig,
) -> Path:
    import earthaccess

    _ensure_earthdata_login()

    downloaded = earthaccess.download([granule], str(work_dir))
    if not downloaded:
        raise RuntimeError(f"earthaccess.download returned no files for {asset_key}")

    src_path = Path(downloaded[0])

    # If dataset specifies an HDF subdataset, extract it to GeoTIFF
    subdataset = dataset.hdf_subdataset
    if subdataset and src_path.suffix.lower() in (".hdf", ".hdf4", ".he4"):
        tif_path = work_dir / f"{src_path.stem}_{asset_key}.tif"
        _extract_hdf_subdataset(src_path, subdataset, tif_path)
        return tif_path

    return src_path


def _extract_hdf_subdataset(hdf_path: Path, subdataset_name: str, out_path: Path) -> Path:
    """Extract a named subdataset from an HDF-EOS2 file to a GeoTIFF.

    Uses netCDF4 to read HDF4 data (more portable than rasterio's HDF4 driver,
    which requires GDAL compiled with libhdf4).  Parses the MODIS sinusoidal
    geotransform from StructMetadata.0 and writes a georeferenced GeoTIFF.
    """
    import re

    import numpy as np
    import rasterio
    from netCDF4 import Dataset
    from rasterio.crs import CRS
    from rasterio.transform import Affine

    ds = Dataset(str(hdf_path), "r")
    try:
        if subdataset_name not in ds.variables:
            available = list(ds.variables.keys())
            raise ValueError(
                f"Subdataset '{subdataset_name}' not found in {hdf_path.name}. "
                f"Available: {available}"
            )

        data = ds.variables[subdataset_name][:].data.astype("int16")

        # Parse geotransform from MODIS StructMetadata
        struct_meta = ds.getncattr("StructMetadata.0")
        ul = re.search(r"UpperLeftPointMtrs=\(([-\d.]+),([-\d.]+)\)", struct_meta)
        lr = re.search(r"LowerRightMtrs=\(([-\d.]+),([-\d.]+)\)", struct_meta)

        if not ul or not lr:
            raise ValueError(f"Cannot parse geotransform from {hdf_path.name}")

        ul_x, ul_y = float(ul.group(1)), float(ul.group(2))
        lr_x, lr_y = float(lr.group(1)), float(lr.group(2))

        nrows, ncols = data.shape
        pixel_x = (lr_x - ul_x) / ncols
        pixel_y = (lr_y - ul_y) / nrows  # negative (north-up)

        transform = Affine(pixel_x, 0, ul_x, 0, pixel_y, ul_y)

        # MODIS sinusoidal projection
        modis_crs = CRS.from_proj4(
            "+proj=sinu +lon_0=0 +x_0=0 +y_0=0 +R=6371007.181 +units=m +no_defs"
        )
    finally:
        ds.close()

    with rasterio.open(
        out_path,
        "w",
        driver="GTiff",
        width=ncols,
        height=nrows,
        count=1,
        dtype="int16",
        crs=modis_crs,
        transform=transform,
    ) as dst:
        dst.write(data, 1)

    logger.info("Extracted HDF subdataset %s → %s", subdataset_name, out_path.name)
    return out_path


# ---------------------------------------------------------------------------
# CHIRPS HTTP backend — monthly rainfall GeoTIFFs from UCSB
# ---------------------------------------------------------------------------

CHIRPS_BASE_URL = "https://data.chc.ucsb.edu/products/CHIRPS-2.0/global_monthly/tifs"


def _search_chirps(
    config: AppConfig,
    dataset: DatasetConfig,
    start_date: datetime | None,
    end_date: datetime | None,
) -> list[SyntheticItem]:
    """Generate one SyntheticItem per month in the date range."""
    now = datetime.now(timezone.utc)
    if start_date is None:
        start_date = now - timedelta(days=dataset.temporal.lookback_days)
    if end_date is None:
        end_date = now

    items = []
    year, month = start_date.year, start_date.month
    while (year, month) <= (end_date.year, end_date.month):
        dt = datetime(year, month, 1, tzinfo=timezone.utc)
        fname = f"chirps-v2.0.{year}.{month:02d}.tif.gz"
        url = f"{CHIRPS_BASE_URL}/{fname}"
        items.append(SyntheticItem(
            id=f"chirps-{year}-{month:02d}",
            datetime=dt,
            metadata={"url": url},
        ))
        # Advance to next month
        month += 1
        if month > 12:
            month = 1
            year += 1

    logger.info(
        "CHIRPS search generated %d monthly items (%s to %s)",
        len(items),
        start_date.strftime("%Y-%m"),
        end_date.strftime("%Y-%m"),
    )
    return items


def _download_chirps(item: SyntheticItem, asset_key: str, work_dir: Path) -> Path:
    """Download and decompress a CHIRPS monthly GeoTIFF."""
    import gzip
    import shutil
    import time
    import urllib.error
    import urllib.request

    url = item.metadata["url"]
    gz_path = work_dir / f"{item.id}.tif.gz"
    tif_path = work_dir / f"{item.id}_{asset_key}.tif"

    max_retries = 3
    for attempt in range(max_retries):
        try:
            logger.info("Downloading CHIRPS %s (attempt %d)", item.id, attempt + 1)
            urllib.request.urlretrieve(url, gz_path)
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < max_retries - 1:
                wait = 2 ** attempt * 5
                logger.warning("HTTP %d downloading CHIRPS, retrying in %ds", e.code, wait)
                time.sleep(wait)
            else:
                raise

    # Decompress .tif.gz → .tif
    with gzip.open(gz_path, "rb") as gz_in, open(tif_path, "wb") as tif_out:
        shutil.copyfileobj(gz_in, tif_out)

    return tif_path


# ---------------------------------------------------------------------------
# Copernicus CDS backend — ERA5-Land soil moisture via CDS API
# ---------------------------------------------------------------------------


def _search_cds(
    config: AppConfig,
    dataset: DatasetConfig,
    start_date: datetime | None,
    end_date: datetime | None,
) -> list[SyntheticItem]:
    """Generate one SyntheticItem per month in the date range."""
    now = datetime.now(timezone.utc)
    if start_date is None:
        start_date = now - timedelta(days=dataset.temporal.lookback_days)
    if end_date is None:
        end_date = now

    # CDS API bbox: [N, W, S, E]
    west, south, east, north = config.aoi.bbox
    cds_area = [north, west, south, east]

    items = []
    year, month = start_date.year, start_date.month
    while (year, month) <= (end_date.year, end_date.month):
        dt = datetime(year, month, 1, tzinfo=timezone.utc)
        items.append(SyntheticItem(
            id=f"era5-soil-{year}-{month:02d}",
            datetime=dt,
            metadata={"year": year, "month": month, "area": cds_area},
        ))
        month += 1
        if month > 12:
            month = 1
            year += 1

    logger.info(
        "CDS search generated %d monthly items (%s to %s)",
        len(items),
        start_date.strftime("%Y-%m"),
        end_date.strftime("%Y-%m"),
    )
    return items


def _download_cds(item: SyntheticItem, asset_key: str, work_dir: Path) -> Path:
    """Download ERA5-Land data from CDS API, extract a single variable+month as GeoTIFF.

    Caches the yearly NetCDF in work_dir to avoid re-downloading for each
    month and variable within the same dataset processing run.
    """
    import cdsapi
    import numpy as np
    import rasterio
    import xarray as xr
    from rasterio.transform import from_bounds

    year = item.metadata["year"]
    month = item.metadata["month"]
    area = item.metadata["area"]

    # Check for cached yearly NetCDF
    nc_path = work_dir / f"era5_soil_{year}.nc"
    if not nc_path.exists():
        logger.info("Downloading ERA5-Land %d from CDS (covers all months + variables)...", year)
        import os
        cds_url = os.environ.get("CDSAPI_URL", "https://cds.climate.copernicus.eu/api")
        cds_key = os.environ.get("CDSAPI_KEY", "")
        client = cdsapi.Client(url=cds_url, key=cds_key)
        client.retrieve(
            "reanalysis-era5-land-monthly-means",
            {
                "product_type": "monthly_averaged_reanalysis",
                "variable": [
                    "volumetric_soil_water_layer_1",
                    "volumetric_soil_water_layer_2",
                    "volumetric_soil_water_layer_3",
                    "volumetric_soil_water_layer_4",
                ],
                "year": str(year),
                "month": [f"{m:02d}" for m in range(1, 13)],
                "time": "00:00",
                "area": area,
                "data_format": "netcdf",
            },
            str(nc_path),
        )
        # CDS may return a .zip — extract the NetCDF from it
        if nc_path.suffix == ".nc":
            # Check if it's actually a zip file
            import zipfile
            if zipfile.is_zipfile(nc_path):
                zip_path = nc_path.with_suffix(".zip")
                nc_path.rename(zip_path)
                with zipfile.ZipFile(zip_path, "r") as zf:
                    nc_files = [f for f in zf.namelist() if f.endswith(".nc")]
                    if not nc_files:
                        raise ValueError(f"No .nc file found in CDS zip: {zf.namelist()}")
                    zf.extract(nc_files[0], work_dir)
                    extracted = work_dir / nc_files[0]
                    extracted.rename(nc_path)
                zip_path.unlink()

        logger.info("Cached ERA5-Land NetCDF: %s", nc_path.name)

    # Extract requested variable and month
    ds = xr.open_dataset(nc_path, engine="netcdf4")
    try:
        # Variable naming varies: try short name, then long name variants
        var_long_names = {
            "swvl1": "volumetric_soil_water_layer_1",
            "swvl2": "volumetric_soil_water_layer_2",
            "swvl3": "volumetric_soil_water_layer_3",
            "swvl4": "volumetric_soil_water_layer_4",
        }
        var_name = None
        for candidate in (asset_key, var_long_names.get(asset_key, ""), f"{asset_key}_mean"):
            if candidate in ds:
                var_name = candidate
                break
        if var_name is None:
            raise ValueError(
                f"Variable '{asset_key}' not found in ERA5 NetCDF. "
                f"Available: {list(ds.data_vars)}"
            )

        # Select the target month (1-indexed)
        da = ds[var_name]
        if "time" in da.dims:
            # Monthly file: time dim has 12 entries (one per month)
            month_idx = month - 1
            if month_idx < len(da.time):
                da = da.isel(time=month_idx)
            else:
                raise ValueError(f"Month {month} not found in ERA5 NetCDF for year {year}")
        elif "valid_time" in da.dims:
            da = da.isel(valid_time=month - 1)

        data = da.values.astype("float32")

        # Get coordinate arrays
        lats = ds["latitude"].values if "latitude" in ds.coords else ds["lat"].values
        lons = ds["longitude"].values if "longitude" in ds.coords else ds["lon"].values
    finally:
        ds.close()

    # Write single-band GeoTIFF
    tif_path = work_dir / f"{item.id}_{asset_key}.tif"
    height, width = data.shape

    # Compute bounds from coordinate arrays (cell centers → cell edges)
    lat_res = abs(lats[1] - lats[0]) if len(lats) > 1 else 0.1
    lon_res = abs(lons[1] - lons[0]) if len(lons) > 1 else 0.1
    west_edge = float(lons.min()) - lon_res / 2
    east_edge = float(lons.max()) + lon_res / 2
    south_edge = float(lats.min()) - lat_res / 2
    north_edge = float(lats.max()) + lat_res / 2

    transform = from_bounds(west_edge, south_edge, east_edge, north_edge, width, height)

    # Replace out-of-range values with NaN
    data = np.where(np.abs(data) > 10, np.nan, data)

    with rasterio.open(
        tif_path,
        "w",
        driver="GTiff",
        width=width,
        height=height,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=transform,
        nodata=np.nan,
    ) as dst:
        dst.write(data, 1)

    logger.info("Extracted ERA5 %s %d-%02d → %s", asset_key, year, month, tif_path.name)
    return tif_path
