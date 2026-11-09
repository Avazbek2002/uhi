#!/usr/bin/env python3
"""
LST Ingestion Pipeline — Tashkent Heat Atlas
=============================================
For every clear-sky Landsat 8/9 scene in the configured date windows:

  1. Compute LST_C (°C) from ST_B10
  2. Export a PNG preview (for Mapbox image overlay)  ← existing behaviour
  3. Export a GeoTIFF                                  ← existing behaviour
  4. Compute per-mahalla zonal stats (mean/min/max)   ← using mahalla_id property
  5. Compute per-district zonal stats (mean/min/max)  ← using full_name property
  6. Detect UTFVI hotspots and tag with admin metadata
  7. Upload PNG + GeoTIFF to Cloudflare R2
  8. Write maps_manifest.json with all scene data

Manifest entry shape (consumed by src/App.tsx):
  {
    "date":   "2024-07-12",
    "url":    "https://<r2>/landsat-lst-scenes/tashkent_2024-07-12.png",
    "bounds": [[minLon, maxLat], [maxLon, maxLat], [maxLon, minLat], [minLon, minLat]],
    "mahalla_stats": { "<mahalla_id>": {"mean": 48.3, "min": 38.1, "max": 61.2} },
    "district_stats": { "<full_name>":  {"mean": 46.1, "min": 37.4, "max": 58.9} },
    "hotspots": [ { "id": "HST-01", "mahalla_id": 2611015, ... } ]
  }

Key rules so the frontend key-matching works:
  • mahalla_stats keys  = str(props["mahalla_id"])  e.g. "2611015"
  • district_stats keys = props["full_name"]         e.g. "Toshkent, Bektemir"

Usage
-----
    python ingest_lst.py

Requirements
------------
    pip install earthengine-api boto3 requests
"""

import json
import os
import shutil

import boto3
import ee
import requests
from botocore.config import Config
from botocore.exceptions import ClientError  # noqa: F401

# =========================================================================
# 1. CONFIGURATION & INITIALIZATION
# =========================================================================

GEE_PROJECT = "elliptical-city-454004-m7"
ee.Initialize(project=GEE_PROJECT)

LOCAL_GEOJSON_PATH = "public/data/tashkent_city_renderable.geojson"  # Unified renderable file
LOCAL_TEMP_DIR = "./temp_output_clean"

if os.path.exists(LOCAL_TEMP_DIR):
    shutil.rmtree(LOCAL_TEMP_DIR)
os.makedirs(LOCAL_TEMP_DIR, exist_ok=True)

# Cloudflare R2 Credentials
ACCOUNT_ID        = os.getenv("R2_ACCOUNT_ID",        "52f04a53ad8a93ac402a9fc7e4121987")
ACCESS_KEY_ID     = os.getenv("R2_ACCESS_KEY_ID",     "9fbd83b05f8049acf7f2723e6c21a1f6")
SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "44f3d6d74bc374f1d6666774000e707a67193bb0bc7a3e27ba0626823e271e83")
BUCKET_NAME       = "tashkent-uhi"
PREFIX            = "landsat-lst-scenes"
PUBLIC_BASE_URL   = f"https://pub-ec33c8a1d13f40f4935fa09d39bb00ce.r2.dev/{PREFIX}"

WORK_PROJ  = ee.Projection("EPSG:32642").atScale(30)
LST_PALETTE = ["0000ff", "00ffff", "ffff00", "ff7700", "ff0000"]

R2_ENDPOINT = f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com"
s3_client = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=ACCESS_KEY_ID,
    aws_secret_access_key=SECRET_ACCESS_KEY,
    config=Config(signature_version="s3v4", region_name="auto"),
)

# =========================================================================
# 2. PURGE EXISTING FILES IN R2 BUCKET
# =========================================================================

