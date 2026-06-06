"""
Multi-Hazard Data Pipeline
==========================
Downloads, processes, and uploads 4 hazard datasets to Mapbox:

  1. FEMA Flood Zones       – ArcGIS REST API (polygon, split by state)
  2. Storm Surge (SLOSH)    – NHC GeoTIFF rasters, one per hurricane category
                              (raster upload to Mapbox, split by state via clip)
  3. Fault Lines            – USGS NSHM ArcGIS REST API (polyline, split by state)
  4. Fire Risk (Burn Prob)  – USFS RDS-2020-0016-2 GeoTIFF per state
                              (raster upload to Mapbox)

Install:
    pip install curl_cffi shapely tqdm requests geopandas rasterio numpy

For Mapbox upload:
    pip install boto3          (faster S3 upload; falls back to requests if absent)
    Set MAPBOX_ACCESS_TOKEN (sk.ey...) and MAPBOX_USERNAME below.

Configuration:
    Edit the CONFIG block at the top.
    TARGET_STATES = None        → all states
    TARGET_STATES = ["WA","TX"] → selected states only
    DATASETS      = list of dataset keys to run (subset if you want)
"""

import os, sys, json, time, zipfile, io, tempfile, pathlib, shutil, urllib.request
import requests as std_requests
from curl_cffi import requests as cffi_requests
from shapely.geometry import MultiPolygon, Polygon, MultiLineString, LineString, mapping, shape
from shapely.strtree import STRtree
from tqdm import tqdm

# ══════════════════════════════════════════════════════════════════
#  CONFIGURATION  ← edit here
# ══════════════════════════════════════════════════════════════════

# Which states to process.  None = all.  e.g. ["WA", "TX", "FL"]
TARGET_STATES = ["WA"]

# Which datasets to run.  Remove any you don't want.
DATASETS = ["fema","fault_lines"]

# Root output directory.  Sub-folders are created per dataset.
OUTPUT_ROOT = r"path:\folder"

# Mapbox credentials  (secret token sk.ey…  with UPLOADS:READ/WRITE/LIST scope)
UPLOAD_TO_MAPBOX    = True
MAPBOX_ACCESS_TOKEN = "sk."
MAPBOX_USERNAME     = "bjgaines"

# ── FEMA ──────────────────────────────────────────────────────────
FEMA_SERVICE    = "https://hazards.fema.gov/arcgis/rest/services/FIRMette/NFHLREST_FIRMette/MapServer/20"
FEMA_BATCH_SIZE = 200
FEMA_SIMPLIFY   = 0.0001    # degrees; 0 = off

# ── Storm Surge ───────────────────────────────────────────────────
# NHC GeoTIFF  (Texas-to-Maine only; categories 1-5 are inside the zip)
SURGE_URL      = "https://www.nhc.noaa.gov/gis/hazardmaps/US_SLOSH_MOM_Inundation_v4.zip"
# Category TIF filenames inside the zip  (adjust if NHC renames them)
SURGE_CATEGORIES = {
    "cat1": "MOM_v4_Cat1.tif",
    "cat2": "MOM_v4_Cat2.tif",
    "cat3": "MOM_v4_Cat3.tif",
    "cat4": "MOM_v4_Cat4.tif",
    "cat5": "MOM_v4_Cat5.tif",
}
# pixel value 0 = no surge; 1-20 = 0-20 ft bins; 21 = >20 ft; 99 = levee
SURGE_SIMPLIFY = 0.0001

# ── Fault Lines ───────────────────────────────────────────────────
FAULT_SERVICE   = "https://services.arcgis.com/v01gqwM5QqNysAAi/ArcGIS/rest/services/USGS_NSHM_Features/FeatureServer/5"
FAULT_FIELDS    = ["FaultName", "State", "FaultDip", "DipDir", "Rake", "slip"]
FAULT_BATCH     = 2000

# ── Fire Risk (Burn Probability) ──────────────────────────────────
# USFS RDS-2020-0016-2: BP_*.tif per state, one zip per state.
# Base URL pattern:  <BASE>/<STATE>/Data/<STATE>_BP.tif
# Full catalog page: https://www.fs.usda.gov/rds/archive/catalog/RDS-2020-0016-2
FIRE_CATALOG_URL = "https://www.fs.usda.gov/rds/archive/catalog/RDS-2020-0016-2"
# We'll scrape the catalog for the actual zip download links per state.

