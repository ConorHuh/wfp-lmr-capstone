# LMR Inference Integration — Technical Approach

> **Status:** In progress  
> **Progress tracker:** `docs/PROGRESS_INFERENCE_INTEGRATION.md`  
> **Archive when done:** move both docs to `docs/archive/`, then update `docs/ARCHITECTURE.md`

---

## Context

The SageMaker inference pipeline (`sagemaker-pipeline/`) was developed independently and is not integrated into the deployed container. It currently requires manual execution via `run_inference_all_schemes.py`. The SageMaker orchestration layer is too expensive and operationally complex for WFP. This plan replaces SageMaker with an AWS Step Functions state machine deployed via CloudFormation, triggered automatically by the existing ingest manifest event.

The deployment is opt-in: `inference.enabled: false` in `datasets.yaml` deploys no Step Function infrastructure and the container image is unchanged — WFP can use this container for other workflows without the inference stack running.

Three trained model artifacts (biannual, quadseasonal, monthly) already exist in the SageMaker bucket and are **never touched**. The integration copies them to the production bucket as a one-time migration with a documented revert path.

---

## Target Architecture

```
EventBridge (rate: 10 days)
  └─> ECS Fargate: --mode ingest          [1 vCPU / 4 GB]
        └─> writes manifests/ingest-{ts}.json to S3
              └─> EventBridge rule (S3 ObjectCreated on manifests/)
                    └─> Lambda (manifest-to-sfn trigger)
                          └─> Step Functions: LMR-Ward-Inference
                                ├─ Step 1: ECS --mode feature-extract  [4 vCPU / 16 GB]
                                │          writes ward_features_*.parquet to S3
                                └─ Step 2: Parallel (3 branches)
                                     ├─ ECS --mode infer --scheme biannual      [1 vCPU / 4 GB]
                                     ├─ ECS --mode infer --scheme quadseasonal  [1 vCPU / 4 GB]
                                     └─ ECS --mode infer --scheme monthly       [1 vCPU / 4 GB]
                                           each writes to:
                                             predictions/livestock-mortality/{YYYY_MM_DD}/
                                               ward_predictions.csv
                                               ward_predictions.geojson
                                               ward_predictions.tif  (3-band COG)
```

**WFP opt-out:** Set `inference.enabled: false` in `datasets.yaml` → `deploy-all.sh` passes `EnableInferencePipeline=false` → Step Functions stack and trigger Lambda are not deployed. Container image is identical regardless.

---

## New `datasets.yaml` Inference Config

```yaml
inference:
  enabled: true                          # false = skip Step Function deployment entirely
  model_name: livestock-mortality
  model_s3_prefix: s3://lmr-data-cogs-dev/models/inference_bundle  # production copy
  model_s3_prefix_fallback: s3://amazon-sagemaker-575108933641-us-east-1-c422b90ce861/dzd-ayr06tncl712p3/5t7l23o0xvt99j/shared/final_lmr_ward_results/inference_bundle  # revert path — never deleted
  ward_boundaries_s3_key: models/geoBoundaries-KEN-ADM3.geojson
  output_bucket: lmr-data-cogs-dev
  output_prefix: predictions/livestock-mortality
  schemes:
    - biannual
    - quadseasonal
    - monthly
  feature_window_months: 36              # how far back feature extraction looks from today
  n_sample_points: 9                     # 3×3 grid per ward polygon
  boundary_file: boundaries/kenya_wards.geojson  # local file used by serve mode
```

**Revert path:** Change `model_s3_prefix` to `model_s3_prefix_fallback` value and redeploy. No retraining needed. Original SageMaker artifacts are never modified.

---

## Updated `AppConfig` (`backend/src/lmr/config.py`)

Replace existing `InferenceConfig` with:

```python
class InferenceConfig(BaseModel):
    enabled: bool = False
    model_name: str = "livestock-mortality"
    model_s3_prefix: str = ""           # s3://bucket/models/inference_bundle
    ward_boundaries_s3_key: str = ""    # S3 key for ADM3 GeoJSON in data bucket
    output_bucket: str = "lmr-data-cogs"
    output_prefix: str = "predictions/livestock-mortality"
    boundary_file: str = "boundaries/kenya_wards.geojson"
    schemes: list[str] = Field(default_factory=lambda: ["biannual", "quadseasonal", "monthly"])
    feature_window_months: int = 36
    n_sample_points: int = 9
    # REMOVED: ssm_prefix
```

