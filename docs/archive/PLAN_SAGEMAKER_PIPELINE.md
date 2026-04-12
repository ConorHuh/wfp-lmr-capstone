# LMR SageMaker Pipeline — Integration Plan

## Overview

This document defines how the SageMaker pipeline integrates with the broader LMR data platform. It is intended as a **handoff document** for the colleague owning the ML pipeline work. The internal pipeline steps (feature engineering, model training, etc.) are out of scope here — this plan focuses on **inputs, outputs, triggers, and integration contracts**.

---

## 1. Where This Pipeline Fits

```
                        THIS DOCUMENT'S SCOPE
                        ┌─────────────────────┐
                        │                     │
EventBridge ──▶ Ingest  │  S3 Event ──▶ SageMaker  │──▶ S3 (predictions) ──▶ Serve Container
  (8 days)     Container│    Notification  Pipeline    │      (COGs)            (Fargate)
                        │                     │
                        └─────────────────────┘
```

**Upstream:** The ingest container writes new satellite COGs to S3 and drops a manifest file.
**Downstream:** The serve container reads prediction COGs from S3 and serves presigned URLs to Prism.

---

## 2. Trigger Mechanism

### S3 Event Notification → EventBridge → SageMaker Pipeline

When the ingest container completes a run, it writes a manifest file to:

```
s3://lmr-data-cogs/manifests/ingest-{timestamp}.json
```

**CloudFormation will configure:**
1. An S3 event notification on the `lmr-data-cogs` bucket for `s3:ObjectCreated:*` events with the prefix `manifests/`
2. This event is routed to EventBridge (S3 → EventBridge integration)
3. An EventBridge rule matches the event and triggers the SageMaker pipeline execution

```yaml
# EventBridge rule pattern (defined in CloudFormation)
{
  "source": ["aws.s3"],
  "detail-type": ["Object Created"],
  "detail": {
    "bucket": { "name": ["lmr-data-cogs"] },
    "object": { "key": [{ "prefix": "manifests/" }] }
  }
}
```

### Alternative: Lambda Trigger

If EventBridge → SageMaker direct invocation is too complex, a thin Lambda function can sit between:

```
S3 Event → Lambda → sagemaker.start_pipeline_execution()
```

The Lambda reads the manifest, extracts the list of new files, and passes them as pipeline parameters.

---

## 3. Integration Contracts

### 3.1 Input Contract — What the Pipeline Receives

**Manifest file** (JSON, written by ingest container):

```json
{
  "run_id": "ingest-2026-03-09T00:00:00Z",
  "timestamp": "2026-03-09T01:23:45Z",
  "datasets_processed": [
    {
      "name": "ndvi-sentinel2",
      "items_ingested": 2,
      "s3_keys": [
        "ingested/ndvi-sentinel2/2026-03-09/B04.tif",
        "ingested/ndvi-sentinel2/2026-03-09/B08.tif"
      ],
      "date_range": {
        "start": "2026-03-01",
        "end": "2026-03-09"
      },
      "stac_items": ["S2A_MSIL2A_20260309T073611_..."]
    },
    {
      "name": "rainfall-chirps",
      "items_ingested": 1,
      "s3_keys": [
        "ingested/rainfall-chirps/2026-03-05/precip.tif"
      ],
      "date_range": {
        "start": "2026-03-01",
        "end": "2026-03-09"
      }
    }
  ],
  "status": "success"
}
```

**Raw COG files** in S3:

```
s3://lmr-data-cogs/ingested/{dataset-name}/{date}/{asset}.tif
```

**Existing model artifacts** (already in S3 from SageMaker training):
- Model artifacts in the SageMaker default bucket
- Model endpoint ARN stored in SSM Parameter Store at `/lmr/model/endpoint-arn`

### 3.2 Output Contract — What the Pipeline Must Produce

**Prediction COGs** written to S3:

```
s3://lmr-data-cogs/predictions/{model-name}/{date}/prediction.tif
```

| Field | Convention |
|-------|-----------|
| Bucket | `lmr-data-cogs` (same bucket as ingested data) |
| Prefix | `predictions/` |
| Model name | e.g., `livestock-mortality` |
| Date | ISO date matching the input data period, e.g., `2026-03-09` |
| File format | Cloud Optimized GeoTIFF (COG) |
| CRS | `EPSG:4326` |
| Spatial extent | Marsabit County bbox (same as ingest AOI) |

