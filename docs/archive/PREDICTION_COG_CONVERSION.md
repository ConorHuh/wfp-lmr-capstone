# Prediction File Conversion — GeoTIFF to COG

This document explains how the team's prediction output files were converted and loaded into the LMR platform for end-to-end visualization in Prism.

---

## Source Files

The SageMaker pipeline team produced two prediction output files stored at:

```
s3://amazon-sagemaker-575108933641-us-east-1-c422b90ce861/
  dzd-ayr06tncl712p3/5t7l23o0xvt99j/dev/pipeline-artifacts/
  lmr-xgb-training-pipeline/dataset/v2/
    ward_predictions.geojson   (1.3 MB)
    ward_predictions.tif       (191 KB)
```

### ward_predictions.geojson

A GeoJSON FeatureCollection with 16 features (one per Marsabit County ward at ADM3 level). Each feature contains:

| Property | Example | Description |
|----------|---------|-------------|
| `ADM3_EN` | "Dukana" | Ward name |
| `ADM3_PCODE` | "KE0212" | Admin code |
| `ADM2_EN` / `ADM1_EN` | "Marsabit" / "Eastern" | Parent admin levels |
| `mean_predicted_loss_ratio` | 0.022695 | Mean predicted livestock mortality |
| `median_predicted_loss_ratio` | 0.022643 | Median predicted livestock mortality |
| `max_predicted_loss_ratio` | 0.022902 | Max predicted livestock mortality |
| `n_observations` | 5 | Number of observations in ward |
| `risk_level` | "Normal" or "Critical" | Classification bucket |
| `confidence` | 0.9998 | Model confidence |
| `top_features` | [{feature, importance}, ...] | Top 5 feature importances |

This file is preserved as-is for potential future use as a vector layer but is not currently served by TiTiler (which serves raster tiles only).

### ward_predictions.tif

A standard GeoTIFF (not a COG) with these properties:

| Property | Value |
|----------|-------|
| CRS | EPSG:4326 |
| Size | 363 x 319 pixels |
| Bands | 3 (float32) |
| NoData | -9999.0 |
| Bounds | 35.34°E–38.96°E, 1.26°N–4.45°N (Marsabit County) |
| Tiled | No (stripped layout, 1-row blocks) |
| Overviews | None |
| Compression | LZW |

