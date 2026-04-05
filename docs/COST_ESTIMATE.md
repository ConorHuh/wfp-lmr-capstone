# LMR Data Platform — AWS Cost Estimate

*Prepared: April 2026*

## Architecture Overview

```
EventBridge cron (8-day) → Fargate Ingest → S3 (COGs + zonal stats)
                                                │
                          Fargate Serve (Spot) ← reads COGs + model → CloudFront (HTTPS) → Prism (Amplify)
```

All infrastructure runs in the default VPC with public subnets. No NAT Gateway, no SageMaker endpoints needed.

---

## 1. S3 Storage

Based on actual data currently stored in `lmr-data-cogs-dev`:

| Dataset | Avg File Size | Cadence | Dates/Year | GB/Year |
|---------|--------------|---------|------------|---------|
| modis-ndvi | 30.8 MB | 16-day | 23 | 0.69 |
| modis-evi | 30.3 MB | 16-day | 23 | 0.68 |
| modis-lai | 2.2 MB | 8-day | 46 | 0.10 |
| modis-fpar | 2.9 MB | 8-day | 46 | 0.13 |
| modis-lst-day | 1.8 MB | 8-day | 46 | 0.08 |
| modis-lst-night | 1.5 MB | 8-day | 46 | 0.07 |
| modis-sr (4 bands) | 31.3 MB | 8-day | 46 | 1.41 |
| modis-et | ~2.9 MB | yearly | 1 | <0.01 |
| modis-pet | ~2.9 MB | yearly | 1 | <0.01 |
| modis-gpp | 5.5 MB | 8-day | 46 | 0.25 |
| **Total** | | | | **3.41** |

Additional storage (negligible):
- Zonal stats JSON: ~1 MB/year
- Manifests: <1 MB/year
- Model artifacts: ~10 MB (one-time)
- Prediction COGs: ~50 MB/year

### S3 Cost

| Timeframe | Storage | Monthly Cost | Annual Cost |
|-----------|---------|-------------|-------------|
| 1 year of data | 3.4 GB | $0.08 | $0.94 |
| 3 years of data | 10.2 GB | $0.23 | $2.82 |
| 5 years of data | 17.1 GB | $0.39 | $4.72 |

S3 Standard pricing: $0.023/GB/month. **Storage cost is negligible.**

S3 request costs (PUT/GET) add ~$1-2/month for ingest uploads and tile serving reads.

---

## 2. Fargate Compute

### Serve Container (always-on, Fargate Spot)

| Resource | Spec | Spot Price |
|----------|------|-----------|
| vCPU | 2 | $0.01214/hr |
| Memory | 8 GB | $0.001334/hr |

| Period | Cost |
|--------|------|
| Monthly | $25.51 |
| Annual | $306.15 |

Fargate Spot provides a 70% discount vs standard Fargate ($85.06/month). The tradeoff is potential interruptions — AWS can reclaim capacity with 2 minutes notice. ECS automatically relaunches the task. Expected downtime: seconds to ~1 minute per interruption, infrequent in practice.

### Ingest Container (scheduled, standard Fargate)

| Resource | Spec | Standard Price |
|----------|------|---------------|
| vCPU | 1 | $0.04048/hr |
| Memory | 4 GB | $0.004445/hr |

Runs every 8 days (~3.75 times/month), ~3 hours per run.

| Period | Cost |
|--------|------|
| Monthly | $0.66 |
| Annual | $7.87 |

### Fargate Total

| Period | Cost |
|--------|------|
| Monthly | $26.17 |
| Annual | $314.02 |

---

## 3. Application Load Balancer (ALB)

| Component | Cost |
|-----------|------|
| Fixed ($0.0225/hr × 730 hr) | $16.20/month |
| LCU charges (~1 LCU avg) | $5.84/month |
| **Monthly** | **$22.04** |
| **Annual** | **$264.48** |

---

## 4. CloudFront CDN

CloudFront sits in front of the ALB to provide HTTPS for the Amplify frontend (avoids mixed content blocking).

Cost is traffic-dependent:

