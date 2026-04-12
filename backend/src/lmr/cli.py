from __future__ import annotations

import argparse
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lmr.common.logging import setup_logging
from lmr.config import load_config


def run_ingest(
    config_path: str,
    full_history: bool = False,
    start_date_override: datetime | None = None,
    end_date_override: datetime | None = None,
) -> None:
    config = load_config(config_path)
    config_dir = Path(config_path).parent
    logger = setup_logging(config.global_.log_level)
    logger.info("Starting ingest run (full_history=%s)", full_history)
    if start_date_override:
        logger.info("Date range override: %s to %s", start_date_override, end_date_override or "now")

    from lmr.ingest.stac_client import search_stac
    from lmr.ingest.cog import download_asset, clip_to_bbox, ensure_cog, merge_cogs
    from lmr.ingest.s3 import (
        build_s3_key,
        upload_file,
        get_last_ingested_date,
        get_existing_dates,
        write_manifest,
    )
    from lmr.ingest.zonal import load_boundaries, compute_zonal_stats, write_zonal_stats_json

    bucket = config.global_.s3_bucket
    prefix = config.global_.s3_prefix
    region = config.global_.region
    datasets_results = []

    # Pre-load admin boundaries for zonal stats
    admin_boundaries = {}
    for admin_level in config.admin_levels:
        admin_boundaries[admin_level.level] = {
            "config": admin_level,
            "gdf": load_boundaries(admin_level, config_dir),
        }

    for dataset in config.datasets:
        if not dataset.enabled:
            logger.info("Skipping disabled dataset: %s", dataset.name)
            continue

        logger.info("Processing dataset: %s", dataset.name)

        # Determine start date for incremental ingestion
        start_date = start_date_override
        if start_date is None and not full_history:
            last_date = get_last_ingested_date(bucket, prefix, dataset.name, region)
            if last_date is not None:
                start_date = last_date + timedelta(days=1)

        items = search_stac(config, dataset, start_date=start_date, end_date=end_date_override)

        if not items:
            logger.info("No new items for %s", dataset.name)
            continue

        dataset_result = {
            "name": dataset.name,
            "items_ingested": 0,
            "s3_keys": [],
            "stats_keys": [],
            "stac_items": [],
        }

        # Group items by date so we can merge tiles covering the same date
        items_by_date: dict[str, list] = defaultdict(list)
        for item in items:
            dt = item.datetime or item.common_metadata.start_datetime
            item_date = dt.strftime("%Y_%m_%d") if dt else "unknown"
            items_by_date[item_date].append(item)

        # Skip dates already in S3 to avoid re-downloading
        existing_dates = get_existing_dates(bucket, prefix, dataset.name, region)
        dates_to_process = {
            d: items for d, items in items_by_date.items() if d not in existing_dates
        }
        if len(dates_to_process) < len(items_by_date):
            logger.info(
                "Skipping %d dates already in S3 for %s, %d new dates to process",
                len(items_by_date) - len(dates_to_process),
                dataset.name,
                len(dates_to_process),
            )

        for item_date, date_items in sorted(dates_to_process.items()):
            logger.info(
                "Processing %s date %s (%d items/tiles)",
                dataset.name, item_date, len(date_items),
            )

            try:
                with tempfile.TemporaryDirectory() as work_dir:
                    work_path = Path(work_dir)

                    for asset_key in dataset.assets:
                        # Process all tiles for this date+asset
                        tile_cogs: list[Path] = []

                        for item in date_items:
                            if asset_key not in item.assets:
                                logger.warning(
                                    "Asset %s not found in item %s, skipping",
                                    asset_key, item.id,
                                )
                                continue

                            # Download
                            raw_path = download_asset(item, asset_key, work_path)

                            # Clip to AOI bbox (full Kenya extent)
                            clipped_path = work_path / f"{item.id}_{asset_key}_clipped.tif"
                            clip_to_bbox(raw_path, config.aoi.bbox, clipped_path)

                            # Convert to COG
                            cog_path = work_path / f"{item.id}_{asset_key}_cog.tif"
                            ensure_cog(clipped_path, cog_path, dataset.processing)
                            tile_cogs.append(cog_path)

                        if not tile_cogs:
                            continue

                        # Merge multiple tiles into a single COG if needed
                        if len(tile_cogs) > 1:
                            merged_path = work_path / f"{dataset.name}_{item_date}_{asset_key}_merged.tif"
                            merge_cogs(tile_cogs, merged_path)
                            final_cog = merged_path
                        else:
                            final_cog = tile_cogs[0]

                        # Upload COG to S3
                        s3_key = build_s3_key(
                            template=dataset.s3_key_template,
                            prefix=prefix,
                            dataset_name=dataset.name,
                            date=item_date,
                            asset=asset_key,
                        )
                        upload_file(final_cog, bucket, s3_key, region)
                        dataset_result["s3_keys"].append(s3_key)

                        # Compute and upload zonal stats per admin level
                        for level, boundary_data in admin_boundaries.items():
                            stats = compute_zonal_stats(
                                final_cog,
                                boundary_data["gdf"],
                                boundary_data["config"],
                            )
                            stats_path = work_path / f"admin{level}_{asset_key}_zonal.json"
                            write_zonal_stats_json(stats, stats_path)

                            stats_key = (
                                f"stats/{dataset.name}/{item_date}"
                                f"/admin{level}_{asset_key}.json"
                            )
                            upload_file(stats_path, bucket, stats_key, region)
                            dataset_result["stats_keys"].append(stats_key)

                    dataset_result["items_ingested"] += len(date_items)
                    dataset_result["stac_items"].extend(item.id for item in date_items)
            except Exception:
                logger.exception(
                    "Failed to process %s date %s, skipping to next date",
                    dataset.name, item_date,
                )
                continue

        datasets_results.append(dataset_result)
        logger.info(
            "Dataset %s: ingested %d items across %d dates",
            dataset.name,
            dataset_result["items_ingested"],
            len(items_by_date),
        )

    # Write manifest
    if datasets_results:
        manifest_uri = write_manifest(bucket, datasets_results, region)
        logger.info("Ingest complete. Manifest: %s", manifest_uri)
    else:
        logger.info("No new data ingested.")