# ══════════════════════════════════════════════════════════════════


# ─────────────────────────────────────────────
#  SESSIONS
# ─────────────────────────────────────────────

BROWSER_HEADERS = {
    "Accept": "application/json, */*; q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://hazards.fema.gov/",
    "Origin": "https://hazards.fema.gov",
}

def make_cffi_session():
    """Chrome-fingerprint session for FEMA (TLS fingerprinting bypass)."""
    s = cffi_requests.Session(impersonate="chrome124")
    s.headers.update(BROWSER_HEADERS)
    return s

def make_std_session():
    """Standard requests session for all other sources."""
    s = std_requests.Session()
    s.headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
    )
    return s


# ─────────────────────────────────────────────
#  US STATE BOUNDARIES
# ─────────────────────────────────────────────

_STATE_CACHE = None

def load_state_boundaries(filter_states=None):
    global _STATE_CACHE
    if _STATE_CACHE is None:
        url = "https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_state_500k.zip"
        print("   📦 Downloading US state boundaries...")
        tmp = tempfile.mkdtemp()
        with urllib.request.urlopen(url) as resp:
            zdata = resp.read()
        with zipfile.ZipFile(io.BytesIO(zdata)) as zf:
            zf.extractall(tmp)
        shp = next(pathlib.Path(tmp).glob("*.shp"))
        try:
            import geopandas as gpd
            gdf = gpd.read_file(shp)
            _STATE_CACHE = [
                (str(row.get("STUSPS", row.get("ABBREVN", ""))).upper(), row.geometry)
                for _, row in gdf.iterrows()
                if row.get("STUSPS") or row.get("ABBREVN")
            ]
        except ImportError:
            import shapefile as shpf
            sf = shpf.Reader(str(shp))
            flds = [f[0] for f in sf.fields[1:]]
            _STATE_CACHE = []
            for sr in sf.shapeRecords():
                rec  = dict(zip(flds, sr.record))
                abbr = rec.get("STUSPS") or rec.get("ABBREVN") or ""
                geom = shape(sr.shape.__geo_interface__)
                if abbr:
                    _STATE_CACHE.append((str(abbr).upper(), geom))
        print(f"   ✔ {len(_STATE_CACHE)} state boundaries loaded")

    if filter_states:
        return [(a, g) for a, g in _STATE_CACHE if a in filter_states]
    return _STATE_CACHE


def build_index(states):
    geoms = [g for _, g in states]
    abbrs = [a for a, _ in states]
    return STRtree(geoms), geoms, abbrs


def assign_state(centroid, tree, geoms, abbrs):
    for idx in tree.query(centroid):
        if geoms[idx].contains(centroid):
            return abbrs[idx]
    return abbrs[tree.nearest(centroid)]


# ─────────────────────────────────────────────
#  ARCGIS HELPERS  (shared by FEMA + Fault Lines)
# ─────────────────────────────────────────────

