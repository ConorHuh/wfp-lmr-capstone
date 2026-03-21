---
name: team_structure_and_aws
description: Team roles, AWS account details, and how each teammate connects to AWS/SageMaker
type: project
---

## Team Structure (as of 2026-03-21)

**Conor** — owns the Docker container (lmr-container), CloudFormation infra, ingestion pipeline, and serve API. Also responsible for Prism frontend integration.

**Abhas** — owns feature engineering pipeline (marsabit_feature_pipeline.py) and built the local Prism frontend demo (TiTiler + PRISM fork on localhost). Also extracted pixel parquets from Planetary Computer to S3.

**Third teammate (pipeline/)** — owns the SageMaker training pipeline (XGBoost), preprocessing (preprocess.py), and target preparation (prepare_targets.py). Runs in SageMaker Studio.

## AWS Account
- Account ID: 575108933641
- Region: us-east-1
- SageMaker Domain: d-lf2eivtg2b7i
- MLflow Tracking Server ARN: arn:aws:sagemaker:us-east-1:575108933641:mlflow-tracking-server/lmr-tracking-server-5t7l23o0xvt99j-chws71x3trpelj-dev

## S3 Buckets
- `lmr-data-cogs-dev` — Conor's ingestion bucket (COGs, stats, manifests, predictions)
- `amazon-sagemaker-575108933641-us-east-1-c422b90ce861` — SageMaker default bucket (training data, pixel parquets, IBLI data)
- `lmr-capstone-s3bucket` — shared bucket with column mappings, etc.

## Key Data Paths
- Pixel parquets: `s3://amazon-sagemaker-.../dzd-.../dev/data/training/planetary_computer/*.parquet`
- Target data: `s3://amazon-sagemaker-.../dzd-.../dev/data/training/ibli/target_data_pipeline.csv`
- Engineered features: `s3://amazon-sagemaker-.../dzd-.../dev/data/training/planetary_computer/pc_features_engineered.parquet`
- Training dataset: `s3://amazon-sagemaker-.../dzd-.../dev/data/training/processed/monthly_pred_2008_2016_loss_ratio.parquet`

**Why:** Understanding team ownership and AWS topology is critical for integration work.
**How to apply:** When building integration, respect bucket boundaries and data contracts between teammates.
