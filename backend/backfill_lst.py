#!/usr/bin/env python3
"""Backfill matching Landsat 8/9 LST scenes into Cloudflare R2.

Each scene gets a PNG, GeoTIFF, and metadata JSON named from its unique
LANDSAT_PRODUCT_ID. The configured R2 prefix is purged before the backfill
starts, so this command always produces a complete replacement dataset.
"""

import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*_args, **_kwargs):
        return False

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env.local")
load_dotenv(ROOT / ".env")
PREFIX = os.getenv("R2_LST_PREFIX", "landsat-lst-scenes").strip("/")
PUBLIC_DOMAIN = os.getenv("R2_PUBLIC_DOMAIN", "").rstrip("/")
BUCKET = os.getenv("R2_BUCKET_NAME", "landsat-lst-scenes")
GEE_PROJECT = os.getenv("GEE_PROJECT") or None
BOUNDARY_PATH = ROOT / "tashkent_city.geojson"
RENDERABLE_BOUNDARY_PATH = ROOT / "public/data/tashkent_city_renderable.geojson"
PALETTE = ["0000ff", "00ffff", "ffff00", "ff7700", "ff0000"]


def make_client():
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def download(url, destination):
    request = Request(url, headers={"User-Agent": "uhi-lst-backfill/1.0"})
    with urlopen(request) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)


def upload(client, path, key, content_type):
    client.upload_file(str(path), BUCKET, key, ExtraArgs={"ContentType": content_type})


def object_exists(client, key):
    try:
        client.head_object(Bucket=BUCKET, Key=key)
        return True
    except client.exceptions.ClientError as error:
        if error.response.get("Error", {}).get("Code") in {"404", "NoSuchKey"}:
            return False
        raise


