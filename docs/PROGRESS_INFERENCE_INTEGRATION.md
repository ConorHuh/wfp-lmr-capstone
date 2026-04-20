# LMR Inference Integration — Progress Tracker

> **Technical approach:** `docs/PLAN_INFERENCE_INTEGRATION.md`  
> **Archive when done:** move both docs to `docs/archive/`, update `docs/ARCHITECTURE.md`

---

## Phase 0 — Setup
> Write working docs. No code changes.

- [x] Write `docs/PLAN_INFERENCE_INTEGRATION.md`
- [x] Write `docs/PROGRESS_INFERENCE_INTEGRATION.md`
- [ ] Archive plan file `gleaming-meandering-duckling.md`

**Test gate:** Both docs exist in `docs/`. No code changed.  
**Status: COMPLETE**

---

## Phase 1 — Model Artifact Migration
> One-time copy of trained model bundles from SageMaker bucket to production bucket. Originals are never deleted.

**Source:** `s3://amazon-sagemaker-575108933641-us-east-1-c422b90ce861/dzd-ayr06tncl712p3/5t7l23o0xvt99j/shared/final_lmr_ward_results/inference_bundle/`  
**Destination:** `s3://lmr-data-cogs-dev/models/inference_bundle/`

- [x] Copy `biannual/` bundle (9 files)
- [x] Copy `quadseasonal/` bundle (9 files)
- [x] Copy `monthly/` bundle (13 files: base 9 + meta_model, meta_scaler, meta_feature_names, ward_encoding)
- [x] Copy `geoBoundaries-KEN-ADM3.geojson` to `s3://lmr-data-cogs-dev/models/`
- [x] Verify originals still present in SageMaker bucket

**Test gate:** PASSED — all 3 scheme folders present in prod bucket with correct file counts. Originals intact.  
**Status: COMPLETE**

---

## Phase 2 — Config & Dependencies
> Update `AppConfig`, `datasets.yaml`, `pyproject.toml`. No inference logic yet.

- [ ] Update `InferenceConfig` in `backend/src/lmr/config.py`
  - Remove: `ssm_prefix`
  - Add: `enabled`, `model_s3_prefix`, `ward_boundaries_s3_key`, `schemes`, `feature_window_months`, `n_sample_points`
- [ ] Remove `ExternalBucketsConfig` class from `config.py`
- [ ] Remove `external_buckets` field from `AppConfig`
- [ ] Update `backend/config/datasets.yaml` — add new `inference:` section (see PLAN doc)
- [ ] Remove `external_buckets:` section from `datasets.yaml`
- [ ] Add `lightgbm>=4.0` to `backend/pyproject.toml`
- [ ] Add `shap>=0.45` to `backend/pyproject.toml`
- [ ] Run `uv sync` — confirm no resolution errors
- [ ] Update `backend/tests/test_config.py` for new schema

**Test gate:** PASSED — `uv run python -m pytest tests/test_config.py` — 8/8 tests pass. Config loads with new schema.  
**Status: COMPLETE**

---

## Phase 3 — Port Inference Logic to Container
> Create 4 new source files in `backend/src/lmr/infer/`. Do not touch CLI yet.

- [ ] Create `backend/src/lmr/infer/preprocess.py`
  - Port `run_inference_preprocess()` from `sagemaker-pipeline/inference_preprocess.py` verbatim
  - No logic changes needed — function already takes S3 URIs as arguments

- [ ] Create `backend/src/lmr/infer/ensemble.py`
  - Port `run_inference()` from `sagemaker-pipeline/inference.py`
  - **Add monthly meta-learner path** (not present in original — see PLAN doc §Monthly Meta-Learner)
  - Keep biannual/quadseasonal weighted-average path unchanged

- [ ] Create `backend/src/lmr/infer/postprocess.py`
  - Port `run_postprocess()` from `sagemaker-pipeline/postprocess.py`
  - Remove all 13 MLflow calls (full list in PLAN doc §MLflow Removal)
  - Drop function params: `experiment_name`, `run_id`, `training_run_id`
  - Keep S3 upload logic (`fs.put`, `to_parquet` calls)
  - Add season → `YYYY_MM_DD` date key mapping (see PLAN doc §Season → Date Key Mapping)

- [ ] Create `backend/src/lmr/infer/feature_extract.py`
  - Port logic from `sagemaker-pipeline/inference_ward_feature_pipeline.py`
  - Replace hardcoded `SM_BUCKET`, `PC_PREFIX`, `WARD_BOUNDARIES_KEY` with values from `AppConfig.inference`
  - Accept `time_start: str`, `time_end: str` as function arguments (passed from CLI)
  - Keep all Planetary Computer STAC querying logic intact

**Test gate:** PASSED — all 4 modules import without error. Monthly meta-learner path implemented in `ensemble.py`.  
**Status: COMPLETE**

---

## Phase 4 — Wire New CLI Modes
> Update `cli.py`. Delete old `predict.py`.

