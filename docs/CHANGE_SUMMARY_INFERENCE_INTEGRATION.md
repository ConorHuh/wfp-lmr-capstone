# Change Summary: SageMaker → Step Functions Inference Integration

**Date:** 2026-04-12  
**Branch:** `feature/int`

---

## Why We Made This Change

The team's ML inference pipeline was developed separately in a SageMaker Pipeline (`sagemaker-pipeline/` directory, separate git repo). It worked, but had three problems:

1. **Cost** — SageMaker Pipelines charges for managed infrastructure (processing jobs, pipeline orchestration, MLflow tracking server). This added ~$1,000/year that WFP didn't need.
2. **Complexity** — Running inference required manual execution of `run_inference_all_schemes.py` from a SageMaker notebook. There was no automated trigger connecting the data ingestion to predictions.
3. **Not deployable** — The inference pipeline lived outside the deployed container. A fresh AWS account couldn't run predictions without manual SageMaker setup, IAM roles, and MLflow configuration.

**The goal:** Make the inference pipeline run automatically after each data ingest, deployed entirely via CloudFormation from a single `deploy-all.sh` command, with a YAML toggle for WFP to enable/disable it.

---

## What Changed — Plain English

### Before

```
Ingest (automated, every 10 days)
  → COGs land in S3
  → Nothing happens automatically

Inference (manual)
  → SSH into SageMaker notebook
  → Run run_inference_all_schemes.py
  → SageMaker spins up 3 processing jobs (one per scheme)
  → Results land in the SageMaker bucket
  → Manually copy prediction files to the serving bucket
  → Manually update layers.json dates in the frontend
```

### After

```
Ingest (automated, every 10 days)
  → COGs + manifest land in S3
  → Manifest triggers Lambda
  → Lambda starts Step Functions state machine
  → Step 1: Feature extraction (4 vCPU / 16 GB Fargate task)
  → Step 2: Three parallel inference tasks (biannual, quadseasonal, monthly)
  → Prediction files land directly in the serving bucket
  → Serve API picks them up automatically
```

The whole thing is one `inference.enabled: true` toggle in `datasets.yaml`.

---

## What We Changed — File by File

### New Python Modules (backend/src/lmr/infer/)

| File | What it does | Ported from |
|------|-------------|-------------|
| `preprocess.py` | Downloads model artifacts from S3, imputes NaN features with training medians, produces Ridge-scaled and raw feature parquets | `sagemaker-pipeline/inference_preprocess.py` (no logic changes) |
| `ensemble.py` | Loads 4 trained models (XGBoost, LightGBM, RF, Ridge), runs predictions, computes weighted average. **New:** implements the monthly stacked meta-learner (lgbm + ridge preds + ward encoding → Ridge meta-model) that was missing from the original script. | `sagemaker-pipeline/inference.py` + §7.3 of Technical Handoff |
| `postprocess.py` | Assigns risk levels (Normal/Concerning/Critical) relative to training mean, computes confidence from ensemble disagreement, writes per-timepoint CSV + GeoJSON + 3-band GeoTIFF to S3. **Removed:** all 13 MLflow calls, SageMaker tracking server ARN. **Added:** season→date mapping for Prism-compatible S3 folder names. | `sagemaker-pipeline/postprocess.py` minus MLflow |
| `ward_features.py` | 1000+ line feature engineering pipeline — samples grid points within ward polygons, computes 20km-window satellite means, engineers indices/lags/drought composites, aggregates to season schemes. **Changed:** hardcoded SageMaker bucket replaced with a `configure()` function. | `sagemaker-pipeline/inference_ward_feature_pipeline.py` |
| `feature_extract.py` | Thin CLI wrapper — reads `AppConfig`, calls `ward_features.configure()` then `ward_features.main()`. | New |
| `pipeline.py` | Orchestrates the 3-step inference for one scheme: finds latest ward features parquet → preprocess → ensemble → postprocess. This is what `--mode infer --scheme biannual` runs. | New (replaces `run_inference_all_schemes.py` orchestration) |

### Deleted Python Modules

| File | Why |
|------|-----|
| `backend/src/lmr/infer/predict.py` | The old single-XGBoost inference stub. Used SSM Parameter Store for model URIs, produced single-band COGs. Entirely incompatible with the ensemble pipeline. |

