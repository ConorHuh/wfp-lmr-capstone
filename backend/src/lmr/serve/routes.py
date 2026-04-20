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


# ISO date (YYYY_MM_DD from PRISM) → S3 folder name mappings
_MONTH_NAMES = {
    "01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr",
    "05": "May", "06": "Jun", "07": "Jul", "08": "Aug",
    "09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec",
}

# Biannual season mapping: LRLD starts ~April, SRSD starts ~October
_BIANNUAL_SEASON = {"04": "LRLD", "10": "SRSD"}


def _iso_to_monthly_folder(iso_date: str) -> str:
    """Convert '2019_01_01' → '2019Jan'."""
    parts = iso_date.split("_")
    return f"{parts[0]}{_MONTH_NAMES[parts[1]]}"


def _iso_to_biannual_folder(iso_date: str) -> str:
    """Convert '2019_04_01' → '2019LRLD', '2019_10_01' → '2019SRSD'."""
    parts = iso_date.split("_")
    season = _BIANNUAL_SEASON.get(parts[1])
    if season is None:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid biannual date {iso_date}: month must be 04 (LRLD) or 10 (SRSD)",
        )
    return f"{parts[0]}{season}"


# Quadseasonal season mapping:
#   LRS     = Mar–Jun, LRS_dry = Jul–Sep
#   SRS     = Oct–Dec, SRS_dry = Jan–Feb
# Year label attaches to the year the SRS starts (e.g. 2018SRS_dry = Jan–Feb 2019).
_QUADSEASONAL = {
    "2018_10": "2018SRS", "2018_11": "2018SRS", "2018_12": "2018SRS",
    "2019_01": "2018SRS_dry", "2019_02": "2018SRS_dry",
    "2019_03": "2019LRS", "2019_04": "2019LRS", "2019_05": "2019LRS", "2019_06": "2019LRS",
    "2019_07": "2019LRS_dry", "2019_08": "2019LRS_dry", "2019_09": "2019LRS_dry",
    "2019_10": "2019SRS", "2019_11": "2019SRS", "2019_12": "2019SRS",
    "2020_01": "2019SRS_dry", "2020_02": "2019SRS_dry",
    "2020_03": "2020LRS", "2020_04": "2020LRS", "2020_05": "2020LRS", "2020_06": "2020LRS",
    "2020_07": "2020LRS_dry", "2020_08": "2020LRS_dry", "2020_09": "2020LRS_dry",
    "2020_10": "2020SRS", "2020_11": "2020SRS", "2020_12": "2020SRS",
}


def _iso_to_quadseasonal_folder(iso_date: str) -> str:
    """Convert '2020_05_01' → '2020LRS'."""
    parts = iso_date.split("_")
    key = f"{parts[0]}_{parts[1]}"
    folder = _QUADSEASONAL.get(key)
    if folder is None:
        raise HTTPException(
            status_code=400,
            detail=f"No quadseasonal prediction for {iso_date} (valid range: 2018-10 to 2020-12)",
        )
    return folder


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


@router.get("/predictions/{model_type}/{period}")
async def prediction_ward_data(request: Request, model_type: str, period: str):
    """Serve ward prediction data for PRISM admin_level_data layers.

    model_type: 'livestock-mortality-monthly', 'livestock-mortality-biannual',
                or 'livestock-mortality-quadseasonal'
    period: e.g. '2019_01_01', '2019_04_01', '2020_05_01'
    """
    config = request.app.state.config
    bucket = config.global_.s3_bucket
    region = config.global_.region
    predictions_prefix = config.serve.predictions_prefix
    feature_labels = config.feature_labels

    if model_type == "livestock-mortality-monthly":
        folder = _iso_to_monthly_folder(period)
    elif model_type == "livestock-mortality-biannual":
        folder = _iso_to_biannual_folder(period)
    elif model_type == "livestock-mortality-quadseasonal":
        folder = _iso_to_quadseasonal_folder(period)
    else:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid model_type: {model_type}. Must be one of "
                "'livestock-mortality-monthly', 'livestock-mortality-biannual', "
                "'livestock-mortality-quadseasonal'"
            ),
        )

    key = f"{predictions_prefix}/{model_type}/{folder}/ward_predictions.geojson"
    geojson = fetch_json_from_s3(bucket, key, region)

    if geojson is None:
        raise HTTPException(status_code=404, detail=f"No predictions found for {model_type}/{period}")

    features = geojson.get("features", [])
    data = [_flatten_prediction_properties(f["properties"], feature_labels) for f in features]
    return {"DataList": data}