def main():
    parser = argparse.ArgumentParser(description="LMR Data Platform")
    parser.add_argument(
        "--mode",
        required=True,
        choices=["ingest", "serve", "infer", "feature-extract"],
        help="Run mode: ingest, serve, infer, or feature-extract",
    )
    parser.add_argument(
        "--config",
        default="/app/config/datasets.yaml",
        help="Path to datasets.yaml config file",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for serve mode",
    )
    parser.add_argument(
        "--full-history",
        action="store_true",
        help="Ingest all available history instead of incremental",
    )
    parser.add_argument(
        "--start-date",
        type=str,
        default=None,
        help="Override start date for ingest (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--end-date",
        type=str,
        default=None,
        help="Override end date for ingest (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--scheme",
        type=str,
        default=None,
        choices=["biannual", "quadseasonal", "monthly"],
        help="Season scheme for infer mode",
    )
    parser.add_argument(
        "--time-start",
        type=str,
        default=None,
        help="Start month for feature-extract mode (YYYY-MM)",
    )
    parser.add_argument(
        "--time-end",
        type=str,
        default=None,
        help="End month for feature-extract mode (YYYY-MM)",
    )

    args = parser.parse_args()

    if args.mode == "ingest":
        start_dt = None
        end_dt = None
        if args.start_date:
            start_dt = datetime.strptime(args.start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if args.end_date:
            end_dt = datetime.strptime(args.end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        run_ingest(
            args.config,
            full_history=args.full_history,
            start_date_override=start_dt,
            end_date_override=end_dt,
        )
    elif args.mode == "serve":
        try:
            import uvicorn
            from lmr.serve.app import create_app

            app = create_app(args.config)
            uvicorn.run(app, host="0.0.0.0", port=args.port)
        except ImportError:
            print("Serve mode dependencies not available", file=sys.stderr)
            sys.exit(1)
    elif args.mode == "feature-extract":
        from lmr.config import load_config
        from lmr.infer.feature_extract import run_feature_extraction

        if not args.time_start or not args.time_end:
            parser.error("--time-start and --time-end are required for feature-extract mode")

        config = load_config(args.config)
        run_feature_extraction(config, args.time_start, args.time_end)

    elif args.mode == "infer":
        from lmr.config import load_config
        from lmr.infer.pipeline import run_inference_pipeline

        if not args.scheme:
            parser.error("--scheme is required for infer mode")

        config = load_config(args.config)
        run_inference_pipeline(config, args.scheme)


if __name__ == "__main__":
    main()
