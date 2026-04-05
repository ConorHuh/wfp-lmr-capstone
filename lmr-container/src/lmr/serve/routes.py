from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from lmr.serve.s3 import generate_presigned_url, list_dataset_dates, resolve_s3_key, s3_key_exists

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/raster_geotiff")
async def raster_geotiff(request: Request, collection: str, date: str, asset: str = "ndvi"):
    config = request.app.state.config
    bucket = config.global_.s3_bucket
    prefix = config.global_.s3_prefix
    region = config.global_.region
    expiry = config.serve.presigned_url_expiry_seconds

    _, key = resolve_s3_key(bucket, prefix, collection, date, asset)

    if not s3_key_exists(bucket, key, region):
        raise HTTPException(status_code=404, detail=f"COG not found: {key}")

    url = generate_presigned_url(bucket, key, region, expiry)
    return {"url": url, "bucket": bucket, "key": key, "expires_in": expiry}


@router.get("/collections")
async def collections(request: Request):
    config = request.app.state.config
    bucket = config.global_.s3_bucket
    prefix = config.global_.s3_prefix
    region = config.global_.region

    result = []
    for dataset in config.datasets:
        if not dataset.enabled:
            continue
        dates = list_dataset_dates(bucket, prefix, dataset.name, region)
        result.append({
            "name": dataset.name,
            "collection": dataset.collection,
            "assets": dataset.assets,
            "available_dates": dates,
            "count": len(dates),
        })
    return {"collections": result}


@router.get("/latest")
async def latest(request: Request, model: str = "livestock-mortality"):
    config = request.app.state.config
    bucket = config.global_.s3_bucket
    region = config.global_.region
    expiry = config.serve.presigned_url_expiry_seconds
    predictions_prefix = config.serve.predictions_prefix

    # Find the latest prediction date folder
    dates = list_dataset_dates(bucket, predictions_prefix, model, region)
    if not dates:
        raise HTTPException(status_code=404, detail=f"No predictions found for model: {model}")

    latest_date = dates[-1]
    key = f"{predictions_prefix}/{model}/{latest_date}/prediction.tif"

    if not s3_key_exists(bucket, key, region):
        raise HTTPException(status_code=404, detail=f"Prediction COG not found: {key}")

    url = generate_presigned_url(bucket, key, region, expiry)
    return {
        "url": url,
        "model": model,
        "date": latest_date,
        "bucket": bucket,
        "key": key,
        "expires_in": expiry,
    }


@router.get("/tile_url")
async def tile_url(request: Request, collection: str, date: str, asset: str = "ndvi"):
    config = request.app.state.config
    bucket = config.global_.s3_bucket
    prefix = config.global_.s3_prefix

    _, key = resolve_s3_key(bucket, prefix, collection, date, asset)
    s3_url = f"s3://{bucket}/{key}"

    # Build TiTiler tile URL template — TiTiler is mounted at /cog
    base = str(request.base_url).rstrip("/")
    tile_template = (
        f"{base}/cog/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}@1x"
        f"?url={s3_url}"
    )
    return {
        "tile_url": tile_template,
        "s3_url": s3_url,
        "collection": collection,
        "date": date,
        "asset": asset,
    }