- [ ] Add `"feature-extract"` to `--mode` choices in `cli.py`
- [ ] Add `--scheme` arg (choices: `biannual`, `quadseasonal`, `monthly`)
- [ ] Add `--time-start` arg (YYYY-MM, required for `feature-extract`)
- [ ] Add `--time-end` arg (YYYY-MM, required for `feature-extract`)
- [ ] Dispatch `feature-extract` → `feature_extract.run_feature_extraction(config, time_start, time_end)`
- [ ] Dispatch `infer` → `ensemble.run_inference_pipeline(config, scheme)` (orchestrates preprocess → ensemble → postprocess)
- [ ] Delete `backend/src/lmr/infer/predict.py`

**Test gate:** PASSED — `lmr --help` shows all 4 modes (ingest, serve, infer, feature-extract). `--scheme` required for infer, `--time-start/--time-end` required for feature-extract. Old `predict.py` deleted. 16/17 tests pass (1 pre-existing failure in test_serve.py unrelated to our changes).  
**Status: COMPLETE**

---

## Phase 5 — Full CloudFormation Infrastructure (Fresh-Account Ready)

### 5a — New Foundation Templates

- [ ] Create `infra/cloudformation/cloudfront.yaml`
  - Origin: ALB DNS (param from `FargateServeStack.Outputs.ALBDnsName`)
  - Protocol policy: redirect HTTP → HTTPS
  - Allowed methods: GET, HEAD, OPTIONS (cache GET + HEAD)
  - Forward headers: `Origin`, `Authorization`
  - Default TTL: 86400s, min TTL: 0
  - Outputs: `DistributionId`, `DistributionDomain`

- [ ] Create `infra/cloudformation/amplify.yaml`
  - `AWS::Amplify::App` resource
  - `AWS::Amplify::Branch` (main branch)
  - Outputs: `AppId`, `DefaultDomain`

### 5b — Inference Templates

- [ ] Create `infra/cloudformation/fargate-infer.yaml`
  - Task def `lmr-feature-extract-{env}`: 4096 CPU / 16384 MB
    - Base command: `["--mode", "feature-extract", "--config", "/app/config/datasets.yaml"]`
    - `--time-start` + `--time-end` injected by Step Functions container command override
  - Task def `lmr-infer-{env}`: 1024 CPU / 4096 MB
    - Base command: `["--mode", "infer", "--config", "/app/config/datasets.yaml"]`
    - `--scheme` injected by Step Functions container command override
  - Same cluster, ECR image, TaskRole, ExecutionRole as ingest task
  - Outputs: `FeatureExtractTaskDefArn`, `InferTaskDefArn`

- [ ] Create `infra/cloudformation/step-functions.yaml`
  - Lambda (Python 3.11, 60s timeout, `lmr-manifest-sfn-trigger-{env}`):
    - Env vars: `SFN_ARN`, `DATA_BUCKET`, `FEATURE_WINDOW_MONTHS` (default 36)
    - Triggered by S3 ObjectCreated on `manifests/` prefix (EventBridge rule)
    - Computes `time_end = today.strftime("%Y-%m")`, `time_start = (today − N months).strftime("%Y-%m")`
    - Builds `ward_features_prefix = s3://{DATA_BUCKET}/inference/ward_features_{ts}_{te}`
    - Calls `sfn:StartExecution` with JSON input: `{timeStart, timeEnd, wardFeaturesPrefix, inferOutputPrefix}`
  - State machine (`lmr-ward-inference-{env}`, Express workflow):
    - State 1 `FeatureExtraction`: `ecs:runTask.sync`, feature-extract task def, command override `--time-start $$.Execution.Input.timeStart --time-end $$.Execution.Input.timeEnd`
    - State 2 `InferenceParallel`: Parallel with 3 branches, each `ecs:runTask.sync`, infer task def, `--scheme biannual|quadseasonal|monthly`
    - Catch: ECS failures → `InferenceFailed` terminal state, log to CloudWatch
  - EventBridge rule: S3 `manifests/` ObjectCreated → Lambda

### 5c — Update Existing Templates

- [ ] Update `infra/cloudformation/main.yaml`
  - Fix `ScheduleIntervalDays` default: 8 → **10**
  - Remove params: `SageMakerBucketArn`, `SageMakerPipelineArn`, `SageMakerRoleArn`
  - Add param: `EnableInferencePipeline` (String, default: "false", AllowedValues: ["true","false"])
  - Remove condition: `HasSageMakerPipeline`
  - Add condition: `HasInferencePipeline: !Equals [!Ref EnableInferencePipeline, "true"]`
  - Add stacks: `CloudFrontStack` (always), `AmplifyStack` (always)
  - Replace `SageMakerTriggerStack` with `StepFunctionsStack` + `FargateInferStack` (both conditional)
  - `VpcId`/`SubnetIds` remain params (auto-discovered by deploy script)

- [ ] Update `infra/cloudformation/iam.yaml`
  - Remove `ssm:GetParameter` from ECS task role
  - Add to ECS task role: `s3:GetObject/PutObject` on `models/*` + `inference/*` prefixes
  - Add `SfnExecutionRole` resource (principal: `states.amazonaws.com`)
  - Add `LambdaSfnTriggerRole` resource (principal: `lambda.amazonaws.com`)
  - Add outputs: `SfnExecutionRoleArn`, `LambdaSfnTriggerRoleArn`

