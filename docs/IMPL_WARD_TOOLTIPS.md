# Implementation: Ward Prediction Tooltips

## Goal
When a user clicks a ward in Prism, show prediction metadata (loss ratio, confidence, risk level, top features) in a tooltip. Data is served dynamically from S3 — no frontend redeploy needed when new predictions arrive.

## Architecture
```
S3: predictions/livestock-mortality/{date}/ward_predictions.geojson
        │
        ▼
Serve container: GET /predictions/livestock-mortality/{date}
   - Reads GeoJSON from S3
   - Extracts properties, maps ADM3_EN → shapeName
   - Flattens top_features into top_feature_1, top_feature_2, ...
   - Returns JSON array
        │
        ▼
CloudFront → Prism (admin_level_data layer)
   - Substitutes selected date into path URL
   - Fetches JSON, joins to boundary polygons by shapeName
   - Colors wards by loss ratio
   - Click → tooltip with featureInfoProps
```

## Steps

### 1. Backend: Add predictions endpoint to serve container
- [x] **1a.** Add route `GET /predictions/livestock-mortality/{date}` to `serve/routes.py`
  - Read `ward_predictions.geojson` from S3
  - Extract feature properties into flat JSON array
  - Map `ADM3_EN` → `shapeName` (join key for boundaries)
  - Flatten `top_features` array into `top_feature_1`, `top_feature_1_importance`, etc.
  - Handle 3 name mismatches: Sagante/Jaldessa→Sagante/Jaldesa, Logologo→Logologo/Marsabit Central, Marsabet central→Logologo/Marsabit Central
- [x] **1b.** Add route `GET /predictions/livestock-mortality/dates` to `serve/routes.py`
  - List S3 folders under `predictions/livestock-mortality/`
  - Return sorted date list
- [x] **1c.** Verify locally: `curl localhost:8000/predictions/livestock-mortality/2026_04_01`

### 2. Frontend: Add admin_level_data layer to layers.json
- [x] **2a.** Add `predictions_ward_data` layer to `prism/kenya_config/layers.json`
  - type: `admin_level_data`
  - path: `https://d31fsorf4vwo9f.cloudfront.net/predictions/livestock-mortality/{YYYY_MM_DD}`
  - dates: `["2026_04_01"]`
  - adminCode: `shapeName`
  - adminLevel: 3
  - dataField: `mean_predicted_loss_ratio`
  - featureInfoProps for: loss ratio, confidence, risk level, top features
- [x] **2b.** Add to predictions category in `prism/kenya_config/prism.json`

### 3. Deploy & verify
- [x] **3a.** Deploy backend: `./scripts/deploy-all.sh --skip-frontend`
- [x] **3b.** Test endpoint: `curl https://d31fsorf4vwo9f.cloudfront.net/predictions/livestock-mortality/2026_04_01` — 16 wards, all fields present
- [x] **3c.** Deploy frontend: `./scripts/deploy-all.sh --skip-backend`
- [ ] **3d.** Verify in Prism: select ward details layer, click a ward, see tooltip

## Name Mapping (ADM3_EN → shapeName)
| Prediction (ADM3_EN) | Boundary (shapeName) |
|----------------------|---------------------|
| Sagante/Jaldessa | Sagante/Jaldesa |
| Logologo | Logologo/Marsabit Central |
| Marsabet central | Logologo/Marsabit Central |
| (other 13 wards) | exact match |

Note: "Logologo" and "Marsabet central" both map to the same boundary feature — they may be referring to the same ward. Need to verify with the team whether this is a split ward or a naming inconsistency. For now, map both to "Logologo/Marsabit Central".
