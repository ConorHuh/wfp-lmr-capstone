# Quickstart — Run LMR Locally on Mac

## Before you start
Note: The data sources outside Planetary Computer were necessary for static datasets and two layers not available in PC. All of these data sources are free. NASA Earthdata and Copernicus CDS require registration.
You need:

- **Docker**, installed and running, with **at least 8 GB memory**
  allocated.
- A **NASA Earthdata account** — free signup at
  https://urs.earthdata.nasa.gov/users/new
- A **Copernicus CDS account** with the **ERA5-Land licence accepted** —
  free signup at https://cds.climate.copernicus.eu/, then accept the
  licence at
  https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land-monthly-means?tab=download#manage-licences
- The **model bundle folder**

## 1. Get the repository

```bash
cd ~
git clone -b abhas-local-testing https://github.com/ConorHuh/wfp-lmr-capstone.git
cd wfp-lmr-capstone
```

## 2. Place the bundle at `~/lmr-bundle/`

Verify the structure:

```bash
ls ~/lmr-bundle
```

You should see exactly two entries:
```
geoBoundaries-KEN-ADM3.geojson
inference_bundle
```

## 3. Create the credentials file

Still inside the repo directory:

```bash
cat > .env <<EOF
EARTHDATA_USERNAME=your_earthdata_username
EARTHDATA_PASSWORD=your_earthdata_password
CDSAPI_URL=https://cds.climate.copernicus.eu/api
CDSAPI_KEY=your_cds_personal_access_token
EOF
chmod 600 .env
```

Open `.env` in any text editor and replace the four placeholder values
with the credentials from your NASA Earthdata and Copernicus CDS
accounts.

## 4. Run the test

```bash
# 2-month smoke test (~30-60 min)
./infra/test-pipeline.sh --window 2months

# Full 24-month run (several hours)
./infra/test-pipeline.sh --window 2years

# Re-run faster after the first pass (skip uploading the bundle):
./infra/test-pipeline.sh --window 2months --skip-bootstrap

# Re-run feature/infer only without re-pulling raw data:
./infra/test-pipeline.sh --window 2months --skip-bootstrap --skip-ingest
```
If you see permissions error: use chmod +x infra/*.sh

When you see **`✓ ALL GREEN`** at the end, open
http://localhost:3000 in your browser.
