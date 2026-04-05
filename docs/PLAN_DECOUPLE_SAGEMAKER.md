# Plan: Decouple Inference from SageMaker

## Goal

Remove all SageMaker runtime dependencies so the LMR platform runs inference entirely within the Fargate serve container. SageMaker becomes a training/experimentation tool only — no SageMaker endpoints, Lambda triggers, or cross-bucket IAM needed at runtime.

## Cost Impact

Eliminates:
- SageMaker endpoint costs (~$36-73/month)
- NAT Gateway dependency (~$43/month)
- Lambda trigger infrastructure
- Cross-bucket IAM complexity

## Current Model Details

From analysis of `pipeline/sagemaker_pipelines_mlflow_updated.ipynb` and `pipeline/preprocess.py`:

- **Model**: Single XGBoost (XGBRegressor, 50 trees, max_depth=5, eta=0.2)
- **Task**: Regression (`reg:squarederror`) — livestock mortality risk
- **Size**: ~5-10 MB total (model ~1-5 MB, scaler <100 KB, medians <5 KB)
- **30 features**: 7 environmental vars (soil, ppt, pdsi, vpd, ndvi, lai, lst) + 21 lags + 2 cyclical time + 1 encoded ID
- **Preprocessing**: Median imputation → StandardScaler
- **Output**: Raw prediction + bucketed category (low/medium/high) + top-3 feature importance

### Artifacts to Collect from Team

| Artifact | Format | Source |
|----------|--------|--------|
| Trained XGBRegressor | `.joblib` or `.xgb` | MLflow model registry |
| Feature scaler | `feature_scaler.joblib` | MLflow preprocessing artifacts |
| Training medians | `train_medians.json` | MLflow preprocessing artifacts |
| Feature column list | (extract from scaler or document) | Training notebook |

## Architecture

```
EventBridge cron → Fargate Ingest → S3 (COGs + manifests)
                                        │
                    Fargate Serve ← reads COGs + model from S3 → Prism
                         │
                    /predict endpoint runs XGBoost in-process
                         │
                    Writes prediction COGs → S3 → Prism prediction layer
```

No SageMaker in the runtime path.

## Implementation Steps

### Step 1 — Upload model artifacts to our S3 bucket
**Blocked on: getting files from team**

Upload to `s3://lmr-data-cogs-dev/models/livestock-mortality/v1/`:
```
models/livestock-mortality/v1/model.joblib       # or model.xgb
models/livestock-mortality/v1/feature_scaler.joblib
models/livestock-mortality/v1/train_medians.json
```

Then copy to `models/livestock-mortality/latest/` as the active version pointer.

### Step 2 — Update `predict.py`
**File**: `lmr-container/src/lmr/infer/predict.py`

Changes:
- Remove SSM parameter indirection — load directly from `s3://{bucket}/{model_prefix}/`
- Replace `xgboost.Booster` with `xgboost.XGBRegressor` (or `joblib.load`) to match training code
- Add enriched prediction output (bucketed category: low/medium/high, top-3 feature importance)
- Add model caching so it loads once and stays in memory

### Step 3 — Add serve endpoints for inference
**File**: `lmr-container/src/lmr/serve/routes.py` and `app.py`

New endpoints:
- `GET /predict?date=YYYY-MM-DD` — runs inference for the given date, returns prediction COG presigned URL
- `POST /reload-model` — re-downloads model artifacts from S3, hot-swaps in memory (zero-downtime model updates)
- `GET /model-info` — returns current model version, feature list, last reload time

Model loads once at app startup (~5-10 MB, <1 second), cached in a module-level variable.

### Step 4 — Add prediction layer to Prism
**Files**: `prism-app/frontend/src/config/kenya/layers.json` and `prism.json`

Add new layer:
```json
"predictions_mortality": {
  "title": "Livestock Mortality Risk",
  "type": "static_raster",
  "base_url": "https://d31fsorf4vwo9f.cloudfront.net/cog/tiles/WebMercatorQuad/{z}/{x}/{y}?url=s3://lmr-data-cogs-dev/predictions/livestock-mortality/{YYYY-MM-DD}/prediction.tif&rescale=0,1&colormap_name=rdylgn_r",
  "dates": [],
  "legend_text": "Predicted livestock mortality risk (0=low, 1=high)",
  "legend": [
    {"value": "Low", "color": "#1a9850"},
    {"value": "Medium", "color": "#fee08b"},
    {"value": "High", "color": "#d73027"}
  ]
}
```

