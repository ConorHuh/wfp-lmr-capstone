"""
Feature extraction entry point — called by CLI --mode feature-extract.

Configures the ward_features module with AppConfig settings and runs the
full ward-level satellite feature extraction pipeline.
"""

from __future__ import annotations

from lmr.common.logging import setup_logging
from lmr.config import AppConfig


# Large collections not used by the 29 model features — skip to avoid OOM.
DEFAULT_SKIP_COLLECTIONS = {"s1_vv", "s1_vh", "s2_red", "s2_nir", "s2_swir1"}

# Marsabit County bounding box (default spatial filter).
MARSABIT_BBOX = (36.0, 1.2, 39.0, 4.5)


def run_feature_extraction(
    config: AppConfig,
    time_start: str,
    time_end: str,
) -> dict[str, str]:
    """
    Run ward-level satellite feature extraction.

    Parameters
    ----------
    config : AppConfig
        Full application config.
    time_start : str
        Start month in YYYY-MM format.
    time_end : str
        End month in YYYY-MM format.

    Returns
    -------
    dict mapping scheme name to S3 URI of output parquet.
    """
    logger = setup_logging(config.global_.log_level)
    inference = config.inference

    logger.info("Feature extraction: %s → %s", time_start, time_end)

    # Configure the ward_features module with config-driven S3 paths.
    from lmr.infer import ward_features

    output_bucket = inference.output_bucket
    output_prefix = f"inference/ward_features_{time_start}_{time_end}"

    ward_features.configure(
        source_data_bucket=inference.source_data_bucket,
        source_data_prefix=inference.source_data_prefix,
        ward_boundaries_key=inference.ward_boundaries_s3_key,
        output_bucket=output_bucket,
    )

    logger.info("Source: s3://%s/%s", inference.source_data_bucket, inference.source_data_prefix)
    logger.info("Output: s3://%s/%s", output_bucket, output_prefix)

    return ward_features.main(
        time_start=time_start,
        time_end=time_end,
        output_prefix=output_prefix,
        scheme=None,
        bbox=MARSABIT_BBOX,
        n_points=inference.n_sample_points,
        skip_collections=DEFAULT_SKIP_COLLECTIONS,
    )