Remove from `AppConfig`: `external_buckets: ExternalBucketsConfig` (SageMaker bucket reference no longer needed).

---

## S3 Path Conventions

```
lmr-data-cogs-dev/
  models/
    inference_bundle/
      biannual/         ← one-time copy from SageMaker bucket
      quadseasonal/
      monthly/
    geoBoundaries-KEN-ADM3.geojson
  inference/
    ward_features_{YYYY-MM}_{YYYY-MM}/    ← feature extraction outputs
      ward_features_biannual.parquet
      ward_features_quadseasonal.parquet
      ward_features_monthly.parquet
      extracted/                          ← per-collection intermediate parquet cache
    preprocessed/{scheme}/               ← inference step intermediates
      preprocessed_features.parquet
      preprocessed_features_ridge.parquet
      inference_metadata.parquet
      predictions_with_metadata.parquet
  predictions/
    livestock-mortality/
      {YYYY_MM_DD}/                       ← end-of-season date key (Prism-compatible)
        ward_predictions.csv
        ward_predictions.geojson
        ward_predictions.tif              ← 3-band COG
```

### Season → Date Key Mapping

Postprocess maps each season label to the end-of-season date for S3 folder naming and Prism `{YYYY_MM_DD}` compatibility:

| Season | Date key |
|--------|----------|
| OND | `{year}_12_31` |
| MAM | `{year}_05_31` |
| LRLD | `{year}_09_30` |
| SRSD | `{year}_02_28` |
| LRS | `{year}_09_30` |
| LRS_dry | `{year}_11_30` |
| SRS | `{year}_02_28` |
| SRS_dry | `{year}_04_30` |
| Monthly | `{year}_{MM:02d}_01` |

---

## New Backend Container Modes

| Mode | Command | Resources | Description |
|------|---------|-----------|-------------|
| `ingest` | unchanged | 1 vCPU / 4 GB | No change |
| `serve` | unchanged | 2 vCPU / 8 GB | No change |
| `feature-extract` | `lmr --mode feature-extract --time-start YYYY-MM --time-end YYYY-MM` | 4 vCPU / 16 GB | Ward satellite feature extraction via Planetary Computer |
| `infer` | `lmr --mode infer --scheme biannual\|quadseasonal\|monthly` | 1 vCPU / 4 GB | Ensemble inference for one scheme (preprocess → predict → postprocess) |

---

## Monthly Meta-Learner (Known Gap in `sagemaker-pipeline/inference.py`)

`sagemaker-pipeline/inference.py` only implements weighted ensemble — it does **not** implement the stacked meta-learner for the monthly scheme. The new `backend/src/lmr/infer/ensemble.py` must handle both paths:

**Biannual / Quadseasonal** (weighted ensemble):
```
X → [xgboost, rf, lgbm, ridge] → normalised weighted average → prediction
```

**Monthly** (stacked meta-learner):
```
Step 1: X → [xgboost, rf, lgbm, ridge] → base_preds
Step 2: meta_X = [lgbm_pred, ridge_pred, ward_enc_value]  ← order from meta_feature_names.json
Step 3: final = clip(ridge_meta.predict(meta_scaler.transform(meta_X)), 0, None)
```

`ward_enc_value` is looked up from `ward_encoding.json` by `ward_name`. Unseen wards fall back to `_global_mean` stored in that file.

Additional bundle files for monthly only: `meta_model.joblib`, `meta_scaler.joblib`, `meta_feature_names.json`, `ward_encoding.json`.

---

## New Source Files

```
backend/src/lmr/infer/
├── __init__.py           unchanged
├── predict.py            DELETE — entirely replaced
├── feature_extract.py    NEW — port of inference_ward_feature_pipeline.py; reads config from AppConfig
├── preprocess.py         NEW — port of sagemaker-pipeline/inference_preprocess.py (no changes needed)
├── ensemble.py           NEW — port of sagemaker-pipeline/inference.py + monthly meta-learner path
└── postprocess.py        NEW — port of sagemaker-pipeline/postprocess.py with MLflow removed
```

### MLflow Removal (`postprocess.py`)

13 MLflow calls are removed. Function signature changes — drop params: `experiment_name`, `run_id`, `training_run_id`. The S3 upload calls (`fs.put`, `to_parquet`) already exist in the original and are kept. MLflow `log_artifacts` and `log_metrics` calls are deleted without replacement.