def purge_r2_folder(bucket_name, prefix):
    print(f"\n{'='*56}")
    print(f" PURGING R2: s3://{bucket_name}/{prefix}/")
    print(f"{'='*56}")
    paginator = s3_client.get_paginator("list_objects_v2")
    deleted_count = 0
    for page in paginator.paginate(Bucket=bucket_name, Prefix=prefix):
        if "Contents" in page:
            keys = [{"Key": obj["Key"]} for obj in page["Contents"]]
            s3_client.delete_objects(Bucket=bucket_name, Delete={"Objects": keys})
            deleted_count += len(keys)
            print(f"  Deleted {len(keys)} objects...")
    print(f"  Total removed: {deleted_count}\n")


purge_r2_folder(BUCKET_NAME, PREFIX)

# =========================================================================
# 3. LOAD LOCAL GEOJSON & BUILD GEE FEATURE COLLECTIONS
# =========================================================================

print(f"Loading boundary from '{LOCAL_GEOJSON_PATH}'...")
with open(LOCAL_GEOJSON_PATH, "r", encoding="utf-8") as f:
    geojson_data = json.load(f)

# ── Split features by type in Python BEFORE sending to GEE ────────────────
# ee.FeatureCollection.filter() on a mixed local file is unreliable for
# reduceRegions — splitting here guarantees correct per-polygon stats.
mahalla_features  = [f for f in geojson_data["features"] if f["properties"].get("feature_type") == "mahalla"]
district_features = [f for f in geojson_data["features"] if f["properties"].get("feature_type") == "district"]

print(f"  {len(mahalla_features)} mahalla features")
print(f"  {len(district_features)} district features")

if not mahalla_features:
    raise ValueError("No mahalla features found — check 'feature_type' property in GeoJSON")
if not district_features:
    raise ValueError("No district features found — check 'feature_type' property in GeoJSON")

# Build GEE FeatureCollections from the pre-split Python lists
mahallas_fc  = ee.FeatureCollection([ee.Feature(f) for f in mahalla_features])
districts_fc = ee.FeatureCollection([ee.Feature(f) for f in district_features])

# City geometry = union of all mahalla geometries (more precise than district union)
city_geom = mahallas_fc.geometry().dissolve(maxError=1)

# WGS84 bounding box for PNG export and Mapbox bounds
bbox_geom   = city_geom.bounds()
bbox_coords = bbox_geom.coordinates().getInfo()[0]
lngs        = [p[0] for p in bbox_coords]
lats        = [p[1] for p in bbox_coords]
min_lng, max_lng = min(lngs), max(lngs)
min_lat, max_lat = min(lats), max(lats)

# Mapbox image source format: [top-left, top-right, bottom-right, bottom-left]
scene_bounds = [
    [round(min_lng, 6), round(max_lat, 6)],
    [round(max_lng, 6), round(max_lat, 6)],
    [round(max_lng, 6), round(min_lat, 6)],
    [round(min_lng, 6), round(min_lat, 6)],
]
print(f"WGS84 overlay bounds: {scene_bounds}")

# =========================================================================
# 4. HELPER FUNCTIONS
# =========================================================================

def mask_landsat_sr(image):
    qa         = image.select("QA_PIXEL")
    cloud_mask = qa.bitwiseAnd(1 << 3).eq(0).And(qa.bitwiseAnd(1 << 4).eq(0))
    return image.updateMask(cloud_mask)


def add_lst_bands(image):
    lst_k = image.select("ST_B10").multiply(0.00341802).add(149.0).rename("LST_K")
    lst_c = lst_k.subtract(273.15).rename("LST_C")
    return image.addBands([lst_k, lst_c])


def upload_to_r2(local_path, r2_key, content_type):
    print(f"  --> Uploading s3://{BUCKET_NAME}/{r2_key}")
    s3_client.upload_file(
        local_path,
        BUCKET_NAME,
        r2_key,
        ExtraArgs={"ContentType": content_type},
    )


def download_stream(url, dest_path):
    resp = requests.get(url, stream=True)
    if resp.status_code == 200:
        with open(dest_path, "wb") as fh:
            for chunk in resp.iter_content(1024 * 1024):
                fh.write(chunk)

