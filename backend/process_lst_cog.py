#!/usr/bin/env python3
"""
LST COG Processing Pipeline
============================
Downloads a Landsat Land Surface Temperature GeoTIFF from Google Earth Engine,
clips it to the Tashkent city boundary, converts it to a Cloud Optimized GeoTIFF
(COG), and uploads it to Cloudflare R2.

No static PNG rendering. Styling is handled at request-time by the tile server
(TiTiler) via URL query parameters (colormap, rescale).

Usage
-----
    python process_lst_cog.py --date 2024-06-04
    python process_lst_cog.py --start 2024-05-01 --end 2024-09-30

Requirements
------------
    pip install -r requirements.txt

Environment Variables (.env)
----------------------------
    R2_ACCOUNT_ID       Cloudflare account ID
    R2_ACCESS_KEY_ID    R2 access key
    R2_SECRET_ACCESS_KEY R2 secret key
    R2_BUCKET_NAME      Target bucket name  (default: landsat-lst-scenes)
    R2_PUBLIC_DOMAIN    Public R2 domain    (e.g. pub-abc123.r2.dev)
    GEE_PROJECT         Google Earth Engine project ID
    TITILER_HOST        (optional) TiTiler base URL for generating tile URLs
"""

import argparse
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import boto3
import numpy as np
import rasterio
import rasterio.crs
import rasterio.mask
import rasterio.transform
from botocore.client import Config
from dotenv import load_dotenv
from rasterio.enums import Resampling
from rasterio.warp import calculate_default_transform, reproject
from rio_cogeo.cogeo import cog_translate
from rio_cogeo.profiles import cog_profiles
from shapely.geometry import shape

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────
load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env.local")
load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

R2_ACCOUNT_ID        = os.environ["R2_ACCOUNT_ID"]
R2_ACCESS_KEY_ID     = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
R2_BUCKET_NAME       = os.getenv("R2_BUCKET_NAME", "landsat-lst-scenes")
R2_PUBLIC_DOMAIN     = os.environ["R2_PUBLIC_DOMAIN"]   # e.g. pub-abc.r2.dev
GEE_PROJECT          = os.getenv("GEE_PROJECT", "")
TITILER_HOST         = os.getenv("TITILER_HOST", "")

R2_PREFIX            = "lst-cog"   # bucket prefix/folder
MANIFEST_KEY         = f"{R2_PREFIX}/manifest.json"

# Temperature range expected for Tashkent summer LST (°C)
LST_MIN_C = 20.0
LST_MAX_C = 65.0

# GeoJSON boundary (relative to repo root)
BOUNDARY_PATH = Path(__file__).parent.parent / "tashkent_city.geojson"

# ── R2 Client ──────────────────────────────────────────────────────────────

def make_r2_client():
    """Return a boto3 S3 client pointed at Cloudflare R2."""
    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


# ── GEE Download ──────────────────────────────────────────────────────────