Add to `prism.json` categories:
```json
"predictions": ["predictions_mortality"]
```

### Step 5 — Remove SageMaker infrastructure
**Can be done immediately (not blocked)**

| File | Action |
|------|--------|
| `cloudformation/sagemaker-trigger.yaml` | Delete entire file |
| `cloudformation/main.yaml` | Remove `SageMakerPipelineArn`, `SageMakerRoleArn`, `SageMakerBucketArn` params; remove `HasSageMakerPipeline` condition; remove `SageMakerTriggerStack` resource; remove `SageMakerBucketArn` from IAMStack params |
| `cloudformation/iam.yaml` | Remove `SageMakerBucketArn` param, `HasSageMakerBucket` condition, and `SageMakerBucketReadOnly` policy |
| `src/lmr/config.py` | Remove `ExternalBucketConfig`, `ExternalBucketsConfig` classes; remove `external_buckets` from `AppConfig`; update `InferenceConfig` to use `model_prefix` instead of `ssm_prefix` |
| `config/datasets.yaml` | Remove `external_buckets:` section; update `inference:` to use `model_prefix` |

### Step 6 — Update `datasets.yaml` inference config

Replace:
```yaml
inference:
  model_name: "livestock-mortality"
  output_bucket: "lmr-data-cogs-dev"
  output_prefix: "predictions/livestock-mortality"
  boundary_file: "boundaries/kenya_wards.geojson"
  ssm_prefix: "/lmr/model"
```

With:
```yaml
inference:
  model_name: "livestock-mortality"
  model_prefix: "models/livestock-mortality/latest"
  output_prefix: "predictions/livestock-mortality"
  boundary_file: "boundaries/kenya_wards.geojson"
```

The model is loaded from `s3://{global.s3_bucket}/{inference.model_prefix}/`.

### Step 7 — Model update workflow

When the team retrains a model:

1. Export artifacts from MLflow/SageMaker notebook:
   ```python
   import joblib, json
   joblib.dump(model, "model.joblib")
   joblib.dump(scaler, "feature_scaler.joblib")
   json.dump(medians, open("train_medians.json", "w"))
   ```

2. Upload to S3 with a version tag:
   ```bash
   aws s3 cp model.joblib s3://lmr-data-cogs-dev/models/livestock-mortality/v2/
   aws s3 cp feature_scaler.joblib s3://lmr-data-cogs-dev/models/livestock-mortality/v2/
   aws s3 cp train_medians.json s3://lmr-data-cogs-dev/models/livestock-mortality/v2/
   ```

3. Promote to latest:
   ```bash
   aws s3 sync s3://lmr-data-cogs-dev/models/livestock-mortality/v2/ \
               s3://lmr-data-cogs-dev/models/livestock-mortality/latest/
   ```

4. Reload the model in the running container (no redeploy needed):
   ```bash
   curl -X POST https://d31fsorf4vwo9f.cloudfront.net/reload-model
   ```

## Execution Order

| # | Step | Effort | Blocked on |
|---|------|--------|------------|
| 1 | Upload model artifacts | Small | Need files from team |
| 2 | Update predict.py | Medium | Step 1 |
| 3 | Add serve endpoints | Medium | Step 2 |
| 4 | Add Prism prediction layer | Small | Step 3 |
| 5 | Remove SageMaker CF/IAM/config | Small | Nothing |
| 6 | Update datasets.yaml | Small | Nothing |
| 7 | Document model update workflow | Small | Nothing |

Steps 5-7 can be done immediately. Steps 1-4 are blocked on getting the model files.

## Model Size Considerations

The current model is ~5-10 MB — trivially small. If the team later moves to a larger ensemble:

| Model size | Fargate config | Startup time | Action needed |
|-----------|---------------|-------------|---------------|
| < 2 GB | Current (2 vCPU / 8 GB) | 5-10s | None |
| 2-5 GB | Bump to 4 vCPU / 16 GB (~$51/mo Spot) | 30-60s | Update task definition |
| 5+ GB | Larger Fargate + EFS mount | 60s+ | Add EFS for persistent cache |
| > 120 GB | Not feasible on Fargate | N/A | Switch to EC2-backed ECS |

For the current XGBoost model, no changes to infrastructure are needed.