The three bands are ward-level rasterizations (each pixel assigned its ward's value):

- **Band 1 — Risk Level**: Categorical. Values are `0` (Normal) or `2` (Critical). 16 wards mapped to 2 categories.
- **Band 2 — Confidence**: Continuous, 0.794–1.0. 15 unique values across 16 wards.
- **Band 3 — Predicted Loss Ratio**: Continuous, 0.013–0.032. The mean predicted livestock mortality ratio per ward.

---

## Why Conversion Was Needed

TiTiler serves map tiles by making HTTP range requests to read just the bytes it needs from a raster file in S3. This requires the file to be a **Cloud Optimized GeoTIFF (COG)** — a GeoTIFF with two specific features:

1. **Tiling**: Pixels are stored in 256x256 blocks instead of row-by-row strips. This allows TiTiler to read a specific spatial region without scanning the entire file.

2. **Overviews**: Pre-computed downsampled versions of the data (like image thumbnails at 1/2, 1/4 resolution). When the user is zoomed out, TiTiler reads from an overview instead of the full-resolution data, which is much faster.

The source `ward_predictions.tif` had neither — it used stripped layout (1-row blocks) and no overviews. Requesting tiles from it would require reading large portions of the file for each tile.

---

## Conversion Process

The conversion was done with rasterio (Python):

```python
import rasterio
from rasterio.enums import Resampling

# Read the original GeoTIFF
with rasterio.open('ward_predictions.tif') as src:
    data = src.read()
    profile = src.profile.copy()

# Update the profile for COG format
profile.update(
    driver='GTiff',
    tiled=True,          # 256x256 pixel blocks instead of strips
    blockxsize=256,
    blockysize=256,
    compress='deflate',  # Compression (smaller file, still supports range reads)
    predictor=2,         # Horizontal differencing predictor (improves compression)
)

# Write the COG
with rasterio.open('ward_predictions_cog.tif', 'w', **profile) as dst:
    dst.write(data)

    # Build overviews at 2x and 4x downsampling
    # nearest-neighbor resampling preserves the discrete ward boundaries
    dst.build_overviews([2, 4], Resampling.nearest)
    dst.update_tags(ns='rio_overview', resampling='nearest')

    # Label the bands for clarity
    dst.set_band_description(1, 'risk_level')
    dst.set_band_description(2, 'confidence')
    dst.set_band_description(3, 'predicted_loss_ratio')
```

Result:

| Property | Before | After |
|----------|--------|-------|
| Tiled | No (1-row strips) | Yes (256x256 blocks) |
| Overviews | None | 2x, 4x |
| Compression | LZW | DEFLATE |
| Block shape | (1, 363) | (256, 256) |
| File size | 191 KB | 20 KB |
| Band descriptions | None | risk_level, confidence, predicted_loss_ratio |

The file got smaller because DEFLATE with predictor=2 compresses the ward-level data (many repeated values within each ward polygon) more efficiently than LZW.

---

## Upload to S3

The converted COG and the original GeoJSON were uploaded to the operational S3 bucket under the predictions prefix:

```
s3://lmr-data-cogs-dev/
  predictions/livestock-mortality/2026-04-01/
    prediction.tif              (20 KB, COG)
    ward_predictions.geojson    (1.3 MB)
```

The date `2026-04-01` was chosen as a label for this test prediction set. It does not correspond to a specific satellite observation date — it is simply a timestamp identifier for this batch of predictions. In production, the inference pipeline would write predictions with the date of the satellite features used as input.

---

## Prism Layer Configuration

Two layers were added to `prism/kenya_config/layers.json` to visualize the prediction COG:

### Livestock Mortality Risk (Band 3 — continuous loss ratio)

```json
"predictions_mortality": {
  "title": "Livestock Mortality Risk (Ward-level)",
  "type": "static_raster",
  "base_url": "https://d31fsorf4vwo9f.cloudfront.net/cog/tiles/WebMercatorQuad/{z}/{x}/{y}?url=s3://lmr-data-cogs-dev/predictions/livestock-mortality/{YYYY-MM-DD}/prediction.tif&bidx=3&rescale=0.01,0.1&colormap_name=rdylgn_r&nodata=-9999",
  "dates": ["2026-04-01"]
}
```

Key parameters:
- `bidx=3` — read band 3 (predicted loss ratio)
- `rescale=0.01,0.1` — map the value range 1%–10% to the 0–255 display range
- `colormap_name=rdylgn_r` — reversed red-yellow-green (green=low risk, red=high risk)
- `nodata=-9999` — treat -9999 pixels as transparent

### Risk Category (Band 1 — categorical Normal/Critical)

```json
"predictions_risk_level": {
  "title": "Livestock Mortality Risk Category",
  "type": "static_raster",
  "base_url": "https://d31fsorf4vwo9f.cloudfront.net/cog/tiles/WebMercatorQuad/{z}/{x}/{y}?url=s3://lmr-data-cogs-dev/predictions/livestock-mortality/{YYYY-MM-DD}/prediction.tif&bidx=1&rescale=0,2&colormap_name=rdylgn_r&nodata=-9999",
  "dates": ["2026-04-01"]
}
```

- `bidx=1` — read band 1 (risk level: 0=Normal, 2=Critical)
- `rescale=0,2` — map the 0–2 range to the full colormap

---

## How It Works End-to-End

1. User opens Prism, selects "Livestock Mortality Risk" layer, date 2026-04-01
2. Prism substitutes `{YYYY-MM-DD}` → `2026-04-01` in the tile URL
3. MapLibre requests tiles for the visible viewport from CloudFront
4. CloudFront forwards to the ALB → Fargate serve container
5. TiTiler reads just the needed bytes from the COG in S3 via HTTP range requests
6. TiTiler applies rescaling + colormap, returns a PNG tile
7. Browser composites the tile onto the map

---

## For Future Predictions

When the inference pipeline produces new prediction files, the process is:

1. Ensure the GeoTIFF is a COG (tiled + overviews). If the pipeline outputs a standard GeoTIFF, convert it using the process above.
2. Upload to `s3://lmr-data-cogs-dev/predictions/livestock-mortality/{YYYY-MM-DD}/prediction.tif`
3. Add the new date to the `dates` array in `prism/kenya_config/layers.json`
4. Run `scripts/deploy-all.sh --skip-backend` to rebuild and deploy the frontend

Alternatively, once the `/predict` endpoint is implemented (see `docs/PLAN_DECOUPLE_SAGEMAKER.md`), the serve container will produce COGs directly and write them to S3, and the `/collections` API endpoint can serve available dates dynamically without hardcoding them in layers.json.