def arcgis_post(session, url, params, timeout=180, retries=5):
    for attempt in range(1, retries + 1):
        try:
            r = session.post(url, data=params, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                raise RuntimeError(f"ArcGIS: {data['error']}")
            return data
        except Exception as e:
            if attempt == retries:
                raise
            time.sleep(3 * attempt)


def arcgis_get_count(session, service_url):
    url = f"{service_url}/query"
    # Try statistics first
    try:
        stat = json.dumps([{"statisticType": "count", "onStatisticField": "OBJECTID",
                            "outStatisticFieldName": "TOTAL"}])
        data = arcgis_post(session, url, {"where": "1=1", "outStatistics": stat, "f": "json"})
        feats = data.get("features", [])
        if feats:
            t = feats[0].get("attributes", {}).get("TOTAL")
            if t:
                return int(t)
    except Exception:
        pass
    # Fallback
    try:
        data = arcgis_post(session, url, {"where": "1=1", "returnCountOnly": "true", "f": "json"})
        c = data.get("count")
        if c and int(c) > 50:
            return int(c)
    except Exception:
        pass
    return None


def arcgis_fetch_page(session, service_url, offset, batch, out_fields, geom_type="polygon"):
    url = f"{service_url}/query"
    params = {
        "where":             "1=1",
        "outFields":         ",".join(out_fields) if out_fields else "*",
        "returnGeometry":    "true",
        "outSR":             "4326",
        "resultOffset":      str(offset),
        "resultRecordCount": str(batch),
        "f":                 "json",
    }
    data     = arcgis_post(session, url, params)
    features = data.get("features", [])
    has_more = bool(data.get("exceededTransferLimit")) or len(features) == batch
    return features, has_more


# ─────────────────────────────────────────────
#  GEOMETRY HELPERS
# ─────────────────────────────────────────────

def rings_to_multipolygon(geom_raw, simplify_tol=0):
    if not geom_raw:
        return None
    polygons = []
    for ring in geom_raw.get("rings", []):
        coords = [(p[0], p[1]) for p in ring if len(p) >= 2]
        if len(coords) < 4:
            continue
        if coords[0] != coords[-1]:
            coords.append(coords[0])
        try:
            poly = Polygon(coords)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.geom_type == "MultiPolygon":
                polygons.extend(list(poly.geoms))
            elif not poly.is_empty and poly.geom_type == "Polygon":
                polygons.append(poly)
        except Exception:
            continue
    if not polygons:
        return None
    try:
        mp = MultiPolygon(polygons) if len(polygons) > 1 else MultiPolygon([polygons[0]])
    except Exception:
        from shapely.ops import unary_union
        mp = unary_union(polygons)
        if mp.is_empty:
            return None
        if mp.geom_type == "Polygon":
            mp = MultiPolygon([mp])
    if simplify_tol > 0:
        mp = mp.simplify(simplify_tol, preserve_topology=True)
    return mp if not mp.is_empty else None


def paths_to_multiline(geom_raw, simplify_tol=0):
    if not geom_raw:
        return None
    lines = []
    for path in geom_raw.get("paths", []):
        coords = [(p[0], p[1]) for p in path if len(p) >= 2]
        if len(coords) >= 2:
            lines.append(LineString(coords))
    if not lines:
        return None
    ml = MultiLineString(lines) if len(lines) > 1 else MultiLineString([lines[0]])
    if simplify_tol > 0:
        ml = ml.simplify(simplify_tol, preserve_topology=True)
    return ml if not ml.is_empty else None


# ─────────────────────────────────────────────
#  SHARED: SAVE + MAPBOX UPLOAD
# ─────────────────────────────────────────────

def save_geojson(features_by_state, out_dir, prefix):
    os.makedirs(out_dir, exist_ok=True)
    saved = {}
    for state, features in sorted(features_by_state.items()):
        fname = f"{prefix}_{state.lower()}.geojson"
        fpath = os.path.join(out_dir, fname)
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump({"type": "FeatureCollection", "features": features},
                      f, separators=(",", ":"))
        mb = os.path.getsize(fpath) / 1024 / 1024
        print(f"   ✔ {state:<5} {len(features):>6,} features  {mb:>6.1f} MB → {fname}")
        saved[state] = fpath
    return saved


def mapbox_upload(filepath, tileset_id, tileset_name):
    """Upload a file (GeoJSON or GeoTIFF) to Mapbox via the Uploads API."""
    if not UPLOAD_TO_MAPBOX:
        return
    token    = MAPBOX_ACCESS_TOKEN
    username = MAPBOX_USERNAME
    full_id  = f"{username}.{tileset_id}"

    if not token.startswith("sk."):
        print(f"   ⚠ Skipping Mapbox upload — need sk.ey... token")
        return

    try:
        r = std_requests.post(
            f"https://api.mapbox.com/uploads/v1/{username}/credentials",
            params={"access_token": token}, timeout=30,
        )
        r.raise_for_status()
        creds = r.json()

        try:
            import boto3
            boto3.client(
                "s3",
                aws_access_key_id=creds["accessKeyId"],
                aws_secret_access_key=creds["secretAccessKey"],
                aws_session_token=creds["sessionToken"],
                region_name="us-east-1",
            ).upload_file(filepath, creds["bucket"], creds["key"])
        except ImportError:
            with open(filepath, "rb") as fh:
                std_requests.put(
                    creds["url"], data=fh,
                    headers={"Content-Type": "application/octet-stream"},
                    timeout=600,
                ).raise_for_status()

        r2 = std_requests.post(
            f"https://api.mapbox.com/uploads/v1/{username}",
            params={"access_token": token},
            json={
                "url":     f"https://{creds['bucket']}.s3.amazonaws.com/{creds['key']}",
                "tileset": full_id,
                "name":    tileset_name,
            },
            timeout=30,
        )
        r2.raise_for_status()
        uid = r2.json().get("id", "?")
        print(f"   ✔ Mapbox: {full_id}  (id: {uid})")
    except Exception as e:
        print(f"   ❌ Mapbox upload failed: {e}")


def upload_state_files(saved_files, tileset_prefix, name_template):
    """Iterate state files and upload each to Mapbox."""
    print(f"\n🗺️  Uploading to Mapbox ({MAPBOX_USERNAME})...")
    for state, fpath in sorted(saved_files.items()):
        tileset_id = f"{tileset_prefix}-{state.lower()}"
        name       = name_template.format(state=state.upper())
        mapbox_upload(fpath, tileset_id, name)
    print("   Monitor: https://studio.mapbox.com/tilesets/")


# ══════════════════════════════════════════════════════════════════
#  DATASET 1 — FEMA FLOOD ZONES
# ══════════════════════════════════════════════════════════════════

def run_fema(target_states):
    print("\n" + "═"*60)
    print("  FEMA Flood Zones")
    print("═"*60)

    out_dir = os.path.join(OUTPUT_ROOT, "fema")
    session = make_cffi_session()
    states  = load_state_boundaries(target_states)
    tree, geoms, abbrs = build_index(states)

    print("\n📥 Getting feature count...")
    total = arcgis_get_count(session, FEMA_SERVICE)
    print(f"   ✔ {total:,} features" if total else "   ✔ Count unknown — open-ended pagination")

    state_data = {}
    offset = 0
    downloaded = skipped = discarded = 0

    print(f"\n🔄 Downloading FEMA features...")
    with tqdm(total=total, unit="feat") as pbar:
        while True:
            try:
                raw, has_more = arcgis_fetch_page(session, FEMA_SERVICE, offset, FEMA_BATCH_SIZE, ["FLD_ZONE"])
            except Exception as e:
                print(f"\n   ⚠ Page @{offset} failed: {e}")
                offset += FEMA_BATCH_SIZE
                if downloaded >= 50: break   # remove after testing
                pbar.update(FEMA_BATCH_SIZE)
                continue
            if not raw:
                break
            for feat in raw:
                attrs    = feat.get("attributes", {}) or {}
                fld_zone = attrs.get("FLD_ZONE") or "UNKNOWN"
                geom     = rings_to_multipolygon(feat.get("geometry"), FEMA_SIMPLIFY)
                if not geom:
                    skipped += 1; pbar.update(1); continue
                state = assign_state(geom.centroid, tree, geoms, abbrs)
                if target_states and state not in target_states:
                    discarded += 1; pbar.update(1); continue
                state_data.setdefault(state, []).append({
                    "type": "Feature",
                    "properties": {"FLD_ZONE": fld_zone},
                    "geometry": mapping(geom),
                })
                downloaded += 1; pbar.update(1)
            offset += len(raw)
            if not has_more:
                break

    print(f"\n   ✔ {downloaded:,} features | {discarded} discarded | {skipped} skipped")
    print(f"\n💾 Saving FEMA → {out_dir}")
    saved = save_geojson(state_data, out_dir, "fema_flood")
    upload_state_files(saved, "fema", "FEMA Flood Zones - {state}")


# ══════════════════════════════════════════════════════════════════
#  DATASET 2 — STORM SURGE  (NHC SLOSH GeoTIFF rasters)
# ══════════════════════════════════════════════════════════════════

def run_storm_surge(target_states):
    """
    Storm surge data is a raster (GeoTIFF), not vector.
    Strategy:
      1. Download the NHC zip (contains Cat1-5 TIFs, ~200 MB)
      2. For each category TIF, clip to each target state's bounding box
         and reproject to EPSG:4326 using rasterio
      3. Save clipped GeoTIFF per state per category
      4. Upload each to Mapbox (Mapbox accepts GeoTIFF directly)
    """
    print("\n" + "═"*60)
    print("  Storm Surge (NHC SLOSH MOM)")
    print("═"*60)

    try:
        import rasterio
        from rasterio.mask import mask as rio_mask
        from rasterio.warp import calculate_default_transform, reproject, Resampling
        import numpy as np
    except ImportError:
        print("   ❌ rasterio not installed.  Run:  pip install rasterio numpy")
        return

    out_dir = os.path.join(OUTPUT_ROOT, "storm_surge")
    os.makedirs(out_dir, exist_ok=True)

    # Download zip — resumable with retry so timeouts don't restart from zero
    zip_path = os.path.join(out_dir, "US_SLOSH_MOM_v4.zip")

    def download_with_resume(url, dest, max_attempts=10):
        dl_session = make_std_session()
        for attempt in range(1, max_attempts + 1):
            existing = os.path.getsize(dest) if os.path.exists(dest) else 0
            headers  = {"Range": f"bytes={existing}-"} if existing else {}
            try:
                r = dl_session.get(url, headers=headers, stream=True, timeout=120)
                if r.status_code == 200 and existing:
                    existing = 0          # server ignored Range header; overwrite
                r.raise_for_status()
                total_bytes = int(r.headers.get("content-length", 0)) + existing
                mode = "ab" if existing else "wb"
                with open(dest, mode) as f, tqdm(
                    total=total_bytes, initial=existing,
                    unit="B", unit_scale=True,
                    desc=f"      attempt {attempt}",
                ) as pbar:
                    for chunk in r.iter_content(65536):
                        f.write(chunk)
                        pbar.update(len(chunk))
                # Verify zip integrity before declaring success
                try:
                    with zipfile.ZipFile(dest) as zf:
                        bad = zf.testzip()
                        if bad:
                            raise zipfile.BadZipFile(f"Bad file in zip: {bad}")
                    return   # ✔ success
                except Exception as ze:
                    print(f"   ⚠ Zip integrity check failed ({ze}), retrying...")
                    os.remove(dest)
            except Exception as e:
                wait = 5 * attempt
                print(f"   ⚠ Attempt {attempt} failed ({e}), retrying in {wait}s...")
                time.sleep(wait)
        raise RuntimeError(f"Failed to download {url} after {max_attempts} attempts")

    if os.path.exists(zip_path):
        try:
            with zipfile.ZipFile(zip_path) as zf:
                bad = zf.testzip()
                if bad:
                    raise zipfile.BadZipFile(f"corrupt: {bad}")
            print(f"   ℹ Zip already complete, skipping download")
        except Exception:
            print(f"   ⚠ Existing zip corrupt — re-downloading...")
            os.remove(zip_path)
            print(f"\n📥 Downloading NHC SLOSH zip (~200 MB, resumable)...")
            download_with_resume(SURGE_URL, zip_path)
            print("   ✔ Download complete")
    else:
        print(f"\n📥 Downloading NHC SLOSH zip (~200 MB, resumable)...")
        download_with_resume(SURGE_URL, zip_path)
        print("   ✔ Download complete")

    # Extract TIFs
    extract_dir = os.path.join(out_dir, "extracted")
    os.makedirs(extract_dir, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        # List actual TIF names in zip for user info
        tif_names = [n for n in zf.namelist() if n.lower().endswith(".tif")]
        print(f"   ℹ TIF files in zip: {tif_names}")
        zf.extractall(extract_dir)

    states = load_state_boundaries(target_states)
    saved  = {}

    from shapely.geometry import box as shapely_box
    import json as _json

    for cat_key, tif_name in SURGE_CATEGORIES.items():
        tif_path = None
        # Search for the file (may be in a subdirectory)
        for p in pathlib.Path(extract_dir).rglob("*.tif"):
            if p.name.lower() == tif_name.lower() or tif_name.lower() in p.name.lower():
                tif_path = str(p)
                break

        if not tif_path:
            # Try to find any TIF matching category number
            cat_num = cat_key[-1]
            for p in pathlib.Path(extract_dir).rglob("*.tif"):
                if f"cat{cat_num}" in p.name.lower() or f"_cat{cat_num}" in p.name.lower() or f"_{cat_num}" in p.name.lower():
                    tif_path = str(p)
                    break

        if not tif_path:
            print(f"   ⚠ Could not find TIF for {cat_key} (looked for '{tif_name}')")
            print(f"     Available: {[p.name for p in pathlib.Path(extract_dir).rglob('*.tif')]}")
            continue

        print(f"\n   🌊 Processing {cat_key}  ({os.path.basename(tif_path)})...")

        with rasterio.open(tif_path) as src:
            src_crs = src.crs

            for state_abbr, state_geom in states:
                if target_states and state_abbr not in target_states:
                    continue

                # Convert state geometry to source CRS for clipping
                from rasterio.crs import CRS
                from shapely.ops import transform
                import pyproj

                src_epsg = src_crs.to_epsg() if src_crs else 4326
                if src_epsg and src_epsg != 4326:
                    transformer = pyproj.Transformer.from_crs("EPSG:4326", f"EPSG:{src_epsg}", always_xy=True)
                    state_geom_proj = transform(transformer.transform, state_geom)
                else:
                    state_geom_proj = state_geom

                geom_json = [_json.loads(state_geom_proj.simplify(0.01).__geo_interface__.__repr__()
                                         if hasattr(state_geom_proj.__geo_interface__, '__repr__')
                                         else _json.dumps(state_geom_proj.__geo_interface__))]
                geom_json = [state_geom_proj.__geo_interface__]

                try:
                    clipped, clipped_transform = rio_mask(src, geom_json, crop=True, nodata=0)
                except Exception as e:
                    # State entirely outside raster extent (e.g. WA for surge data)
                    continue

                if clipped.max() == 0:
                    continue  # no surge data in this state

                # Reproject to EPSG:4326 for Mapbox
                dst_crs = "EPSG:4326"
                transform_4326, width, height = calculate_default_transform(
                    src_crs, dst_crs, clipped.shape[2], clipped.shape[1],
                    *rasterio.transform.array_bounds(clipped.shape[1], clipped.shape[2], clipped_transform)
                )

                fname  = f"surge_{cat_key}_{state_abbr.lower()}.tif"
                fpath  = os.path.join(out_dir, fname)

                with rasterio.open(
                    fpath, "w",
                    driver="GTiff",
                    height=height, width=width,
                    count=src.count,
                    dtype=clipped.dtype,
                    crs=dst_crs,
                    transform=transform_4326,
                    compress="deflate",
                    nodata=0,
                ) as dst:
                    reproject(
                        source=clipped,
                        destination=rasterio.band(dst, 1),
                        src_transform=clipped_transform,
                        src_crs=src_crs,
                        dst_transform=transform_4326,
                        dst_crs=dst_crs,
                        resampling=Resampling.nearest,
                    )

                mb = os.path.getsize(fpath) / 1024 / 1024
                print(f"      ✔ {state_abbr}  {mb:.1f} MB → {fname}")
                saved[(state_abbr, cat_key)] = fpath

    # Upload to Mapbox
    if UPLOAD_TO_MAPBOX and saved:
        print(f"\n🗺️  Uploading storm surge rasters to Mapbox...")
        for (state, cat), fpath in sorted(saved.items()):
            tid  = f"surge-{cat}-{state.lower()}"
            name = f"Storm Surge {cat.upper()} - {state}"
            mapbox_upload(fpath, tid, name)
        print("   Monitor: https://studio.mapbox.com/tilesets/")


# ══════════════════════════════════════════════════════════════════
#  DATASET 3 — FAULT LINES  (USGS NSHM ArcGIS)
# ══════════════════════════════════════════════════════════════════

def run_fault_lines(target_states):
    print("\n" + "═"*60)
    print("  Fault Lines (USGS NSHM 2023/2025)")
    print("═"*60)

    out_dir = os.path.join(OUTPUT_ROOT, "fault_lines")
    session = make_std_session()
    states  = load_state_boundaries(target_states)
    tree, geoms, abbrs = build_index(states)

    print("\n📥 Getting fault feature count...")
    total = arcgis_get_count(session, FAULT_SERVICE)
    print(f"   ✔ {total:,} fault sections" if total else "   ✔ Count unknown")

    # Fault layer has a 'State' field — we can use it for faster filtering
    # but also do spatial assignment as fallback
    state_data = {}
    offset = 0
    downloaded = skipped = discarded = 0

    print("\n🔄 Downloading fault lines...")
    with tqdm(total=total, unit="feat") as pbar:
        while True:
            try:
                raw, has_more = arcgis_fetch_page(session, FAULT_SERVICE, offset, FAULT_BATCH, FAULT_FIELDS)
            except Exception as e:
                print(f"\n   ⚠ Page @{offset} failed: {e}")
                offset += FAULT_BATCH; pbar.update(FAULT_BATCH); continue
            if not raw:
                break

            for feat in raw:
                attrs = feat.get("attributes", {}) or {}
                geom  = paths_to_multiline(feat.get("geometry"))
                if not geom:
                    skipped += 1; pbar.update(1); continue

                # Try the State field first (may be comma-separated "CA,NV")
                state_field = str(attrs.get("State") or "").strip().upper()
                state_list  = [s.strip() for s in state_field.split(",") if s.strip()]

                if state_list:
                    # Fault spans multiple states — duplicate into each
                    for st in state_list:
                        if not target_states or st in target_states:
                            props = {k: attrs.get(k) for k in FAULT_FIELDS}
                            state_data.setdefault(st, []).append({
                                "type": "Feature",
                                "properties": props,
                                "geometry": mapping(geom),
                            })
                    downloaded += 1; pbar.update(1)
                else:
                    # Fallback: spatial assignment via centroid
                    state = assign_state(geom.centroid, tree, geoms, abbrs)
                    if target_states and state not in target_states:
                        discarded += 1; pbar.update(1); continue
                    props = {k: attrs.get(k) for k in FAULT_FIELDS}
                    state_data.setdefault(state, []).append({
                        "type": "Feature",
                        "properties": props,
                        "geometry": mapping(geom),
                    })
                    downloaded += 1; pbar.update(1)

            offset += len(raw)
            if not has_more:
                break

    print(f"\n   ✔ {downloaded:,} faults | {discarded} discarded | {skipped} skipped")
    print(f"\n💾 Saving fault lines → {out_dir}")
    saved = save_geojson(state_data, out_dir, "faults")
    upload_state_files(saved, "faults", "Fault Lines - {state}")


# ══════════════════════════════════════════════════════════════════
#  DATASET 4 — FIRE RISK / BURN PROBABILITY  (USFS RDS-2020-0016-2)
# ══════════════════════════════════════════════════════════════════

def run_fire_risk(target_states):
    """
    The USFS dataset is organised as one zip per state.
    Each zip contains BP_<STATE>.tif (Burn Probability raster).
    We download each state zip, extract the BP TIF, and upload to Mapbox.
    """
    print("\n" + "═"*60)
    print("  Fire Risk / Burn Probability (USFS RDS-2020-0016-2)")
    print("═"*60)

    try:
        import rasterio
        from rasterio.warp import calculate_default_transform, reproject, Resampling
    except ImportError:
        print("   ❌ rasterio not installed.  Run:  pip install rasterio")
        return

    out_dir = os.path.join(OUTPUT_ROOT, "fire_risk")
    os.makedirs(out_dir, exist_ok=True)
    session = make_std_session()

    # Scrape the catalog page for per-state zip download links
    print("\n📋 Fetching USFS fire risk catalog...")
    try:
        r = session.get(FIRE_CATALOG_URL, timeout=60)
        r.raise_for_status()
        catalog_html = r.text
    except Exception as e:
        print(f"   ❌ Could not fetch catalog: {e}")
        return

    # Parse download links  — USFS catalog uses hrefs like:
    # https://www.fs.usda.gov/rds/archive/products/RDS-2020-0016-2/<STATE>/Data/<STATE>_BP.zip
    import re
    # Find all unique state abbreviations from the catalog page links
    pattern = r'RDS-2020-0016-2/([A-Z]{2})/Data/'
    states_in_catalog = sorted(set(re.findall(pattern, catalog_html)))

    if not states_in_catalog:
        # Fallback: try known URL pattern with all US states
        print("   ℹ Could not auto-detect states from catalog; using known URL pattern")
        states_in_catalog = [
            "AL","AR","AZ","CA","CO","FL","GA","ID","IL","IN","KS","KY","LA",
            "MI","MN","MO","MS","MT","NC","ND","NE","NM","NV","NY","OH","OK",
            "OR","PA","SC","SD","TN","TX","UT","VA","WA","WI","WV","WY"
        ]

    states_to_process = [s for s in states_in_catalog
                         if not target_states or s in target_states]

    if not states_to_process:
        print(f"   ⚠ No target states found in USFS catalog.  Available: {states_in_catalog}")
        return

    print(f"   ✔ {len(states_to_process)} states to process: {', '.join(states_to_process)}")
    saved = {}

    for state in states_to_process:
        zip_url   = (f"https://www.fs.usda.gov/rds/archive/products/"
                     f"RDS-2020-0016-2/{state}/Data/{state}_BP.zip")
        zip_local = os.path.join(out_dir, f"{state}_BP.zip")
        tif_out   = os.path.join(out_dir, f"fire_bp_{state.lower()}.tif")

        if os.path.exists(tif_out):
            print(f"   ℹ {state}: output already exists, skipping download")
            saved[state] = tif_out
            continue

        # Download zip
        print(f"   📥 {state}: downloading burn probability zip...")
        try:
            with session.get(zip_url, stream=True, timeout=300) as r:
                if r.status_code == 404:
                    print(f"   ⚠ {state}: no data at {zip_url}")
                    continue
                r.raise_for_status()
                total_bytes = int(r.headers.get("content-length", 0))
                with open(zip_local, "wb") as f, tqdm(total=total_bytes, unit="B", unit_scale=True,
                                                       desc=f"      {state}") as pbar:
                    for chunk in r.iter_content(65536):
                        f.write(chunk); pbar.update(len(chunk))
        except Exception as e:
            print(f"   ❌ {state}: download failed: {e}")
            continue

        # Extract BP TIF
        extract_dir = os.path.join(out_dir, f"{state}_extracted")
        os.makedirs(extract_dir, exist_ok=True)
        try:
            with zipfile.ZipFile(zip_local) as zf:
                all_names = zf.namelist()
                bp_files  = [n for n in all_names if "BP" in n.upper() and n.lower().endswith(".tif")]
                if not bp_files:
                    # Some packages use .img or different naming
                    bp_files = [n for n in all_names if n.lower().endswith((".tif", ".img"))]
                if not bp_files:
                    print(f"   ⚠ {state}: no BP TIF found in zip. Contents: {all_names[:10]}")
                    continue
                bp_name = bp_files[0]
                zf.extract(bp_name, extract_dir)
                tif_src = os.path.join(extract_dir, bp_name)
        except Exception as e:
            print(f"   ❌ {state}: zip extraction failed: {e}")
            continue

        # Reproject to EPSG:4326 and compress
        try:
            with rasterio.open(tif_src) as src:
                dst_crs = "EPSG:4326"
                transform_4326, width, height = calculate_default_transform(
                    src.crs, dst_crs, src.width, src.height, *src.bounds
                )
                with rasterio.open(
                    tif_out, "w",
                    driver="GTiff", height=height, width=width,
                    count=src.count, dtype=src.dtypes[0],
                    crs=dst_crs, transform=transform_4326,
                    compress="deflate", nodata=src.nodata,
                ) as dst:
                    reproject(
                        source=rasterio.band(src, 1),
                        destination=rasterio.band(dst, 1),
                        src_transform=src.transform,
                        src_crs=src.crs,
                        dst_transform=transform_4326,
                        dst_crs=dst_crs,
                        resampling=Resampling.bilinear,
                    )
            mb = os.path.getsize(tif_out) / 1024 / 1024
            print(f"   ✔ {state}: {mb:.1f} MB → {os.path.basename(tif_out)}")
            saved[state] = tif_out
        except Exception as e:
            print(f"   ❌ {state}: rasterio processing failed: {e}")
            continue

        # Clean up zip to save space
        os.remove(zip_local)
        shutil.rmtree(extract_dir, ignore_errors=True)

    # Upload to Mapbox
    if UPLOAD_TO_MAPBOX and saved:
        print(f"\n🗺️  Uploading fire risk rasters to Mapbox...")
        for state, fpath in sorted(saved.items()):
            tid  = f"fire-bp-{state.lower()}"
            name = f"Fire Burn Probability - {state}"
            mapbox_upload(fpath, tid, name)
        print("   Monitor: https://studio.mapbox.com/tilesets/")


# ══════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════

def main():
    t0 = time.time()

    target_states = None
    if TARGET_STATES:
        target_states = {s.strip().upper() for s in TARGET_STATES}
        print(f"🎯 Target states: {', '.join(sorted(target_states))}")
    else:
        print("🎯 Target states: ALL")

    print(f"📦 Datasets: {', '.join(DATASETS)}")
    print(f"📁 Output:   {OUTPUT_ROOT}\n")

    if "fema"         in DATASETS: run_fema(target_states)
    if "storm_surge"  in DATASETS: run_storm_surge(target_states)
    if "fault_lines"  in DATASETS: run_fault_lines(target_states)
    if "fire_risk"    in DATASETS: run_fire_risk(target_states)

    print(f"\n🎉 All done in {(time.time()-t0)/60:.1f} minutes")
    print(f"   Files: {OUTPUT_ROOT}")
    if UPLOAD_TO_MAPBOX:
        print("   Tilesets: https://studio.mapbox.com/tilesets/")


if __name__ == "__main__":
    main()