---

## CloudFormation Changes — Complete Template List

### New Templates

| Template | Purpose |
|----------|---------|
| `infra/cloudformation/cloudfront.yaml` | CloudFront distribution pointing to ALB. HTTPS-only, forward `Origin` header, allow OPTIONS. Outputs: `DistributionId`, `DistributionDomain`. |
| `infra/cloudformation/amplify.yaml` | Amplify app + main branch. Outputs: `AppId`, `DefaultDomain`. |
| `infra/cloudformation/step-functions.yaml` | State machine (Express) + manifest-trigger Lambda + EventBridge rule. Conditional on `HasInferencePipeline`. |
| `infra/cloudformation/fargate-infer.yaml` | Task defs: feature-extract (4 vCPU/16384 MB) + infer (1 vCPU/4096 MB). Conditional on `HasInferencePipeline`. |

### Updated Templates

| Template | Changes |
|----------|---------|
| `main.yaml` | Fix `ScheduleIntervalDays` default 8 → **10**. Remove `SageMakerBucketArn/PipelineArn/RoleArn` params. Add `EnableInferencePipeline` param + `HasInferencePipeline` condition. Add `CloudFrontStack`, `AmplifyStack`, `StepFunctionsStack` (conditional), `FargateInferStack` (conditional). Remove `SageMakerTriggerStack`. |
| `iam.yaml` | Remove `ssm:GetParameter` from task role. Add `s3:GetObject/PutObject` on `models/*` + `inference/*`. Add `SfnExecutionRole` + `LambdaSfnTriggerRole` resources + outputs. |
| `s3.yaml` | Remove `SageMakerRoleArn` parameter + conditional bucket policy. |

### Archived Templates

| Template | Destination |
|----------|-------------|
| `sagemaker-trigger.yaml` | `docs/archive/sagemaker-trigger.yaml` |

---

## `main.yaml` — Updated Structure

```yaml
Parameters:
  # REMOVE: SageMakerBucketArn, SageMakerPipelineArn, SageMakerRoleArn
  # ADD:
  EnableInferencePipeline:
    Type: String
    Default: "false"
    AllowedValues: ["true", "false"]
  ScheduleIntervalDays:
    Type: Number
    Default: 10      # changed from 8

Conditions:
  HasInferencePipeline: !Equals [!Ref EnableInferencePipeline, "true"]
  # REMOVE: HasSageMakerPipeline

Resources:
  # Always-on stacks (unchanged): ECRStack, S3Stack, IAMStack, FargateIngestStack, EventBridgeStack, FargateServeStack
  # New always-on:
  CloudFrontStack:
    DependsOn: FargateServeStack
    Properties:
      Parameters:
        AlbDnsName: !GetAtt FargateServeStack.Outputs.ALBDnsName

  AmplifyStack: { ... }

  # Conditional on HasInferencePipeline:
  FargateInferStack: { ... }
  StepFunctionsStack:
    Properties:
      Parameters:
        FeatureExtractTaskDefArn: !GetAtt FargateInferStack.Outputs.FeatureExtractTaskDefArn
        InferTaskDefArn: !GetAtt FargateInferStack.Outputs.InferTaskDefArn
        SfnExecutionRoleArn: !GetAtt IAMStack.Outputs.SfnExecutionRoleArn
        LambdaSfnTriggerRoleArn: !GetAtt IAMStack.Outputs.LambdaSfnTriggerRoleArn
```

---

## IAM Permissions — Final State

**ECS Task Role** (`lmr-task-role-{env}`):

| Action | Resource |
|--------|----------|
| `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` | `arn:aws:s3:::lmr-data-cogs-{env}/*` |
| `s3:ListBucket` | `arn:aws:s3:::lmr-data-cogs-{env}` |
| `logs:CreateLogStream`, `logs:PutLogEvents` | `*` |
| ~~`ssm:GetParameter`~~ | ~~removed~~ |

**Step Functions Execution Role** (`lmr-sfn-execution-role-{env}`) — NEW:

| Action | Resource |
|--------|----------|
| `ecs:RunTask` | `*` |
| `iam:PassRole` | TaskRoleArn + ExecutionRoleArn |
| `logs:CreateLogDelivery`, `logs:PutLogEvents` | `*` |