def download_lst_from_gee(date_str: str, output_path: Path) -> bool:
    """
    Export a Landsat 8/9 Collection-2 LST GeoTIFF for a single date from
    Google Earth Engine, reprojected to EPSG:4326.

    Returns True on success, False if no valid scene exists for that date.
    """
    try:
        import ee
    except ImportError:
        log.error("earthengine-api not installed. Run: pip install earthengine-api")
        raise

    # Authenticate / initialise
    try:
        ee.Initialize(project=GEE_PROJECT) if GEE_PROJECT else ee.Initialize()
    except Exception:
        log.info("Running ee.Authenticate() — follow the browser prompt once.")
        ee.Authenticate()
        ee.Initialize(project=GEE_PROJECT) if GEE_PROJECT else ee.Initialize()

    # Date window ±1 day to catch WRS-2 path/row timing
    start = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    end   = (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=2)).strftime("%Y-%m-%d")

    # Tashkent bounding box
    with open(BOUNDARY_PATH) as f:
        boundary_geojson = json.load(f)
    city_geom = shape(boundary_geojson["features"][0]["geometry"])
    roi = ee.Geometry.BBox(*city_geom.bounds)   # (minLon, minLat, maxLon, maxLat)

    # Landsat Collection-2 Level-2 ST band (Kelvin → Celsius conversion built-in below)
    collection = (
        ee.ImageCollection("LANDSAT/LC09/C02/T1_L2")
        .merge(ee.ImageCollection("LANDSAT/LC08/C02/T1_L2"))
        .filterDate(start, end)
        .filterBounds(roi)
        .filter(ee.Filter.lt("CLOUD_COVER", 30))
        .sort("CLOUD_COVER")
    )

    count = collection.size().getInfo()
    if count == 0:
        log.warning(f"No Landsat scenes found for {date_str} (±1 day) with <30% cloud cover.")
        return False

    image = collection.first()
    scene_date = ee.Date(image.get("DATE_ACQUIRED")).format("YYYY-MM-dd").getInfo()
    log.info(f"Using scene from {scene_date} (requested {date_str})")

    # ST_B10 is in scaled integer form; apply scale + offset → Kelvin → Celsius
    lst_kelvin = image.select("ST_B10").multiply(0.00341802).add(149.0)
    lst_celsius = lst_kelvin.subtract(273.15).rename("LST_C")

    # Clip to city boundary
    boundary_fc = ee.FeatureCollection(boundary_geojson["features"])
    lst_clipped = lst_celsius.clip(boundary_fc.geometry())

    # Export as GeoTIFF in WGS84 at 30 m
    export_task = ee.batch.Export.image.toDrive(
        image=lst_clipped.toFloat(),
        description=f"tashkent_lst_{date_str}",
        folder="tashkent_lst_exports",
        fileNamePrefix=f"tashkent_lst_{date_str}",
        crs="EPSG:4326",
        scale=30,
        region=roi,
        maxPixels=1e9,
        fileFormat="GeoTIFF",
    )

    # For local download without Drive we use getDownloadURL (small areas OK)
    url = lst_clipped.toFloat().getDownloadURL({
        "name": f"tashkent_lst_{date_str}",
        "crs": "EPSG:4326",
        "scale": 30,
        "region": roi,
        "format": "GEO_TIFF",
        "filePerBand": False,
    })

    import urllib.request
    log.info(f"Downloading LST GeoTIFF from GEE ({url[:80]}…)")
    urllib.request.urlretrieve(url, output_path)
    log.info(f"Saved raw GeoTIFF → {output_path}")
    return True


# ── Clip & Reproject ───────────────────────────────────────────────────────

def clip_to_boundary(src_path: Path, dst_path: Path) -> dict:
    """
    Clip a single-band float32 raster to the Tashkent city boundary,
    reproject to EPSG:4326 if needed, and write a plain (non-COG) intermediate.

    Returns a dict with {min_val, max_val, nodata} for downstream use.
    """
    with open(BOUNDARY_PATH) as f:
        boundary = json.load(f)

    shapes = [feat["geometry"] for feat in boundary["features"]]

    with rasterio.open(src_path) as src:
        log.info(f"Input CRS: {src.crs}  |  shape: {src.width}×{src.height}")

        # Reproject to WGS84 if the source is not already there
        if src.crs and src.crs.to_epsg() != 4326:
            log.info(f"Reprojecting from {src.crs} → EPSG:4326")
            transform, width, height = calculate_default_transform(
                src.crs, rasterio.crs.CRS.from_epsg(4326),
                src.width, src.height, *src.bounds
            )
            wgs_meta = src.meta.copy()
            wgs_meta.update({
                "crs": rasterio.crs.CRS.from_epsg(4326),
                "transform": transform,
                "width": width,
                "height": height,
                "dtype": "float32",
                "nodata": -9999.0,
            })
            with tempfile.NamedTemporaryFile(suffix="_wgs84.tif", delete=False) as tmp:
                tmp_wgs = Path(tmp.name)

            with rasterio.open(tmp_wgs, "w", **wgs_meta) as dst_wgs:
                reproject(
                    source=rasterio.band(src, 1),
                    destination=rasterio.band(dst_wgs, 1),
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=transform,
                    dst_crs=rasterio.crs.CRS.from_epsg(4326),
                    resampling=Resampling.bilinear,
                )

            # re-open as WGS84 for clipping step
            src_for_clip = rasterio.open(tmp_wgs)
            cleanup_tmp = tmp_wgs
        else:
            src_for_clip = src
            cleanup_tmp = None

        out_image, out_transform = rasterio.mask.mask(
            src_for_clip, shapes, crop=True, nodata=-9999.0, filled=True
        )

        out_meta = src_for_clip.meta.copy()
        out_meta.update({
            "driver": "GTiff",
            "dtype": "float32",
            "nodata": -9999.0,
            "height": out_image.shape[1],
            "width": out_image.shape[2],
            "transform": out_transform,
            "crs": rasterio.crs.CRS.from_epsg(4326),
            "count": 1,
        })

        if cleanup_tmp:
            src_for_clip.close()
            cleanup_tmp.unlink(missing_ok=True)

    # Write intermediate (plain GTiff; will be COG-ified next step)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(dst_path, "w", **out_meta) as dst:
        dst.write(out_image[0].astype("float32"), 1)

    # Compute valid-pixel statistics
    valid = out_image[out_image != -9999.0]
    stats = {
        "min_val": float(np.nanmin(valid)) if valid.size else LST_MIN_C,
        "max_val": float(np.nanmax(valid)) if valid.size else LST_MAX_C,
        "nodata": -9999.0,
        "pixel_count": int(valid.size),
    }
    log.info(
        f"Clipped raster: {out_meta['width']}×{out_meta['height']} px  "
        f"LST range: {stats['min_val']:.1f}–{stats['max_val']:.1f} °C"
    )
    return stats


