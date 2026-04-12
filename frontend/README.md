# Frontend — Prism Kenya Configuration

This directory contains the Kenya-specific configuration files that get injected into WFP's [Prism](https://github.com/WFP-VAM/prism-app) frontend during deployment.

## Files

```
kenya_config/
├── prism.json                  # Country settings, map center, layer categories
├── layers.json                 # Layer definitions (types, URLs, legends, dates)
└── admin_boundaries.geojson    # Ward boundaries with pcode, first_prov, first_dist

patches/
└── 0001-support-hyphenated-date-format-in-static-raster-urls.patch
```

## How deployment works

Prism source is **not** checked into this repo. During deployment (`infra/deploy-all.sh`):

1. Prism is cloned at a pinned commit from `https://github.com/WFP-VAM/prism-app.git`
2. Kenya config files are copied into `frontend/src/config/kenya/`
3. Kenya is registered in Prism's config index
4. Admin boundaries are copied to `frontend/public/data/kenya/`
5. Patches from `patches/` are applied (e.g., hyphenated date format support)
6. Prism common library is built
7. Frontend is built with `REACT_APP_COUNTRY=kenya`
8. Build output is deployed to AWS Amplify

## Layer types

**`static_raster`** — COG tiles served via TiTiler. URL contains `{YYYY-MM-DD}` date template. Used for satellite data and prediction rasters.

**`admin_level_data`** — Fetches JSON from the serve API, joins to boundary polygons by `pcode`, colors wards by a numeric field, shows tooltip on click. URL contains `{YYYY_MM_DD}` date template. Used for ward prediction details.

## Updating layer dates

After new data is ingested or predictions are generated, update the `dates` arrays in `layers.json` to include the new dates. The dates must match the folder names in S3 (e.g., `2026-01-17` for rasters, `2026_04_01` for predictions).

## Local development

To test Prism locally with Kenya config:

```bash
# Clone prism-app
git clone https://github.com/WFP-VAM/prism-app.git /tmp/prism-test
cd /tmp/prism-test
git checkout 6f22f3b6063ad813f3277fa312b23bb0c9bbbab0

# Inject config (same steps as deploy-all.sh)
mkdir -p frontend/src/config/kenya
cp /path/to/this/repo/frontend/kenya_config/layers.json frontend/src/config/kenya/
cp /path/to/this/repo/frontend/kenya_config/prism.json frontend/src/config/kenya/
cp /path/to/this/repo/frontend/kenya_config/admin_boundaries.geojson \
   frontend/public/data/kenya/ken_bnd_adm3_WFP.json

# Build and run
cd common && yarn install && yarn build && cd ../frontend
yarn install
REACT_APP_COUNTRY=kenya yarn start
```
