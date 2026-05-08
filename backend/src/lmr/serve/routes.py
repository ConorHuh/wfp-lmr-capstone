from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from lmr.serve.s3 import (
    fetch_json_from_s3,
    generate_presigned_url,
    list_dataset_dates,
    resolve_s3_key,
    s3_key_exists,
)

# Deterministic shapeID → enriched KE pcode mapping for 45 Marsabit-region wards.
# Built from the enriched admin boundary file (frontend/kenya_config/admin_boundaries.geojson)
# which contains both shapeID (geoBoundaries) and pcode (enriched KE-format) for each ward.
_SHAPEID_TO_PCODE = {
    "90231094B10172771319566": "KE0037",   # Arbajahan
    "90231094B72025053214099": "KE0034",   # Angata Nanyokie
    "90231094B72746936744105": "KE0047",   # Baawa
    "90231094B15364119625366": "KE0123",   # Butiye
    "90231094B50144609273926": "KE0149",   # Chari
    "90231094B87449600047850": "KE0179",   # Cherab
    "90231094B5573987448146": "KE0212",    # Dukana
    "90231094B188332930189": "KE_El-Barta",  # El-Barta
    "90231094B59854911915600": "KE0313",   # Golbo
    "90231094B31992479059302": "KE0360",   # Illeret
    "90231094B76245390051388": "KE0427",   # Kalapata
    "90231094B83090338457236": "KE0432",   # Kalokol
    "90231094B38147959739491": "KE_Kang'Atotha",  # Kang'Atotha
    "90231094B29136219307336": "KE0471",   # Kapedo/Napeitom
    "90231094B1337157862833": "KE0505",    # Karare
    "90231094B53566041103404": "KE0509",   # Kargi/South Horr
    "90231094B92569591317187": "KE0530",   # Katilia
    "90231094B26546531882406": "KE0548",   # Kerio Delta
    "90231094B29407240705955": "KE0676",   # Korondile
    "90231094B8071195372928": "KE0677",    # Korr/Ngurunit
    "90231094B40010710391655": "KE_Laisamis",  # Laisamis
    "90231094B49006740348855": "KE0710",   # Lake Zone
    "90231094B46732073330793": "KE_Lakoley South/Basir",  # Lakoley South/Basir
    "90231094B9458721871414": "KE_Log Logo",  # Log Logo
    "90231094B56895889347103": "KE0743",   # Loiyangalani
    "90231094B81821065805517": "KE0740",   # Marsabit Central
    "90231094B13029526517882": "KE0747",   # Lokori/Kochodin
    "90231094B72561831501862": "KE0753",   # Loosuk
    "90231094B43646863097503": "KE0796",   # Maikona
    "90231094B91795569302668": "KE1022",   # Nachola
    "90231094B62369780661110": "KE1052",   # Ndoto
    "90231094B74485578386276": "KE1086",   # North Horr
    "90231094B16944892781111": "KE1126",   # Nyiro
    "90231094B82073157238535": "KE1136",   # Obbu
    "90231094B12946353808942": "KE1150",   # Poro
    "90231094B90774889939565": "KE_Ribkwo",  # Ribkwo
    "90231094B10583428946278": "KE1192",   # Sagante/Jaldesa
    "90231094B92349644184476": "KE1255",   # Sololo
    "90231094B4411108441834": "KE1211",    # Sericho
    "90231094B58027541120438": "KE1319",   # Tirioko
    "90231094B36492978778614": "KE1338",   # Turbi
    "90231094B52894736113617": "KE1351",   # Uran
    "90231094B82036966044005": "KE1373",   # Wamba East
    "90231094B35618945672487": "KE1374",   # Wamba North
    "90231094B52652220782701": "KE1384",   # Waso
    "90231094B33506971670808": "KE1207",   # Sekerr
}
_RISK_LEVEL_ENCODING = {"Normal": 0, "Concerning": 1, "Critical": 2}

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


# ── Date normalisation for the predictions route ──────────────────────────
#
# Postprocess (backend/src/lmr/infer/postprocess.py:189) writes every
# scheme's predictions under a single canonical S3 prefix:
#
#   predictions/livestock-mortality/{YYYY_MM_DD}/ward_predictions.geojson
#
# where YYYY_MM_DD is derived from the season's end date for biannual /
# quadseasonal, or directly from year+month for monthly. The scheme is
# implicit in WHICH dates exist for that scheme — the frontend already
# tracks that via per-scheme `dates: [...]` arrays in layers.json.
#
# So the route doesn't need to derive a scheme-specific folder name from
# the date — it just needs to normalise the incoming date to YYYY_MM_DD
# and look it up directly. PRISM hands us dates with hyphen separators
# (e.g. "2026-04-01"); accept either separator for robustness.