**Prediction manifest** (optional but recommended):

```json
{
  "pipeline_execution_id": "execution-abc123",
  "timestamp": "2026-03-09T03:45:00Z",
  "trigger_manifest": "manifests/ingest-2026-03-09T00:00:00Z.json",
  "model_endpoint": "arn:aws:sagemaker:us-east-1:123456789:endpoint/lmr-model-v2",
  "predictions": [
    {
      "model": "livestock-mortality",
      "date": "2026-03-09",
      "s3_key": "predictions/livestock-mortality/2026-03-09/prediction.tif",
      "metrics": {
        "mean_prediction": 0.23,
        "coverage_pct": 98.5
      }
    }
  ],
  "status": "success"
}
```

Written to: `s3://lmr-data-cogs/manifests/prediction-{timestamp}.json`

---

## 4. SageMaker Pipeline Structure

The colleague owns the internal implementation. Below is the **recommended structure** for integration compatibility.

### 4.1 Pipeline Definition

The pipeline should be defined as a SageMaker Pipeline (not just notebooks):

```python
# pipeline_definition.py (colleague's responsibility)
from sagemaker.workflow.pipeline import Pipeline
from sagemaker.workflow.steps import ProcessingStep, TransformStep
from sagemaker.workflow.parameters import ParameterString

pipeline = Pipeline(
    name="lmr-prediction-pipeline",
    parameters=[
        ParameterString(name="ManifestS3Uri"),
        ParameterString(name="InputBucket", default_value="lmr-data-cogs"),
        ParameterString(name="OutputPrefix", default_value="predictions"),
    ],
    steps=[
        # Step 1: Read manifest, load new COGs
        # Step 2: Feature engineering
        # Step 3: Run inference (batch transform or endpoint)
        # Step 4: Post-process predictions to COG format
        # Step 5: Write prediction COGs to S3
        # Step 6: Write prediction manifest to S3
    ],
)
```

### 4.2 Converting Notebooks to Pipeline Steps

Current state: Processing logic lives in Jupyter notebooks.
Target state: Each notebook becomes a SageMaker Processing Step.

| Notebook | Pipeline Step | Processor |
|----------|--------------|-----------|
| Data loading / feature engineering | `ProcessingStep` | `SKLearnProcessor` or `ScriptProcessor` |
| Model inference | `TransformStep` (batch) or `ProcessingStep` calling endpoint | `Transformer` or `ScriptProcessor` |
| Post-processing / COG generation | `ProcessingStep` | `ScriptProcessor` with rasterio |

**Key conversion steps for the colleague:**

1. Extract notebook cells into standalone Python scripts
2. Define inputs/outputs as S3 URIs (not local paths)
3. Wrap each script in a `ProcessingStep` with appropriate instance type
4. Chain steps with data dependencies
5. Add the `ManifestS3Uri` parameter as the pipeline entry point

### 4.3 Recommended Instance Types

| Step | Instance | Reason |
|------|----------|--------|
| Feature engineering | `ml.m5.xlarge` | CPU-bound, moderate memory |
| Batch inference | `ml.m5.xlarge` or `ml.g4dn.xlarge` | Depends on model type |
| COG post-processing | `ml.m5.large` | Light rasterio work |

---

## 5. SSM Parameter Store Integration

The pipeline should read/write the following SSM parameters:

| Parameter Path | Owner | Description |
|---------------|-------|-------------|
| `/lmr/model/endpoint-arn` | Colleague (pipeline) | Latest deployed model endpoint ARN |
| `/lmr/model/version` | Colleague (pipeline) | Current model version string |
| `/lmr/pipeline/last-execution` | Pipeline (auto) | Timestamp of last successful pipeline run |
| `/lmr/ingest/last-run` | Ingest container | Timestamp of last successful ingest |

---

## 6. CloudFormation Resources (provided by main stack)

The main CloudFormation stack will provision the following resources that the pipeline depends on:

```yaml
# These resources are created by the main LMR stack
# The colleague's pipeline should reference them

Resources to be provided:
  - S3 bucket: lmr-data-cogs (with event notifications)
  - EventBridge rule: triggers pipeline on manifest upload
  - IAM role: lmr-sagemaker-pipeline-role
    Permissions:
      - s3:GetObject, s3:PutObject on lmr-data-cogs
      - sagemaker:InvokeEndpoint
      - ssm:GetParameter, ssm:PutParameter on /lmr/*
      - logs:CreateLogStream, logs:PutLogEvents
  - SSM parameters: /lmr/model/* namespace reserved
```

The colleague needs to:
1. Create the SageMaker Pipeline definition
2. Register it with SageMaker
3. Provide the pipeline ARN so the EventBridge rule can target it

---

## 7. Testing the Integration

### 7.1 End-to-End Test

```bash
# 1. Manually upload a test manifest to trigger the pipeline
aws s3 cp test-manifest.json s3://lmr-data-cogs/manifests/test-ingest.json

# 2. Verify EventBridge rule triggered
aws events list-rules --name-prefix lmr

# 3. Check SageMaker pipeline execution
aws sagemaker list-pipeline-executions --pipeline-name lmr-prediction-pipeline

# 4. Verify prediction COGs in S3
aws s3 ls s3://lmr-data-cogs/predictions/ --recursive

# 5. Verify serve container can find and return the prediction
curl "https://<alb-dns>/raster_geotiff?collection=livestock-mortality&date=2026-03-09"
```

### 7.2 Stub Pipeline for Development

Before the real pipeline is ready, a stub can be used:

```python
# stub_pipeline.py — drops a dummy prediction COG for integration testing
import boto3
import rasterio
import numpy as np
from rasterio.transform import from_bounds

def create_dummy_prediction(output_s3_key):
    """Create a dummy COG prediction for integration testing."""
    transform = from_bounds(36.0, 1.5, 42.0, 4.5, 100, 100)
    data = np.random.rand(1, 100, 100).astype(np.float32)

    with rasterio.open(
        "/tmp/prediction.tif", "w",
        driver="GTiff", height=100, width=100,
        count=1, dtype="float32",
        crs="EPSG:4326", transform=transform,
    ) as dst:
        dst.write(data)

    s3 = boto3.client("s3")
    s3.upload_file("/tmp/prediction.tif", "lmr-data-cogs", output_s3_key)
```

---

## 8. Timeline & Handoff Checklist

### What the main stack (Conor) provides:
- [x] S3 bucket with event notifications (in CloudFormation)
- [x] EventBridge rule for manifest → pipeline trigger
- [x] IAM role for pipeline execution
- [x] SSM parameter namespace (`/lmr/*`)
- [x] Manifest format specification (this document)
- [x] Prediction output format specification (this document)
- [x] Stub pipeline for integration testing

### What the colleague needs to deliver:
- [ ] Convert notebooks to standalone Python scripts
- [ ] Define SageMaker Pipeline with `ProcessingStep`s
- [ ] Accept `ManifestS3Uri` as pipeline input parameter
- [ ] Write prediction COGs to `s3://lmr-data-cogs/predictions/{model}/{date}/prediction.tif`
- [ ] Write prediction manifest to `s3://lmr-data-cogs/manifests/prediction-{timestamp}.json`
- [ ] Store model endpoint ARN in SSM at `/lmr/model/endpoint-arn`
- [ ] Provide pipeline ARN for EventBridge rule target
- [ ] Test end-to-end with a manually uploaded manifest

---

## 9. Architecture Decision: Batch Transform vs Endpoint Inference

The colleague should choose based on their model:

| Approach | When to use | Cost |
|----------|------------|------|
| **Batch Transform** | Model runs on full AOI raster at once. Prediction is a spatial output. | Pay per job, no idle cost. Recommended for dekadal schedule. |
| **Real-time Endpoint** | Model needs to be available for ad-hoc requests (not our case). | Always-on cost. Not recommended for this use case. |

**Recommendation:** Use **Batch Transform** or a **Processing Step that loads the model locally** and runs inference. Since predictions only run every 8-10 days, there's no need for an always-on endpoint. The endpoint ARN in SSM can be kept for future use if real-time needs arise, but for now batch is more cost-effective.
