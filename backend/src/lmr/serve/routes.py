from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from lmr.serve.s3 import (
    fetch_json_from_s3,
    generate_presigned_url,
    list_dataset_dates,
    resolve_s3_key,
    s3_key_exists,
)

# Ward name mapping: prediction GeoJSON ADM3_EN → boundary pcode
# Covers all 45 Marsabit-region wards used in livestock mortality predictions.
# Wards with KE_ prefix have synthetic pcodes matching the enriched boundary file.
_WARD_PCODE_MAP = {
    "Angata Nanyokie": "KE0034",
    "Arbajahan": "KE0037",
    "Baawa": "KE0047",
    "Butiye": "KE0123",
    "Chari": "KE0149",
    "Cherab": "KE0179",
    "Dukana": "KE0212",
    "El-Barta": "KE_El-Barta",
    "Golbo": "KE0313",
    "Illeret": "KE0360",
    "Kalapata": "KE0427",
    "Kalokol": "KE0432",
    "Kang'Atotha": "KE_Kang'Atotha",
    "Kapedo/Napeitom": "KE0471",
    "Karare": "KE0505",
    "Kargi/South Horr": "KE0509",
    "Katilia": "KE0530",
    "Kerio Delta": "KE0548",
    "Korondile": "KE0676",
    "Korr/Ngurunit": "KE0677",
    "Laisamis": "KE_Laisamis",
    "Lake Zone": "KE0710",
    "Lakoley South/Basir": "KE_Lakoley South/Basir",
    "Log Logo": "KE_Log Logo",
    "Loiyangalani": "KE0743",
    "Logologo": "KE0740",
    "Lokori/Kochodin": "KE0747",
    "Loosuk": "KE0753",
    "Maikona": "KE0796",
    "Marsabit Central": "KE0740",
    "Marsabet central": "KE0740",
    "Nachola": "KE1022",
    "Ndoto": "KE1052",
    "North Horr": "KE1086",
    "Nyiro": "KE1126",
    "Obbu": "KE1136",
    "Poro": "KE1150",
    "Ribkwo": "KE_Ribkwo",
    "Sagante/Jaldesa": "KE1192",
    "Sagante/Jaldessa": "KE1192",
    "Sekerr": "KE1207",
    "Sericho": "KE1211",
    "Sololo": "KE1255",
    "Tirioko": "KE1319",
    "Turbi": "KE1338",
    "Uran": "KE1351",
    "Wamba East": "KE1373",
    "Wamba North": "KE1374",
    "Waso": "KE1384",
}

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
    # Try new naming convention first, fall back to legacy
    key = f"{predictions_prefix}/{model}/{latest_date}/ward_predictions.tif"
    if not s3_key_exists(bucket, key, region):
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


def _flatten_prediction_properties(props: dict) -> dict:
    """Convert ward prediction GeoJSON properties into a flat dict for Prism."""
    # New GeoJSON includes pcode directly; fall back to name→pcode map for legacy files
    pcode = props.get("pcode")
    if pcode is None:
        ward_name = props.get("ADM3_EN", "")
        pcode = _WARD_PCODE_MAP.get(ward_name, f"KE_{ward_name}")
    flat = {
        "pcode": pcode,
        "mean_predicted_loss_ratio": props.get("mean_predicted_loss_ratio"),
        "median_predicted_loss_ratio": props.get("median_predicted_loss_ratio"),
        "max_predicted_loss_ratio": props.get("max_predicted_loss_ratio"),
        "confidence": props.get("confidence"),
        "risk_level": props.get("risk_level"),
        "n_observations": props.get("n_observations"),
    }
    # Flatten top_features array into individual fields
    for i, feat in enumerate(props.get("top_features", [])[:5], start=1):
        flat[f"top_feature_{i}"] = feat.get("feature", "")
        flat[f"top_feature_{i}_importance"] = round(feat.get("importance", 0), 4)
    return flat


@router.get("/predictions/livestock-mortality/dates")
async def prediction_dates(request: Request):
    config = request.app.state.config
    bucket = config.global_.s3_bucket
    region = config.global_.region
    predictions_prefix = config.serve.predictions_prefix

    dates = list_dataset_dates(bucket, predictions_prefix, "livestock-mortality", region)
    return {"dates": dates}


@router.get("/predictions/livestock-mortality/{date}")
async def prediction_ward_data(request: Request, date: str):
    config = request.app.state.config
    bucket = config.global_.s3_bucket
    region = config.global_.region
    predictions_prefix = config.serve.predictions_prefix

    key = f"{predictions_prefix}/livestock-mortality/{date}/ward_predictions.geojson"
    geojson = fetch_json_from_s3(bucket, key, region)

    if geojson is None:
        raise HTTPException(status_code=404, detail=f"No predictions found for date: {date}")

    features = geojson.get("features", [])
    data = [_flatten_prediction_properties(f["properties"]) for f in features]
    return {"DataList": data}