# ── COG Conversion ─────────────────────────────────────────────────────────

def convert_to_cog(src_path: Path, cog_path: Path) -> None:
    """
    Convert an intermediate GeoTIFF to a valid Cloud Optimized GeoTIFF.

    COG requirements satisfied:
      - Internal 256×256 tiles
      - Overview pyramid (2x down to thumbnail level)
      - DEFLATE lossless compression
      - GDAL-standard ghost metadata for COG compliance
      - Nodata preserved for transparency
    """
    log.info("Converting to Cloud Optimized GeoTIFF…")

    # Use DEFLATE for lossless compression of float32 data
    cog_profile = cog_profiles.get("deflate")
    cog_profile.update(
        {
            "blockxsize": 256,
            "blockysize": 256,
            "dtype": "float32",
            "nodata": -9999.0,
        }
    )

    # Overview levels: 2, 4, 8, 16, 32 (covers zoom levels ~8–14)
    overview_levels = [2, 4, 8, 16, 32]

    cog_translate(
        input=str(src_path),
        output=str(cog_path),
        profile=cog_profile,
        overview_level=len(overview_levels),
        overview_resampling="average",
        nodata=-9999.0,
        quiet=False,
        forward_band_tags=True,
        forward_ns_tags=True,
        allow_intermediate_compression=True,
        resampling="nearest",
    )
    log.info(f"COG written → {cog_path}  ({cog_path.stat().st_size / 1e6:.2f} MB)")


def validate_cog(cog_path: Path) -> bool:
    """Run COG validation. Returns True if valid."""
    from rio_cogeo.cogeo import cog_validate
    is_valid, errors, warnings = cog_validate(str(cog_path))
    if is_valid:
        log.info("COG validation: ✓ PASSED")
    else:
        log.error(f"COG validation: ✗ FAILED — {errors}")
        for w in warnings:
            log.warning(f"  Warning: {w}")
    return is_valid


# ── R2 Upload ──────────────────────────────────────────────────────────────

def purge_r2_prefix(client, prefix: str) -> None:
    """Delete all objects under a given R2 prefix before uploading new ones."""
    log.info(f"Purging existing objects under s3://{R2_BUCKET_NAME}/{prefix}/")
    paginator = client.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=R2_BUCKET_NAME, Prefix=prefix + "/")
    keys = [obj["Key"] for page in pages for obj in page.get("Contents", [])]
    if not keys:
        log.info("Nothing to purge.")
        return
    client.delete_objects(
        Bucket=R2_BUCKET_NAME,
        Delete={"Objects": [{"Key": k} for k in keys], "Quiet": True},
    )
    log.info(f"Purged {len(keys)} object(s).")


def upload_cog(client, local_path: Path, date_str: str) -> str:
    """
    Upload a COG file to R2. Uses multipart upload for files > 8 MB.

    R2 requires the Content-Type and Accept-Ranges header to be set correctly
    so clients can issue HTTP Range Requests against the COG.

    Returns the public URL of the uploaded COG.
    """
    key = f"{R2_PREFIX}/tashkent_lst_{date_str}.tif"
    file_size_mb = local_path.stat().st_size / 1e6

    log.info(f"Uploading {local_path.name} ({file_size_mb:.2f} MB) → r2://{R2_BUCKET_NAME}/{key}")

    # Multipart threshold: 8 MB
    from boto3.s3.transfer import TransferConfig
    transfer_cfg = TransferConfig(
        multipart_threshold=8 * 1024 * 1024,
        multipart_chunksize=8 * 1024 * 1024,
        max_concurrency=4,
    )

    client.upload_file(
        Filename=str(local_path),
        Bucket=R2_BUCKET_NAME,
        Key=key,
        ExtraArgs={
            # IMPORTANT: image/tiff lets the browser/TiTiler identify the type.
            # Accept-Ranges is returned by R2 automatically; no need to set it here.
            "ContentType": "image/tiff",
            # CORS-friendly public cache
            "CacheControl": "public, max-age=86400",
        },
        Config=transfer_cfg,
    )

    public_url = f"https://{R2_PUBLIC_DOMAIN}/{key}"
    log.info(f"Uploaded → {public_url}")
    return public_url


