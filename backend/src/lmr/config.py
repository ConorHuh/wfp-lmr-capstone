from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class GlobalConfig(BaseModel):
    region: str = "us-east-1"
    schedule_interval_days: int = 8
    s3_bucket: str
    s3_prefix: str = "ingested"
    log_level: str = "INFO"


class AOIConfig(BaseModel):
    name: str
    bbox: list[float] = Field(min_length=4, max_length=4)


class AdminFilterConfig(BaseModel):
    field: str
    values: list[str]


class AdminLevelConfig(BaseModel):
    level: int
    name: str
    boundary_file: str
    id_field: str
    name_field: str
    filter: AdminFilterConfig | None = None


class STACConfig(BaseModel):
    catalog_url: str
    requires_signing: bool = True


class TemporalConfig(BaseModel):
    lookback_days: int = 16


class ProcessingConfig(BaseModel):
    output_format: str = "cog"
    resolution_m: int = 30
    crs: str = "EPSG:4326"


class DatasetConfig(BaseModel):
    name: str
    enabled: bool = True
    collection: str
    assets: list[str]
    query_filters: dict[str, Any] = Field(default_factory=dict)
    temporal: TemporalConfig = TemporalConfig()
    processing: ProcessingConfig = ProcessingConfig()
    s3_key_template: str = "{prefix}/{dataset}/{date}/{asset}.tif"


class ServeConfig(BaseModel):
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])
    presigned_url_expiry_seconds: int = 3600
    predictions_prefix: str = "predictions"


class InferenceConfig(BaseModel):
    enabled: bool = False
    model_name: str = "livestock-mortality"
    model_s3_prefix: str = ""
    ward_boundaries_s3_key: str = ""
    output_bucket: str = "lmr-data-cogs"
    output_prefix: str = "predictions/livestock-mortality"
    boundary_file: str = "boundaries/kenya_wards.geojson"
    schemes: list[str] = Field(default_factory=lambda: ["biannual", "quadseasonal", "monthly"])
    feature_window_months: int = 36
    n_sample_points: int = 9
    source_data_bucket: str = ""
    source_data_prefix: str = ""


class AppConfig(BaseModel):
    global_: GlobalConfig = Field(alias="global")
    aoi: AOIConfig
    admin_levels: list[AdminLevelConfig] = Field(default_factory=list)
    stac: STACConfig
    datasets: list[DatasetConfig]
    serve: ServeConfig = ServeConfig()
    inference: InferenceConfig = InferenceConfig()

    model_config = {"populate_by_name": True}


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    with path.open() as f:
        raw = yaml.safe_load(f)
    return AppConfig.model_validate(raw)