| Scenario | Requests/Month | Data Transfer | Monthly Cost |
|----------|---------------|---------------|-------------|
| Light (few users, ~10K tiles/day) | 300K | 60 GB | ~$8 |
| Moderate (regular use, ~100K tiles/day) | 3M | 600 GB | ~$54 |
| Heavy (many concurrent users) | 10M+ | 2+ TB | ~$180+ |

CloudFront pricing: $0.085/GB transfer (first 10 TB), $0.01/10K HTTPS requests.

---

## 5. AWS Amplify (Frontend Hosting)

| Component | Cost |
|-----------|------|
| Build minutes | Free tier (1,000 min/month) |
| Hosting storage | Free tier (5 GB) |
| Data transfer | Free tier (15 GB/month) |
| **Monthly** | **$0** |

Static site well within free tier.

---

## 6. Other Services

| Service | Monthly Cost | Notes |
|---------|-------------|-------|
| ECR (container images) | $0.50 | ~5 GB stored at $0.10/GB |
| CloudWatch Logs | $2.00 | Log ingestion + storage |
| EventBridge | $0.00 | Free tier (scheduled rules) |
| SSM Parameter Store | $0.00 | Standard parameters free |
| **Subtotal** | **$2.50** | |

---

## Total Cost Summary

### Decoupled from SageMaker (recommended)

| Service | Monthly | Annual |
|---------|---------|--------|
| S3 Storage (3yr history) | $0.23 | $2.82 |
| Fargate Serve (Spot) | $25.51 | $306.15 |
| Fargate Ingest (Standard) | $0.66 | $7.87 |
| ALB | $22.04 | $264.48 |
| CloudFront | $8 - $54 | $96 - $648 |
| Other (ECR, CW) | $2.50 | $30.00 |
| Amplify | $0.00 | $0.00 |
| **Total** | **$59 - $105** | **$707 - $1,259** |

### Cost Breakdown by Category

| Category | % of Total (moderate traffic) |
|----------|------------------------------|
| Compute (Fargate) | 25% |
| Networking (ALB + CloudFront) | 72% |
| Storage (S3 + ECR) | 1% |
| Observability (CloudWatch) | 2% |

---

## Comparison: With vs Without SageMaker

| Component | Decoupled | With SageMaker |
|-----------|-----------|---------------|
| SageMaker endpoint | $0 | $36-73/month |
| NAT Gateway | $0 | $43/month |
| Lambda trigger | $0 | <$1/month |
| Cross-bucket IAM | None | Required |
| **Additional cost** | **$0** | **$79-117/month** |

Decoupling from SageMaker saves **$950-$1,400/year**.

---

## Cost Optimization Options

| Optimization | Savings | Tradeoff |
|-------------|---------|----------|
| Downsize serve to 1 vCPU / 4 GB | ~$13/month | Less headroom for concurrent tile requests |
| Schedule serve (business hours only) | ~$17/month | No access outside hours |
| Remove CloudFront, use ACM + ALB HTTPS | ~$8-54/month | Requires a custom domain |
| Use S3 Intelligent-Tiering | ~$0 savings | Data too small to matter |
| Remove ALB, use CloudFront → Fargate direct | ~$22/month | More complex setup |

---

## Scaling Considerations

### Adding more datasets
Each new MODIS dataset adds ~0.1-1.4 GB/year to S3 (depending on resolution). Storage cost impact: <$0.40/year per dataset. Negligible.

### Adding more countries
Kenya's bounding box is ~9° × 10°. Larger countries produce proportionally larger COGs. A continent-wide deployment would multiply storage ~10-50x (still under $15/year for S3) and increase ingest runtime (may need larger Fargate task or parallelization).

### Model size growth
If the team moves to a larger ensemble model:

| Model Size | Fargate Config | Spot Cost/Month | Notes |
|-----------|---------------|-----------------|-------|
| < 2 GB | 2 vCPU / 8 GB (current) | $25.51 | No changes needed |
| 2-5 GB | 4 vCPU / 16 GB | ~$51 | Update task definition |
| 5+ GB | 4 vCPU / 30 GB + EFS | ~$55 + EFS | Add EFS for persistent model cache |

Current model (XGBoost, ~5-10 MB) requires no infrastructure changes.