# ── Manifest ───────────────────────────────────────────────────────────────

def load_manifest(client) -> list:
    """Fetch existing manifest from R2, or return empty list."""
    try:
        obj = client.get_object(Bucket=R2_BUCKET_NAME, Key=MANIFEST_KEY)
        data = json.loads(obj["Body"].read())
        return data.get("scenes", [])
    except client.exceptions.NoSuchKey:
        return []
    except Exception as e:
        log.warning(f"Could not load existing manifest: {e}")
        return []


def save_manifest(client, scenes: list) -> None:
    """
    Upload an updated manifest JSON to R2.

    Manifest format (COG-based; no PNG URLs):
    {
      "updated": "2024-06-04T12:00:00Z",
      "scenes": [
        {
          "date":    "2024-06-04",
          "cog_url": "https://<domain>/lst-cog/tashkent_lst_2024-06-04.tif",
          "tile_url": "https://<titiler>/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png
                        ?url=<cog_url>&colormap_name=inferno&rescale=20,65",
          "stats": { "min": 24.1, "max": 58.3, "pixel_count": 182400 }
        }
      ]
    }
    """
    # Sort newest-first
    scenes_sorted = sorted(scenes, key=lambda s: s["date"], reverse=True)
    manifest = {
        "updated": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scenes": scenes_sorted,
    }
    body = json.dumps(manifest, indent=2).encode()
    client.put_object(
        Bucket=R2_BUCKET_NAME,
        Key=MANIFEST_KEY,
        Body=body,
        ContentType="application/json",
        CacheControl="public, max-age=60",   # short TTL so new scenes appear quickly
    )
    log.info(f"Manifest saved → r2://{R2_BUCKET_NAME}/{MANIFEST_KEY}")
    log.info(f"Public manifest URL: https://{R2_PUBLIC_DOMAIN}/{MANIFEST_KEY}")


def build_tile_url(cog_url: str, min_val: float, max_val: float) -> str:
    """
    Build a TiTiler tile URL template for this COG.

    The tile server reads float32 pixel values directly from the COG over
    HTTP Range Requests and applies colormap + rescale at request-time.
    No re-processing needed for different colormaps.
    """
    if not TITILER_HOST:
        return ""
    rescale = f"{min_val:.1f},{max_val:.1f}"
    import urllib.parse
    encoded_url = urllib.parse.quote(cog_url, safe="")
    return (
        f"{TITILER_HOST.rstrip('/')}/cog/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}.png"
        f"?url={encoded_url}"
        f"&colormap_name=inferno"
        f"&rescale={rescale}"
        f"&nodata=-9999"
    )


# ── Date Range Helper ──────────────────────────────────────────────────────

def date_range(start: str, end: str) -> list[str]:
    """Return every 16-day Landsat revisit date between start and end."""
    # Landsat has a 16-day repeat cycle. We return candidate dates spaced 16 days
    # apart; GEE will find the nearest valid scene within ±1 day.
    dates = []
    current = datetime.strptime(start, "%Y-%m-%d")
    finish  = datetime.strptime(end, "%Y-%m-%d")
    while current <= finish:
        dates.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=16)
    return dates


# ── Main Pipeline ──────────────────────────────────────────────────────────