# =========================================================================
# 5. QUERY LANDSAT SCENES (SUMMER 2024 – 2026)
# =========================================================================

summer_filters = [
    ee.Filter.date("2024-06-01", "2024-08-31"),
    ee.Filter.date("2025-06-01", "2025-08-31"),
    ee.Filter.date("2026-06-01", "2026-08-31"),
]

l8 = (
    ee.ImageCollection("LANDSAT/LC08/C02/T1_L2")
    .filterBounds(city_geom)
    .filter(ee.Filter.Or(*summer_filters))
    .filter(ee.Filter.lt("CLOUD_COVER", 15))
)
l9 = (
    ee.ImageCollection("LANDSAT/LC09/C02/T1_L2")
    .filterBounds(city_geom)
    .filter(ee.Filter.Or(*summer_filters))
    .filter(ee.Filter.lt("CLOUD_COVER", 15))
)

collection  = l8.merge(l9).map(mask_landsat_sr).map(add_lst_bands).sort("system:time_start", True)
scene_list  = collection.toList(collection.size())
scene_count = scene_list.size().getInfo()
print(f"Found {scene_count} clear-sky summer scenes.")

# =========================================================================
# 6. BATCH PROCESSING & R2 SYNC
# =========================================================================

maps_manifest_entries = []