### Config Changes

| File | What changed | Why |
|------|-------------|-----|
| `backend/src/lmr/config.py` | `InferenceConfig` now has: `enabled`, `model_s3_prefix`, `ward_boundaries_s3_key`, `schemes`, `feature_window_months`, `n_sample_points`, `source_data_bucket`, `source_data_prefix`. Removed: `ssm_prefix`. Deleted: `ExternalBucketsConfig` class entirely. | SSM is no longer used (models are in S3 directly). SageMaker bucket reference removed from config. New fields drive the Step Functions pipeline. |
| `backend/config/datasets.yaml` | New `inference:` section with all fields above. Removed: `external_buckets:` section. | Config-driven inference toggle + model paths |
| `backend/pyproject.toml` | Added: `lightgbm>=4.0`, `shap>=0.45`, `s3fs>=2024.0`, `pyproj>=3.6` | LightGBM is an ensemble member, SHAP for feature importance, s3fs for S3 writes in postprocess, pyproj for coordinate transforms in feature extraction |

### CLI Changes

| File | What changed |
|------|-------------|
| `backend/src/lmr/cli.py` | Added `feature-extract` mode (`--time-start`, `--time-end` required). Updated `infer` mode (`--scheme` required). Dispatch wired to new modules. |

### Serve API Changes

| File | What changed | Why |
|------|-------------|-----|
| `backend/src/lmr/serve/routes.py` | `GET /latest` now tries `ward_predictions.tif` first, falls back to `prediction.tif`. `_flatten_prediction_properties` reads `pcode` directly from GeoJSON when present. | New postprocess writes `ward_predictions.tif` (3-band). New GeoJSON includes `pcode` field, avoiding the hardcoded name→pcode map. |

### New CloudFormation Templates

| Template | What it creates | Why |
|----------|----------------|-----|
| `cloudfront.yaml` | CloudFront distribution pointing to ALB. HTTPS-only, CORS configured (Origin header forwarded, OPTIONS allowed). | Was manually configured via `aws cloudfront update-distribution` in the deploy script. Now declarative. |
| `amplify.yaml` | Amplify app + main branch. | Was a hardcoded `AMPLIFY_APP_ID` in the deploy script. Now created by CloudFormation. |
| `fargate-infer.yaml` | Two ECS task definitions: `lmr-feature-extract-{env}` (4 vCPU / 16 GB) and `lmr-infer-{env}` (1 vCPU / 4 GB). | Feature extraction is memory-intensive (loads satellite parquets). Inference is lighter. Same Docker image, different commands. |
| `step-functions.yaml` | State machine (feature-extract → 3× parallel infer), Lambda trigger function, EventBridge rule on S3 `manifests/` prefix. | Replaces SageMaker Pipeline orchestration. Lambda computes the time window and passes infrastructure config as execution input. |

### Updated CloudFormation Templates

| Template | What changed | Why |
|----------|-------------|-----|
| `main.yaml` | `ScheduleIntervalDays` default: 8→10. Removed: `SageMakerBucketArn`, `SageMakerPipelineArn`, `SageMakerRoleArn` params + `HasSageMakerPipeline` condition. Added: `EnableInferencePipeline` param/condition, `CloudFrontStack`, `AmplifyStack`, `FargateInferStack`, `StepFunctionsStack`. | Fresh-account deploy + inference toggle |
| `iam.yaml` | Removed: `ssm:GetParameter` from task role, `SageMakerBucketArn` param + conditional SageMaker bucket policy. Added: `s3:DeleteObject` to task role, `SfnExecutionRole` (states.amazonaws.com → ecs:RunTask + iam:PassRole), `LambdaSfnTriggerRole` (lambda.amazonaws.com → states:StartExecution). | SSM no longer needed. Step Functions needs its own IAM roles. |
| `s3.yaml` | Removed: `SageMakerRoleArn` param + conditional `DataBucketPolicy` (SageMaker cross-bucket read/write). | No more SageMaker access to the data bucket. |

### Archived

| File | From | To |
|------|------|----|
| `sagemaker-trigger.yaml` | `infra/cloudformation/` | `docs/archive/sagemaker-trigger.yaml` |