def process_date(date_str: str, client, tmpdir: Path, force: bool = False) -> dict | None:
    """
    Full pipeline for a single acquisition date:
      1. Download raw LST GeoTIFF from GEE
      2. Clip to Tashkent boundary + reproject to EPSG:4326
      3. Convert to COG
      4. Validate COG
      5. Upload to R2
      6. Return scene metadata dict

    Returns None if no valid scene found.
    """
    log.info(f"\n{'='*60}\nProcessing date: {date_str}\n{'='*60}")

    raw_path  = tmpdir / f"raw_{date_str}.tif"
    clip_path = tmpdir / f"clipped_{date_str}.tif"
    cog_path  = tmpdir / f"tashkent_lst_{date_str}_cog.tif"

    # Step 1: Download
    success = download_lst_from_gee(date_str, raw_path)
    if not success:
        return None

    # Step 2: Clip + reproject
    stats = clip_to_boundary(raw_path, clip_path)
    if stats["pixel_count"] == 0:
        log.warning(f"No valid pixels after clipping for {date_str}. Skipping.")
        return None

    # Step 3: Convert to COG
    convert_to_cog(clip_path, cog_path)

    # Step 4: Validate
    if not validate_cog(cog_path):
        log.error(f"COG validation failed for {date_str}. Skipping upload.")
        return None

    # Step 5: Upload
    cog_url = upload_cog(client, cog_path, date_str)

    # Step 6: Build scene record
    tile_url = build_tile_url(
        cog_url,
        min_val=max(LST_MIN_C, stats["min_val"] - 2),
        max_val=min(LST_MAX_C, stats["max_val"] + 2),
    )

    scene = {
        "date": date_str,
        "cog_url": cog_url,
        "tile_url": tile_url,
        "stats": {
            "min": round(stats["min_val"], 2),
            "max": round(stats["max_val"], 2),
            "pixel_count": stats["pixel_count"],
        },
    }
    log.info(f"Scene record: {json.dumps(scene, indent=2)}")
    return scene


def main():
    parser = argparse.ArgumentParser(
        description="Process LST scenes to COG and upload to Cloudflare R2."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--date", help="Single acquisition date (YYYY-MM-DD)")
    group.add_argument("--start", help="Start date for range processing (YYYY-MM-DD)")
    parser.add_argument("--end", help="End date for range (default: today)")
    parser.add_argument("--force", action="store_true", help="Re-process even if already in manifest")
    parser.add_argument("--no-purge", action="store_true", help="Skip bucket purge before upload")
    parser.add_argument(
        "--use-local",
        metavar="PATH",
        help="Skip GEE download and use a local .tif instead (for testing)",
    )
    args = parser.parse_args()

    client = make_r2_client()
    existing_scenes = load_manifest(client)
    existing_dates = {s["date"] for s in existing_scenes}

    # Determine which dates to process
    if args.date:
        dates = [args.date]
    else:
        end_date = args.end or datetime.utcnow().strftime("%Y-%m-%d")
        dates = date_range(args.start, end_date)

    log.info(f"Dates to process: {dates}")

    if not args.no_purge and not args.use_local:
        purge_r2_prefix(client, R2_PREFIX)

    new_scenes = []
    with tempfile.TemporaryDirectory(prefix="lst_cog_") as tmpdir:
        tmpdir = Path(tmpdir)

        for date_str in dates:
            if date_str in existing_dates and not args.force:
                log.info(f"Skipping {date_str} (already in manifest, use --force to reprocess)")
                continue

            # Allow local file override (useful for testing without GEE access)
            if args.use_local:
                clip_path = tmpdir / f"clipped_{date_str}.tif"
                cog_path  = tmpdir / f"tashkent_lst_{date_str}_cog.tif"
                log.info(f"Using local file: {args.use_local}")
                stats = clip_to_boundary(Path(args.use_local), clip_path)
                convert_to_cog(clip_path, cog_path)
                if not validate_cog(cog_path):
                    log.error("COG validation failed.")
                    sys.exit(1)
                cog_url = upload_cog(client, cog_path, date_str)
                tile_url = build_tile_url(cog_url, stats["min_val"], stats["max_val"])
                scene = {
                    "date": date_str,
                    "cog_url": cog_url,
                    "tile_url": tile_url,
                    "stats": {
                        "min": round(stats["min_val"], 2),
                        "max": round(stats["max_val"], 2),
                        "pixel_count": stats["pixel_count"],
                    },
                }
                new_scenes.append(scene)
            else:
                scene = process_date(date_str, client, tmpdir, force=args.force)
                if scene:
                    new_scenes.append(scene)

    # Merge new scenes into manifest and save
    merged = {s["date"]: s for s in existing_scenes}
    for s in new_scenes:
        merged[s["date"]] = s
    save_manifest(client, list(merged.values()))

    log.info(f"\n{'='*60}")
    log.info(f"Done. Processed {len(new_scenes)} new scene(s).")
    if new_scenes:
        log.info("\nTile URL examples:")
        for s in new_scenes:
            log.info(f"  {s['date']}: {s.get('tile_url', s['cog_url'])}")
    log.info(f"{'='*60}")


if __name__ == "__main__":
    main()