- [ ] Update `infra/cloudformation/s3.yaml`
  - Remove `SageMakerRoleArn` parameter
  - Remove conditional `SageMakerBucketPolicy` resource

### 5d — Update Deploy Script

- [ ] Update `infra/deploy-all.sh`
  - Add bootstrap (before CFN deploy, idempotent): `aws s3 mb s3://lmr-cfn-artifacts-${ENVIRONMENT} 2>/dev/null || true`
  - Remove hardcoded `VPC_ID`, `SUBNET_IDS` — auto-discover default VPC
  - Remove hardcoded `CLOUDFRONT_DISTRIBUTION_ID`, `AMPLIFY_APP_ID` — read from CFN outputs
  - Remove `SageMakerBucketArn/PipelineArn/RoleArn` from PARAMS
  - Add `EnableInferencePipeline` to PARAMS (read from `datasets.yaml`)
  - Add model migration block (idempotent, only when `ENABLE_INFERENCE=true`)
  - Remove Step 2 (manual CloudFront CORS config) — now in `cloudfront.yaml`
  - Add `--vpc-id` / `--subnet-ids` optional flags for custom VPC override

**Test gate:** PASSED — `aws cloudformation validate-template` passes on all 12 templates (4 new + 3 updated + 5 unchanged). Deploy script updated: auto-discovers VPC, reads inference toggle from datasets.yaml, bootstrap bucket creation, model migration, CloudFront managed by CFN.  
**Status: COMPLETE**

---

## Phase 6 — Serve API Verification
> Confirm prediction endpoints work with season-based `YYYY_MM_DD` date keys.

- [ ] Review `backend/src/lmr/serve/routes.py` — confirm `GET /predictions/livestock-mortality/dates` lists S3 date folders correctly
- [ ] Confirm `GET /predictions/livestock-mortality/{date}` serves `ward_predictions.geojson` with correct schema
- [ ] Confirm `GET /latest?model=livestock-mortality` returns most recent prediction date

**Test gate:** PASSED — 8/8 serve tests pass. `/latest` updated to try `ward_predictions.tif` first (new naming), fallback to `prediction.tif` (legacy). `_flatten_prediction_properties` uses `pcode` field directly when present in new GeoJSON format.  
**Status: COMPLETE**

---

## Phase 7 — End-to-End Test
> Full pipeline run triggered by ingest manifest.

- [ ] Run ingest task manually to generate fresh manifest
- [ ] Confirm Step Function execution triggered in console
- [ ] Confirm feature extraction task completes — `inference/ward_features_*/` parquets present in S3
- [ ] Confirm 3 inference tasks complete in parallel
- [ ] Confirm `predictions/livestock-mortality/{YYYY_MM_DD}/` contains all 3 output files per timepoint
- [ ] Verify serve API returns new prediction dates
- [ ] Verify ward GeoJSON is valid (correct schema, geometries present)
- [ ] Verify 3-band GeoTIFF is readable (`rio info s3://...ward_predictions.tif`)

**Test gate:** Full pipeline produces valid outputs at all expected S3 paths. Serve API returns correct data.  
**Status: PENDING — requires live AWS deployment (`deploy-all.sh`)**

---

## Phase 8 — Cleanup
> Remove SageMaker remnants. Verify fresh-account deploy.

- [ ] Archive `infra/cloudformation/sagemaker-trigger.yaml` → `docs/archive/`
- [ ] Confirm `sagemaker-pipeline/` stays intact (reference only)
- [ ] Confirm `backend/src/lmr/infer/predict.py` is gone (deleted in Phase 4)
- [ ] Remove `ExternalBucketsConfig` class from `backend/src/lmr/config.py`
- [ ] Remove `external_buckets:` from `backend/config/datasets.yaml`
- [ ] Verify no SageMaker/MLflow/SSM refs in `backend/src/`:
  ```
  grep -r "ssm_prefix\|SageMakerPipeline\|sagemaker_trigger\|mlflow\|ssm:GetParameter" backend/src/
  ```
  Expect: no matches
- [ ] Update `docs/ARCHITECTURE.md` (full update — system diagram, container modes, S3 structure, infrastructure table, key design decisions)
- [ ] Update `frontend/kenya_config/layers.json` with newly generated prediction dates

**Test gate:** PASSED — grep returns zero SageMaker/MLflow/SSM matches. `sagemaker-trigger.yaml` archived. `ARCHITECTURE.md` fully rewritten. 53/53 tests pass. `layers.json` update pending Phase 7 (needs real prediction dates from E2E run).  
**Status: COMPLETE (except layers.json — pending Phase 7)**

---

## Phase 9 — Archive Working Docs
> Once Phase 8 complete and `ARCHITECTURE.md` updated.

- [ ] Move `docs/PLAN_INFERENCE_INTEGRATION.md` → `docs/archive/`
- [ ] Move `docs/PROGRESS_INFERENCE_INTEGRATION.md` → `docs/archive/`
- [ ] Confirm `docs/ARCHITECTURE.md` is the single source of truth

**Status: NOT STARTED**