**Lambda SFN Trigger Role** (`lmr-lambda-sfn-role-{env}`) — NEW:

| Action | Resource |
|--------|----------|
| `states:StartExecution` | State machine ARN |
| `s3:GetObject` | `manifests/*` prefix |
| `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents` | `*` |

---

## `deploy-all.sh` — Key Changes

1. **Bootstrap** (idempotent, runs before CFN deploy):
   ```bash
   aws s3 mb s3://lmr-cfn-artifacts-${ENVIRONMENT} --region ${REGION} 2>/dev/null || true
   ```

2. **Auto-discover default VPC** (replaces hardcoded IDs):
   ```bash
   VPC_ID=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
     --query 'Vpcs[0].VpcId' --output text --region "${REGION}")
   SUBNET_IDS=$(aws ec2 describe-subnets \
     --filters Name=vpc-id,Values=${VPC_ID} Name=defaultForAz,Values=true \
     --query 'Subnets[*].SubnetId' --output text --region "${REGION}" | tr '\t' ',')
   ```
   Override with `--vpc-id` / `--subnet-ids` flags for custom VPCs.

3. **Read inference toggle from config**:
   ```bash
   ENABLE_INFERENCE=$(python3 -c "
   import yaml
   d = yaml.safe_load(open('backend/config/datasets.yaml'))
   print(str(d.get('inference', {}).get('enabled', False)).lower())
   ")
   PARAMS="${PARAMS} EnableInferencePipeline=${ENABLE_INFERENCE}"
   ```

4. **Read CloudFront/Amplify IDs from CFN outputs** (replaces hardcoded):
   ```bash
   CLOUDFRONT_DISTRIBUTION_ID=$(aws cloudformation describe-stacks \
     --stack-name ${STACK_NAME} --query \
     "Stacks[0].Outputs[?OutputKey=='CloudFrontDistributionId'].OutputValue" \
     --output text --region "${REGION}")
   AMPLIFY_APP_ID=$(aws cloudformation describe-stacks \
     --stack-name ${STACK_NAME} --query \
     "Stacks[0].Outputs[?OutputKey=='AmplifyAppId'].OutputValue" \
     --output text --region "${REGION}")
   ```

5. **Model migration** (idempotent, only when `ENABLE_INFERENCE=true`):
   ```bash
   if [[ "${ENABLE_INFERENCE}" == "true" ]]; then
     SM_BUCKET="amazon-sagemaker-575108933641-us-east-1-c422b90ce861"
     SM_PREFIX="dzd-ayr06tncl712p3/5t7l23o0xvt99j/shared/final_lmr_ward_results/inference_bundle"
     for scheme in biannual quadseasonal monthly; do
       aws s3 sync s3://${SM_BUCKET}/${SM_PREFIX}/${scheme}/ \
         s3://${S3_BUCKET}/models/inference_bundle/${scheme}/ \
         --no-progress 2>/dev/null || echo "  Warn: model migration failed for ${scheme} (check cross-account permissions)"
     done
     aws s3 cp s3://${SM_BUCKET}/dzd-.../shared/geoBoundaries-KEN-ADM3.geojson \
       s3://${S3_BUCKET}/models/geoBoundaries-KEN-ADM3.geojson 2>/dev/null || true
   fi
   ```

6. **Remove** Step 2 (manual CloudFront CORS config) — now handled by `cloudfront.yaml`.

7. **Remove** `SageMakerBucketArn`, `SageMakerPipelineArn`, `SageMakerRoleArn` from `PARAMS`.

---

## New Python Dependencies

Add to `backend/pyproject.toml`:
- `lightgbm>=4.0` — monthly/quadseasonal ensemble member (not currently installed)
- `shap>=0.45` — SHAP feature importance in postprocess (not currently installed)

---

## Cleanup Targets

| Item | Action |
|------|--------|
| `backend/src/lmr/infer/predict.py` | Delete |
| `backend/src/lmr/config.py` `ExternalBucketsConfig` | Delete class + field |
| `backend/config/datasets.yaml` `external_buckets:` | Remove section |
| `infra/cloudformation/sagemaker-trigger.yaml` | Archive to `docs/archive/` |
| `sagemaker-pipeline/` | Keep intact (reference, training artifacts) |
| `docs/ARCHITECTURE.md` | Full update after Phase 8 |