### Deploy Script (`infra/deploy-all.sh`)

| Change | Before | After |
|--------|--------|-------|
| VPC | Hardcoded `vpc-0c392a79120ac5b1c` | Auto-discovers default VPC (`aws ec2 describe-vpcs --filters Name=isDefault`) |
| Subnets | Hardcoded two subnet IDs | Auto-discovers default subnets |
| CloudFront | Hardcoded distribution ID + manual CORS config via `aws cloudfront update-distribution` (50 lines of inline Python) | Reads distribution ID from CloudFormation stack outputs. CORS now in `cloudfront.yaml`. Cache invalidation only. |
| Amplify | Hardcoded `d3dvy50qlv6dr6` | Reads app ID from CloudFormation stack outputs |
| SageMaker params | 3 params passed to CloudFormation | Removed |
| Inference toggle | Didn't exist | Reads `inference.enabled` from `datasets.yaml`, passes as `EnableInferencePipeline` param |
| Model migration | Manual | Automatic `aws s3 sync` when inference enabled (idempotent) |
| Bootstrap | Assumed CFN artifacts bucket existed | Creates `lmr-cfn-artifacts-{env}` if missing |

### New Tests

| File | Tests | What's covered |
|------|-------|---------------|
| `tests/test_ensemble.py` | 9 tests | Weighted ensemble (5): uniform weights, single model, normalization, zero-weight error, realistic weights. Stacked meta-learner (4): correct shape, matches manual computation, unseen ward fallback to `_global_mean`, negative clipping. |
| `tests/test_postprocess.py` | 28 tests | Risk level assignment (9 boundary conditions). Confidence computation (6 cases). Time column detection (3). Timepoint label formatting (3). Season→date key mapping (7 including all known seasons + unknown fallback). |

### Updated Tests

| File | What changed |
|------|-------------|
| `tests/test_config.py` | Updated for new `InferenceConfig` fields. Removed `test_external_buckets_config`. Fixed stale assertions (lookback_days, disabled dataset list). |

### Documentation

| File | What changed |
|------|-------------|
| `README.md` | Complete rewrite (was deleted, recreated). System diagram, quick start, repo structure, config docs, API reference. |
| `docs/ARCHITECTURE.md` | Complete rewrite. New system diagram with Step Functions. Updated container modes (4 modes), S3 layout (models/, inference/ prefixes), infrastructure table (11 stacks), key design decisions (10 items). |
| `docs/PLAN_INFERENCE_INTEGRATION.md` | New — technical approach document for this integration. |
| `docs/PROGRESS_INFERENCE_INTEGRATION.md` | New — phase-by-phase progress tracker. |

---

## Model Artifact Migration

One-time AWS CLI operation (also automated in `deploy-all.sh`):

```
Source:  s3://amazon-sagemaker-575108933641-us-east-1-c422b90ce861/
         dzd-.../shared/final_lmr_ward_results/inference_bundle/{scheme}/

Dest:    s3://lmr-data-cogs-dev/models/inference_bundle/{scheme}/
```

- **biannual**: 9 files (4 model jobblibs + weights, features, medians, scaler, metadata)
- **quadseasonal**: 9 files (same structure)
- **monthly**: 13 files (same + meta_model, meta_scaler, meta_feature_names, ward_encoding)
- **Ward boundaries**: `geoBoundaries-KEN-ADM3.geojson`

**Originals were NOT deleted.** Revert by changing `model_s3_prefix` in `datasets.yaml` to the SageMaker bucket path and redeploying.

---

## What's NOT in Scope / Still Pending

1. **Phase 7 (E2E test)** — Requires a live `deploy-all.sh` run to test the full trigger chain: ingest → manifest → Lambda → Step Functions → feature-extract → 3× infer → predictions in S3 → serve API returns them.
2. **`layers.json` date update** — Once E2E produces real predictions, add those dates to `frontend/kenya_config/layers.json` for the Prism frontend.
3. **Phase 9 (archive)** — Move `PLAN_INFERENCE_INTEGRATION.md` and `PROGRESS_INFERENCE_INTEGRATION.md` to `docs/archive/` once everything is shipped and `ARCHITECTURE.md` is the single source of truth.
