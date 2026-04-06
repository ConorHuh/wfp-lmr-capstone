# Plan: Ward Prediction Tooltips

How to display prediction metadata (top features, confidence score, loss ratio) when a user clicks or hovers over a ward in Prism.

## Problem

The raster COG only has 3 bands (risk level, confidence, loss ratio) — structured data like "top 5 features with importance scores" can't be stored in raster pixels. The full metadata lives in `ward_predictions.geojson`:

```
mean_predicted_loss_ratio, median_predicted_loss_ratio, max_predicted_loss_ratio,
confidence, risk_level, n_observations,
top_features: [{feature, importance}, ...]
```

## Options

### Option 1: Enrich the admin boundaries GeoJSON

Join prediction properties directly onto `admin_boundaries.geojson`. Each ward polygon already has `pcode` — match on `ADM3_PCODE` from `ward_predictions.geojson`. When Prism renders the boundary layer, clicking a ward shows all its properties in the tooltip.

**Pros:**
- Simple, no backend changes
- Works with Prism's existing boundary click behavior
- No additional network requests

**Cons:**
- Boundaries become prediction-date-specific (what happens with multiple prediction dates?)
- Mixes static boundary data with dynamic prediction data
- Need to re-merge every time predictions update

### Option 2: Serve predictions GeoJSON as its own vector layer

Add `ward_predictions.geojson` as a separate Prism layer (vector overlay on top of the raster). Click/hover on the polygon shows the full prediction detail.

**Pros:**
- Clean separation of boundaries vs predictions
- Supports multiple prediction dates naturally
- Each prediction date gets its own GeoJSON

**Cons:**
- Need to verify Prism supports custom vector layers with tooltip properties out of the box
- Two overlapping polygon layers (boundaries + predictions) may cause click conflicts

### Option 3: API endpoint on the serve container

Add a `/predictions/ward/{pcode}` endpoint that reads the GeoJSON from S3 and returns the full prediction record. Frontend calls it on ward click.

**Pros:**
- Most flexible, cleanly decoupled
- Supports any future metadata without changing layer config
- Multiple prediction dates handled naturally

**Cons:**
- More work (new endpoint + frontend customization to call it)
- Adds a network round-trip on each click
- Requires Prism patching or custom JS to wire up the click handler

## Recommendation

**Short term (demo):** Option 1 — merge prediction properties into admin boundaries. Fast, works today, 16 wards is trivially small.

**Long term (production):** Option 3 — API endpoint. Cleanly decouples prediction metadata from boundary geometry, scales to multiple dates and richer interactivity.