def _normalize_date_key(iso_date: str) -> str:
    """Convert '2026-04-01' or '2026_04_01' → '2026_04_01' (postprocess format).

    Raises HTTPException(400) on malformed input rather than crashing the
    route handler with an IndexError further down.
    """
    parts = iso_date.replace("-", "_").split("_")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid date '{iso_date}': expected YYYY-MM-DD or YYYY_MM_DD",
        )
    y, m, d = parts
    return f"{y}_{m.zfill(2)}_{d.zfill(2)}"


def _flatten_prediction_properties(props: dict, feature_labels: dict) -> dict:
    """Convert ward prediction GeoJSON properties into a flat dict for Prism.

    Resolves shapeID-format pcode to enriched KE pcode via deterministic lookup.
    Formats numbers and maps feature names to human-readable labels.
    """
    # Map shapeID → KE pcode
    raw_pcode = props.get("pcode", "")
    pcode = _SHAPEID_TO_PCODE.get(raw_pcode, raw_pcode)

    mean_lr = props.get("mean_predicted_loss_ratio")
    median_lr = props.get("median_predicted_loss_ratio")
    confidence = props.get("confidence")

    flat: dict = {
        "pcode": pcode,
        "mean_predicted_loss_ratio": round(mean_lr, 4) if mean_lr is not None else None,
        "median_predicted_loss_ratio": round(median_lr, 4) if median_lr is not None else None,
        "confidence": round(confidence, 2) if confidence is not None else None,
        "risk_level": props.get("risk_level"),
        "risk_level_encoded": _RISK_LEVEL_ENCODING.get(props.get("risk_level")),
        "n_observations": props.get("n_observations"),
    }
    # Flatten top_features with readable labels
    for i, feat in enumerate(props.get("top_features", [])[:5], start=1):
        raw_name = feat.get("feature", "")
        label_cfg = feature_labels.get(raw_name)
        label = label_cfg.short if label_cfg else raw_name
        flat[f"top_feature_{i}"] = label
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


_VALID_MODEL_TYPES = {
    "livestock-mortality-monthly",
    "livestock-mortality-biannual",
    "livestock-mortality-quadseasonal",
    # Accept the un-suffixed alias too — for direct API consumers
    "livestock-mortality",
}


@router.get("/predictions/{model_type}/{period}")
async def prediction_ward_data(request: Request, model_type: str, period: str):
    """Serve ward prediction data for PRISM admin_level_data layers.

    model_type: 'livestock-mortality-monthly', 'livestock-mortality-biannual',
                or 'livestock-mortality-quadseasonal'
    period: e.g. '2026-04-01' (PRISM hyphen format) or '2026_04_01'

    Postprocess writes every scheme's predictions under a single shared S3
    prefix `predictions/livestock-mortality/{YYYY_MM_DD}/`, with the date
    encoding the season (end-of-period date) implicitly. The model_type
    arg is validated for API correctness but not used for S3 lookup —
    each frontend layer's `dates: [...]` array already restricts requests
    to dates that exist for that scheme.
    """
    if model_type not in _VALID_MODEL_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid model_type: {model_type}. Must be one of "
                + ", ".join(sorted(_VALID_MODEL_TYPES))
            ),
        )

    config = request.app.state.config
    bucket = config.global_.s3_bucket
    region = config.global_.region
    predictions_prefix = config.serve.predictions_prefix
    feature_labels = config.feature_labels

    date_key = _normalize_date_key(period)
    key = f"{predictions_prefix}/livestock-mortality/{date_key}/ward_predictions.geojson"
    geojson = fetch_json_from_s3(bucket, key, region)

    if geojson is None:
        raise HTTPException(
            status_code=404,
            detail=f"No predictions found for {model_type}/{period} at s3://{bucket}/{key}",
        )

    features = geojson.get("features", [])
    data = [_flatten_prediction_properties(f["properties"], feature_labels) for f in features]
    return {"DataList": data}