for i in range(scene_count):
    image    = ee.Image(scene_list.get(i))
    date_str = ee.Date(image.get("system:time_start")).format("YYYY-MM-dd").getInfo()

    file_prefix   = f"tashkent_{date_str}"
    png_filename  = f"{file_prefix}.png"
    tif_filename  = f"{file_prefix}.tif"
    png_r2_key    = f"{PREFIX}/{png_filename}"
    tif_r2_key    = f"{PREFIX}/{tif_filename}"
    local_png     = os.path.join(LOCAL_TEMP_DIR, png_filename)
    local_tif     = os.path.join(LOCAL_TEMP_DIR, tif_filename)
    public_png_url = f"{PUBLIC_BASE_URL}/{png_filename}"

    print(f"\n{'='*56}")
    print(f" Scene [{i+1}/{scene_count}]: {date_str}")
    print(f"{'='*56}")

    lst_c = image.select("LST_C").clip(city_geom).reproject(WORK_PROJ)
    lst_k = image.select("LST_K").clip(city_geom).reproject(WORK_PROJ)

    # ── A. EXPORT RASTER FILES ────────────────────────────────────────────
    print("  * Exporting GeoTIFF...")
    tif_url = lst_c.getDownloadURL({
        "name": file_prefix,
        "scale": 30,
        "crs": "EPSG:32642",
        "region": city_geom,
        "format": "GEO_TIFF",
    })
    download_stream(tif_url, local_tif)

    print("  * Exporting PNG preview (EPSG:4326)...")
    png_url = lst_c.getThumbURL({
        "min": 25.0,
        "max": 55.0,
        "palette": LST_PALETTE,
        "dimensions": 1024,
        "crs": "EPSG:4326",
        "region": bbox_geom,
        "format": "png",
    })
    download_stream(png_url, local_png)

    upload_to_r2(local_tif, tif_r2_key, "image/tiff")
    upload_to_r2(local_png, png_r2_key, "image/png")

    # ── B. MAHALLA ZONAL STATS ────────────────────────────────────────────
    # reduceRegions returns original feature properties + reducer outputs.
    # mahalla_id is a numeric property in the GeoJSON → key as str(int(mahalla_id))
    # to match what the frontend expects: mStats["2611015"]
    print("  * Computing mahalla LST zonal stats...")
    mahalla_reducer = (
        ee.Reducer.mean()
        .combine(ee.Reducer.min(), sharedInputs=True)
        .combine(ee.Reducer.max(), sharedInputs=True)
        .combine(ee.Reducer.count(), sharedInputs=True)
    )
    mahalla_stats_fc = lst_c.reduceRegions(
        collection=mahallas_fc,
        reducer=mahalla_reducer,
        scale=30,
    )
    # Only pull the fields we need — avoids hitting GEE payload limits
    raw_mahalla = mahalla_stats_fc.select(
        ["mahalla_id", "mean", "min", "max", "count"]
    ).getInfo()["features"]

    mahalla_stats_dict: dict = {}
    for feat in raw_mahalla:
        props = feat["properties"]
        m_id  = props.get("mahalla_id")
        if m_id is None or props.get("mean") is None:
            continue
        mahalla_stats_dict[str(int(m_id))] = {
            "mean":  round(float(props["mean"]),  1),
            "min":   round(float(props["min"]),   1),
            "max":   round(float(props["max"]),   1),
            "count": int(props.get("count", 0)),
        }

    print(f"    → {len(mahalla_stats_dict)}/{len(mahalla_features)} mahallas with valid stats")

    # ── C. DISTRICT ZONAL STATS ───────────────────────────────────────────
    # Districts have no numeric ID in the GeoJSON — but every mahalla
    # has a "district_id" property (e.g. 2611) matching its parent.
    # We build a full_name → district_id lookup from the mahalla features,
    # then key district_stats by str(district_id) to match the frontend.
    print("  * Computing district LST zonal stats...")

    # Build full_name → district_id map from mahalla properties
    district_name_to_id: dict = {}
    for feat in mahalla_features:
        p = feat["properties"]
        d_id = p.get("district_id")
        parent = p.get("parent_district", "")  # e.g. "Toshkent, Bektemir"
        if d_id and parent:
            district_name_to_id[parent] = int(d_id)

    district_reducer = (
        ee.Reducer.mean()
        .combine(ee.Reducer.min(), sharedInputs=True)
        .combine(ee.Reducer.max(), sharedInputs=True)
    )
    district_stats_fc = lst_c.reduceRegions(
        collection=districts_fc,
        reducer=district_reducer,
        scale=30,
    )
    raw_district = district_stats_fc.select(
        ["full_name", "mean", "min", "max"]
    ).getInfo()["features"]

    district_stats_dict: dict = {}
    for feat in raw_district:
        props  = feat["properties"]
        d_name = props.get("full_name")
        if not d_name or props.get("mean") is None:
            continue
        # Key by numeric district_id (matching frontend d_id)
        # Fall back to full_name if mapping not found
        d_id = district_name_to_id.get(d_name)
        key = str(d_id) if d_id else d_name
        district_stats_dict[key] = {
            "mean": round(float(props["mean"]), 1),
            "min":  round(float(props["min"]),  1),
            "max":  round(float(props["max"]),  1),
        }

    print(f"    → {len(district_stats_dict)}/{len(district_features)} districts with valid stats")

    # ── D. UTFVI HOTSPOT DETECTION ────────────────────────────────────────
    # IMPORTANT: All geometry operations use .transform('EPSG:4326') so
    # centroid coordinates come out as [lng, lat] in WGS84 degrees, not
    # UTM meters. Without this, Mapbox rejects the coordinates.
    print("  * Computing UTFVI hotspots...")

    # UTFVI = (LST_K - mean_K) / mean_K  — must use Kelvin
    mean_k = ee.Number(
        lst_k.reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=city_geom,
            scale=30,
            maxPixels=1e9,
        ).values().get(0)
    )
    utfvi = lst_k.subtract(mean_k).divide(mean_k).rename("UTFVI")

    # Vectorise in WGS84 so geometries/centroids are in degrees from the start
    wgs84 = ee.Projection("EPSG:4326")
    hotspot_polys = utfvi.gte(0.010).reduceToVectors(
        geometry=city_geom,
        scale=30,
        geometryType="polygon",
        eightConnected=True,
        labelProperty="utfvi_class",
        crs=wgs84,
    )
    filtered_hotspots = (
        hotspot_polys
        .map(lambda feat: feat.set(
            "area_ha", feat.geometry().area(maxError=1).divide(10000)
        ))
        .filter(ee.Filter.gte("area_ha", 0.27))
    )

    # Reduce LST_C values (Celsius) over each hotspot polygon
    hotspot_zonal = lst_c.reduceRegions(
        collection=filtered_hotspots,
        reducer=ee.Reducer.max().combine(ee.Reducer.mean(), sharedInputs=True),
        scale=30,
    )

    def attach_admin_metadata(feat):
        # centroid in WGS84 → coordinates() returns [lng, lat] in degrees
        centroid = feat.geometry().centroid(maxError=1).transform(wgs84, maxError=1)
        parent_m = mahallas_fc.filterBounds(centroid).first()
        m_id     = ee.Algorithms.If(parent_m, parent_m.get("mahalla_id"), 0)
        m_name   = ee.Algorithms.If(parent_m, parent_m.get("name"),       "Unknown")
        d_name   = ee.Algorithms.If(parent_m, parent_m.get("parent_district"), "Tashkent")
        return feat.set({
            "mahalla_id":    m_id,
            "mahalla_name":  m_name,
            "district_name": d_name,
            # coordinates().get(0) = lng, get(1) = lat  (WGS84 degrees)
            "lng": centroid.coordinates().get(0),
            "lat": centroid.coordinates().get(1),
        })

    hotspots_tagged = hotspot_zonal.map(attach_admin_metadata)
    raw_hotspots    = hotspots_tagged.getInfo()["features"]

    hotspots_list = []
    for idx, spot in enumerate(raw_hotspots):
        props = spot["properties"]
        lng   = props.get("lng", 0)
        lat   = props.get("lat", 0)

        # Final guard: skip any hotspot whose coords are still in UTM
        if not (-180 <= lng <= 180 and -90 <= lat <= 90):
            print(f"    WARNING: skipping {props.get('id', idx+1)} — coords [{lng}, {lat}] not WGS84")
            continue

        severity = "Critical" if (props.get("utfvi_class", 0) or 0) >= 3 else "Severe"
        hotspots_list.append({
            "id":            f"HST-{idx+1:02d}",
            "mahalla_id":    props.get("mahalla_id", 0),
            "mahalla_name":  props.get("mahalla_name", "Unknown"),
            "district_name": props.get("district_name", "Tashkent"),
            "coordinates":   [round(float(lng), 5), round(float(lat), 5)],
            "peak_lst":      round(props.get("max",      0), 1),
            "mean_lst":      round(props.get("mean",     0), 1),
            "area_ha":       round(props.get("area_ha",  0), 2),
            "severity":      severity,
        })

    print(f"    → {len(hotspots_list)} hotspot polygons with valid WGS84 coords")

    # ── E. APPEND TO MANIFEST ─────────────────────────────────────────────
    maps_manifest_entries.append({
        "date":           date_str,
        "url":            public_png_url,
        "label":          "Landsat LST",
        "bounds":         scene_bounds,
        "mahalla_stats":  mahalla_stats_dict,
        "district_stats": district_stats_dict,
        "hotspots":       hotspots_list,
    })

# =========================================================================
# 7. WRITE & UPLOAD MANIFEST
# =========================================================================

manifest_payload   = {"maps": maps_manifest_entries}
local_manifest     = os.path.join(LOCAL_TEMP_DIR, "maps_manifest.json")
manifest_r2_key    = f"{PREFIX}/maps_manifest.json"

with open(local_manifest, "w", encoding="utf-8") as fh:
    json.dump(manifest_payload, fh, indent=2, ensure_ascii=False)

print(f"\n{'='*56}")
print(" Uploading maps_manifest.json to R2...")
print(f"{'='*56}")
upload_to_r2(local_manifest, manifest_r2_key, "application/json")

print("\nDone.")
print(f"Public manifest: {PUBLIC_BASE_URL}/maps_manifest.json")
print(f"Scenes processed: {len(maps_manifest_entries)}")

# ── Copy manifest to local public folder so the app works offline ─────────
import shutil
local_public_manifest = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "public", "data", "lst-manifest.json"
)
shutil.copy(local_manifest, local_public_manifest)
print(f"Copied manifest → {local_public_manifest}")
