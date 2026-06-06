[README_File.md](https://github.com/user-attachments/files/28671388/README_File.md)
# Multi-Hazard Data Pipeline

Automated pipeline that downloads, processes, and uploads four US natural hazard datasets to **Mapbox** as state-level tilesets.

| Dataset | Source | Format | Tileset naming |
|---|---|---|---|
| FEMA Flood Zones | FEMA ArcGIS REST API | GeoJSON (vector) | `<user>.fema-tx` |
| Storm Surge (SLOSH) | NOAA NHC GeoTIFF | Raster per category | `<user>.surge-cat3-fl` |
| Fault Lines | USGS NSHM ArcGIS REST | GeoJSON (vector) | `<user>.faults-ca` |
| Fire Burn Probability | USFS RDS-2020-0016-2 | Raster per state | `<user>.fire-bp-wa` |

---

## Repository Structure

```
hazard-pipeline/
├── hazard_pipeline.py        ← main composite pipeline (all 4 datasets)
├── fema_pipeline_simplified.py  ← standalone FEMA-only pipeline
└── README.md
```

---

## Requirements

### Python version
Python 3.9 or higher recommended.

### Install dependencies
```bash
pip install curl_cffi shapely tqdm requests geopandas rasterio numpy pyproj boto3
```

| Package | Purpose |
|---|---|
| `curl_cffi` | Chrome TLS fingerprint — required to bypass FEMA bot detection |
| `shapely` | Geometry processing and simplification |
| `geopandas` | Reading Census state boundary shapefiles |
| `rasterio` | Clipping, reprojecting, and saving GeoTIFF rasters |
| `numpy` | Array operations for raster data |
| `pyproj` | CRS transformations for raster clipping |
| `tqdm` | Progress bars |
| `boto3` | Fast S3 upload to Mapbox (optional but recommended for large files) |

---

## Mapbox Token Setup

The Uploads API requires a **secret token** (`sk.ey…`), not a public token (`pk.ey…`).

1. Go to [https://account.mapbox.com/access-tokens/](https://account.mapbox.com/access-tokens/)
2. Click **Create a token**
3. Enable these scopes:
   - `UPLOADS:READ`
   - `UPLOADS:WRITE`
   - `UPLOADS:LIST`
4. Copy the token — it starts with `sk.ey…`

> ⚠️ Never commit your secret token to Git.  
> Use an environment variable or a local `.env` file (see Security section below).

---

## Configuration

Open `hazard_pipeline.py` and edit the `CONFIGURATION` block at the top:

```python
# Which states to process
TARGET_STATES = None          # None = all 50 states
TARGET_STATES = ["WA"]        # single state
TARGET_STATES = ["TX", "FL"]  # multiple states

# Which datasets to run
DATASETS = ["fema", "storm_surge", "fault_lines", "fire_risk"]  # all
DATASETS = ["fault_lines"]    # one at a time

# Output folder (all files saved here)
OUTPUT_ROOT = r"E:\FEMA_ETL"   # Windows
OUTPUT_ROOT = "/data/hazards"  # Mac/Linux

# Mapbox credentials
MAPBOX_ACCESS_TOKEN = "sk.ey..."
MAPBOX_USERNAME     = "your_username"

# Geometry simplification (for vector datasets)
# 0.0001 ≈ 10 m detail  |  0.001 ≈ 100 m  |  0 = no simplification
FEMA_SIMPLIFY = 0.0001
```

---

## Running the Pipeline

### Run all datasets, all states
```bash
python hazard_pipeline.py
```

### Run a single dataset
Edit `DATASETS = ["fault_lines"]` then run:
```bash
python hazard_pipeline.py
```

### Run the standalone FEMA pipeline
```bash
python fema_pipeline_simplified.py
```

---

## Recommended Test Order

Run one dataset at a time with a small state before doing a full run.

### Step 1 — Fault Lines (fastest, ~30 seconds)
```python
TARGET_STATES = ["WA"]
DATASETS      = ["fault_lines"]
```
Tests ArcGIS pagination, state assignment, GeoJSON output, and Mapbox upload end-to-end.

### Step 2 — FEMA (a few minutes)
```python
TARGET_STATES = ["WA"]
DATASETS      = ["fema"]
```

### Step 3 — Fire Risk
```python
TARGET_STATES = ["WA"]
DATASETS      = ["fire_risk"]
```
Tests rasterio reproject and Mapbox GeoTIFF upload.

### Step 4 — Storm Surge (use FL, not WA)
```python
TARGET_STATES = ["FL"]   # WA has no surge coverage and will silently skip
DATASETS      = ["storm_surge"]
```
Downloads a ~200 MB zip from NHC once; subsequent runs reuse the cached file.

### Step 5 — Two-state combined test
```python
TARGET_STATES = ["WA", "FL"]
DATASETS      = ["fema", "storm_surge", "fault_lines", "fire_risk"]
```

### Full run
```python
TARGET_STATES = None
DATASETS      = ["fema", "storm_surge", "fault_lines", "fire_risk"]
```

---

## Output Structure

After a full run, `OUTPUT_ROOT` will contain:

```
E:\FEMA_ETL\
├── fema\
│   ├── fema_flood_wa.geojson
│   ├── fema_flood_tx.geojson
│   └── ...
├── storm_surge\
│   ├── US_SLOSH_MOM_v4.zip          ← cached download (can delete after run)
│   ├── extracted\                   ← raw TIFs (can delete after run)
│   ├── surge_cat1_fl.tif
│   ├── surge_cat2_fl.tif
│   └── ...
├── fault_lines\
│   ├── faults_wa.geojson
│   └── ...
└── fire_risk\
    ├── fire_bp_wa.tif
    ├── fire_bp_tx.tif
    └── ...
```

---

## Mapbox Tileset Names

Once uploaded, tilesets appear at [https://studio.mapbox.com/tilesets/](https://studio.mapbox.com/tilesets/) with these IDs:

| Dataset | Tileset ID pattern | Example |
|---|---|---|
| FEMA Flood | `<user>.fema-<state>` | `bjgaines.fema-tx` |
| Storm Surge Cat 1 | `<user>.surge-cat1-<state>` | `bjgaines.surge-cat1-fl` |
| Storm Surge Cat 3 | `<user>.surge-cat3-<state>` | `bjgaines.surge-cat3-fl` |
| Fault Lines | `<user>.faults-<state>` | `bjgaines.faults-ca` |
| Fire Burn Prob | `<user>.fire-bp-<state>` | `bjgaines.fire-bp-wa` |

---

## Data Sources

| Dataset | Source | License |
|---|---|---|
| FEMA Flood Zones | [FEMA NFHL](https://hazards.fema.gov/arcgis/rest/services/FIRMette/NFHLREST_FIRMette/MapServer/20) | Public domain |
| Storm Surge | [NOAA NHC SLOSH MOM v4](https://www.nhc.noaa.gov/nationalsurge/) | Public domain |
| Fault Lines | [USGS NSHM 2023/2025](https://services.arcgis.com/v01gqwM5QqNysAAi/ArcGIS/rest/services/USGS_NSHM_Features/FeatureServer/5) | Public domain |
| Fire Burn Probability | [USFS RDS-2020-0016-2](https://www.fs.usda.gov/rds/archive/catalog/RDS-2020-0016-2) | Public domain |
| State Boundaries | [US Census Bureau TIGER 2023](https://www2.census.gov/geo/tiger/GENZ2023/shp/) | Public domain |

---

## Known Issues & Notes

- **FEMA TLS fingerprinting** — FEMA's server blocks standard Python HTTP clients. The pipeline uses `curl_cffi` with `impersonate="chrome124"` to mimic a real browser TLS handshake. Do not replace with `requests`.
- **Storm surge coverage** — SLOSH data only covers the US Gulf and East Coasts, Hawaii, and a few territories. States like WA, OR, CA (north) will produce empty outputs and are silently skipped.
- **Mapbox 300 MB upload limit** — large states (TX, FL, LA) may produce FEMA GeoJSON files over 300 MB. Increase `FEMA_SIMPLIFY` to `0.001` to reduce file size if uploads fail.
- **Fire risk coverage** — not all 50 states have burn probability data. The pipeline checks the USFS catalog and skips states with no data automatically.
- **Full run time** — expect 2–6 hours for all datasets across all states depending on your internet speed.

---