def calculate_legacy_metadata(image, city, mahallas_fc, districts_fc, mahalla_features, bounds):
    """Calculate the same per-scene fields used by the original ingestion."""
    import ee

    lst_k = image.select("ST_B10").multiply(0.00341802).add(149.0).rename("LST_K").clip(city)
    lst_c = lst_k.subtract(273.15).rename("LST_C")
    reducer = (
        ee.Reducer.mean()
        .combine(ee.Reducer.min(), sharedInputs=True)
        .combine(ee.Reducer.max(), sharedInputs=True)
        .combine(ee.Reducer.count(), sharedInputs=True)
    )
    raw_mahallas = lst_c.reduceRegions(collection=mahallas_fc, reducer=reducer, scale=30).select(
        ["mahalla_id", "mean", "min", "max", "count"]
    ).getInfo()["features"]
    mahalla_stats = {}
    for feature in raw_mahallas:
        props = feature["properties"]
        mahalla_id = props.get("mahalla_id")
        if mahalla_id is not None and props.get("mean") is not None:
            mahalla_stats[str(int(mahalla_id))] = {
                "mean": round(float(props["mean"]), 1),
                "min": round(float(props["min"]), 1),
                "max": round(float(props["max"]), 1),
                "count": int(props.get("count", 0)),
            }

    district_name_to_id = {}
    for feature in mahalla_features:
        props = feature["properties"]
        if props.get("district_id") and props.get("parent_district"):
            district_name_to_id[props["parent_district"]] = int(props["district_id"])
    district_reducer = ee.Reducer.mean().combine(ee.Reducer.min(), sharedInputs=True).combine(ee.Reducer.max(), sharedInputs=True)
    raw_districts = lst_c.reduceRegions(collection=districts_fc, reducer=district_reducer, scale=30).select(
        ["full_name", "mean", "min", "max"]
    ).getInfo()["features"]
    district_stats = {}
    for feature in raw_districts:
        props = feature["properties"]
        name = props.get("full_name")
        if name and props.get("mean") is not None:
            key = str(district_name_to_id.get(name, name))
            district_stats[key] = {
                "mean": round(float(props["mean"]), 1),
                "min": round(float(props["min"]), 1),
                "max": round(float(props["max"]), 1),
            }

    mean_k = ee.Number(lst_k.reduceRegion(reducer=ee.Reducer.mean(), geometry=city, scale=30, maxPixels=1e9).values().get(0))
    hotspot_polys = lst_k.subtract(mean_k).divide(mean_k).rename("UTFVI").gte(0.010).reduceToVectors(
        geometry=city, scale=30, geometryType="polygon", eightConnected=True,
        labelProperty="utfvi_class", crs=ee.Projection("EPSG:4326"),
    )
    filtered_hotspots = hotspot_polys.map(
        lambda feature: feature.set("area_ha", feature.geometry().area(maxError=1).divide(10000))
    ).filter(ee.Filter.gte("area_ha", 0.27))
    hotspot_zonal = lst_c.reduceRegions(
        collection=filtered_hotspots,
        reducer=ee.Reducer.max().combine(ee.Reducer.mean(), sharedInputs=True),
        scale=30,
    )
    wgs84 = ee.Projection("EPSG:4326")

    def attach_admin(feature):
        centroid = feature.geometry().centroid(maxError=1).transform(wgs84, maxError=1)
        parent = mahallas_fc.filterBounds(centroid).first()
        return feature.set({
            "mahalla_id": ee.Algorithms.If(parent, parent.get("mahalla_id"), 0),
            "mahalla_name": ee.Algorithms.If(parent, parent.get("name"), "Unknown"),
            "district_name": ee.Algorithms.If(parent, parent.get("parent_district"), "Tashkent"),
            "lng": centroid.coordinates().get(0),
            "lat": centroid.coordinates().get(1),
        })

    hotspots = []
    for index, feature in enumerate(hotspot_zonal.map(attach_admin).getInfo()["features"]):
        props = feature["properties"]
        lng, lat = props.get("lng", 0), props.get("lat", 0)
        if not (-180 <= lng <= 180 and -90 <= lat <= 90):
            continue
        hotspots.append({
            "id": f"HST-{index + 1:02d}",
            "mahalla_id": props.get("mahalla_id", 0),
            "mahalla_name": props.get("mahalla_name", "Unknown"),
            "district_name": props.get("district_name", "Tashkent"),
            "coordinates": [round(float(lng), 5), round(float(lat), 5)],
            "peak_lst": round(float(props.get("max", 0)), 1),
            "mean_lst": round(float(props.get("mean", 0)), 1),
            "area_ha": round(float(props.get("area_ha", 0)), 2),
            "severity": "Critical" if (props.get("utfvi_class", 0) or 0) >= 3 else "Severe",
        })
    return {"bounds": bounds, "mahalla_stats": mahalla_stats, "district_stats": district_stats, "hotspots": hotspots}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-cloud", type=float, default=30.0)
    parser.add_argument("--work-dir", type=Path, default=None)
    args = parser.parse_args()
    required = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_PUBLIC_DOMAIN")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        parser.error(f"missing required environment variable(s): {', '.join(missing)}")

    try:
        import ee
    except ImportError as error:
        parser.error(f"missing backend dependency: {error.name}; install backend/requirements.txt")

    ee.Initialize(project=GEE_PROJECT) if GEE_PROJECT else ee.Initialize()
    client = make_client()
    work_dir = args.work_dir or Path(tempfile.mkdtemp(prefix="lst-backfill-"))
    work_dir.mkdir(parents=True, exist_ok=True)

    with RENDERABLE_BOUNDARY_PATH.open(encoding="utf-8") as boundary_file:
        boundary = json.load(boundary_file)
    mahalla_features = [feature for feature in boundary["features"] if feature["properties"].get("feature_type") == "mahalla"]
    district_features = [feature for feature in boundary["features"] if feature["properties"].get("feature_type") == "district"]
    if not mahalla_features or not district_features:
        raise ValueError("Renderable boundary must contain both mahalla and district features")
    mahallas_fc = ee.FeatureCollection([ee.Feature(feature) for feature in mahalla_features])
    districts_fc = ee.FeatureCollection([ee.Feature(feature) for feature in district_features])
    city = mahallas_fc.geometry().dissolve(maxError=1)
    bbox = city.bounds().coordinates().getInfo()[0]
    min_lng, max_lng = min(point[0] for point in bbox), max(point[0] for point in bbox)
    min_lat, max_lat = min(point[1] for point in bbox), max(point[1] for point in bbox)
    bounds = [[min_lng, max_lat], [max_lng, max_lat], [max_lng, min_lat], [min_lng, min_lat]]

    summer_windows = [ee.Filter.date(f"{year}-06-01", f"{year}-09-01") for year in (2024, 2025, 2026)]
    collections = []
    for collection_id in ("LANDSAT/LC08/C02/T1_L2", "LANDSAT/LC09/C02/T1_L2"):
        collections.append(
            ee.ImageCollection(collection_id)
            .filter(ee.Filter.Or(*summer_windows))
            .filterBounds(city)
            .filter(ee.Filter.lte("CLOUD_COVER", args.max_cloud))
        )
    collection = collections[0].merge(collections[1]).sort("system:time_start")
    images = collection.toList(collection.size())
    count = collection.size().getInfo()
    print(f"Found {count} Landsat scenes for June-August 2024-2026.")

    print(f"Purging all objects under s3://{BUCKET}/{PREFIX}/")
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=f"{PREFIX}/"):
        keys = [{"Key": item["Key"]} for item in page.get("Contents", [])]
        if keys:
            client.delete_objects(Bucket=BUCKET, Delete={"Objects": keys})

    manifest = []
    for index in range(count):
        image = ee.Image(images.get(index))
        identifier = image.get("LANDSAT_PRODUCT_ID").getInfo() or image.get("system:index").getInfo()
        acquired = ee.Date(image.get("system:time_start")).format("YYYY-MM-dd").getInfo()
        prefix = f"{PREFIX}/{identifier}"
        png_key, tif_key, json_key = f"{prefix}.png", f"{prefix}.tif", f"{prefix}.json"
        paths = {suffix: work_dir / f"{identifier}{suffix}" for suffix in (".png", ".tif", ".json")}
        public = {"png": f"{PUBLIC_DOMAIN}/{png_key}", "geotiff": f"{PUBLIC_DOMAIN}/{tif_key}", "metadata": f"{PUBLIC_DOMAIN}/{json_key}"}

        print(f"[{index + 1}/{count}] exporting {identifier} ({acquired})")
        lst_c = image.select("ST_B10").multiply(0.00341802).add(149.0).subtract(273.15).rename("LST_C").clip(city)
        download(lst_c.getDownloadURL({"name": identifier, "scale": 30, "crs": "EPSG:32642", "region": city, "format": "GEO_TIFF"}), paths[".tif"])
        download(lst_c.getThumbURL({"min": 25, "max": 55, "palette": PALETTE, "dimensions": 1024, "crs": "EPSG:4326", "region": city.bounds(), "format": "png"}), paths[".png"])
        scene_metadata = calculate_legacy_metadata(
            image, city, mahallas_fc, districts_fc, mahalla_features, bounds
        )
        properties = image.toDictionary().getInfo()
        metadata = {
            "schema_version": 1,
            "scene_id": identifier,
            "date": acquired,
            "url": public["png"],
            "assets": public,
            "processing": {"lst_band": "ST_B10", "scale": 0.00341802, "offset_kelvin": 149.0, "output_unit": "celsius", "output_crs": "EPSG:32642", "scale_m": 30},
            "earth_engine_properties": properties,
            **scene_metadata,
        }
        paths[".json"].write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
        upload(client, paths[".tif"], tif_key, "image/tiff")
        upload(client, paths[".png"], png_key, "image/png")
        upload(client, paths[".json"], json_key, "application/json")
        manifest.append({"date": acquired, "scene_id": identifier, "url": public["png"], "metadata_url": public["metadata"], **scene_metadata})

    manifest_path = work_dir / "backfill-manifest.json"
    manifest_path.write_text(json.dumps({"maps": manifest}, indent=2), encoding="utf-8")
    upload(client, manifest_path, f"{PREFIX}/backfill-manifest.json", "application/json")
    print(f"Done. Manifest: {PUBLIC_DOMAIN}/{PREFIX}/backfill-manifest.json")


if __name__ == "__main__":
    main()