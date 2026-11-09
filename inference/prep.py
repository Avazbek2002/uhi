import argparse
from datetime import datetime, timedelta, timezone
import io
import json
import os
import urllib.request
import boto3
from botocore.config import Config
import ee
import numpy as np
import rasterio
import requests
import torch
from tqdm import tqdm
from matplotlib.colors import LinearSegmentedColormap
from PIL import Image
from rasterio.features import shapes
from shapely.geometry import shape, Point
from shapely.strtree import STRtree
import rasterio.mask
import rasterio.warp

# ==============================================================================
# 1. CONFIGURATION & CLOUD CREDENTIALS
# ==============================================================================
CLOUD_PROJECT = os.getenv("GEE_PROJECT_ID", "modal-uhi-worker")
CITY_GEOJSON_PATH = "tashkent_city.geojson"
ADMIN_GEOJSON_PATH = "tashkent_city_renderable.geojson"
LOCAL_TEMP_DIR = "./temp_output_clean"
TRACED_MODEL_PATH = "dlstfm_traced_model.pt"

# Cloudflare R2 Credentials
ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID")
ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID")
SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")

BUCKET_NAME = "tashkent-uhi"
PREFIX = "landsat-lst-scenes"
PUBLIC_BASE_URL = f"https://pub-ec33c8a1d13f40f4935fa09d39bb00ce.r2.dev/{PREFIX}"

TARGET_CRS = "EPSG:32642"  # UTM Zone 42N for Tashkent
TARGET_SCALE = 30         # 30-meter pixel resolution
LST_PALETTE = ["0000ff", "00ffff", "ffff00", "ff7700", "ff0000"]

BAND_NAMES = [
    "modis_lst_t0",
    "landsat_lst_t0",
    "modis_lst_tk",
    "ndvi_tk",
    "ndbi_tk",
    "albedo_tk",
    "dem_static",
    "ground_truth_lst_tk",
]

# Initialize S3 Client for Cloudflare R2
s3_client = boto3.client(
    "s3",
    endpoint_url=f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=ACCESS_KEY_ID,
    aws_secret_access_key=SECRET_ACCESS_KEY,
    config=Config(signature_version="s3v4", region_name="auto"),
)

os.makedirs(LOCAL_TEMP_DIR, exist_ok=True)


# ==============================================================================
# 2. HELPER UTILITIES & R2 STORAGE ENGINE
# ==============================================================================
def initialize_earth_engine():
    gee_service_account_json = os.getenv("GEE_SERVICE_ACCOUNT_KEY")
    
    if gee_service_account_json:
        key_dict = json.loads(gee_service_account_json)
        client_email = key_dict.get("client_email")
        
        # Priority: Environment Var -> JSON project_id -> Fallback Default
        project_id = os.getenv("GEE_PROJECT_ID") or key_dict.get("project_id") or CLOUD_PROJECT
        
        print(f"🔑 Initializing Earth Engine with SA: {client_email}")
        print(f"🌐 Target GCP Project ID: {project_id}")
        
        credentials = ee.ServiceAccountCredentials(client_email, key_data=gee_service_account_json)
        ee.Initialize(credentials=credentials, project=project_id)
    else:
        project_id = os.getenv("GEE_PROJECT_ID", CLOUD_PROJECT)
        print(f"🔑 Initializing Earth Engine with User Auth on Project: {project_id}")
        try:
            ee.Initialize(project=project_id)
        except Exception:
            ee.Authenticate()
            ee.Initialize(project=project_id)


def upload_to_r2(local_file_path, r2_key, content_type):
    print(f"  --> Uploading to R2: {r2_key}")
    s3_client.upload_file(local_file_path, BUCKET_NAME, r2_key, ExtraArgs={"ContentType": content_type})


def download_stream(url, dest_path):
    resp = requests.get(url, stream=True)
    if resp.status_code == 200:
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(1024 * 1024):
                f.write(chunk)


def generate_hybrid_weight_matrix(patch_size=256, clip_margin=64):
    W = np.zeros((patch_size, patch_size), dtype=np.float32)
    inner_size = patch_size - 2 * clip_margin
    for i in range(patch_size):
        for j in range(patch_size):
            d_i = min(i, patch_size - 1 - i)
            d_j = min(j, patch_size - 1 - j)
            if d_i >= clip_margin and d_j >= clip_margin:
                inner_d_i = d_i - clip_margin
                inner_d_j = d_j - clip_margin
                ramp = min(inner_d_i, inner_d_j) / (inner_size / 2.0)
                W[i, j] = min(1.0, ramp)
    return W


# ==============================================================================
# 3. GEE DATA STREAMING & MINI-BATCHED GPU INFERENCE
# ==============================================================================
def fetch_raw_satellite_cube(geojson_path, t0_str, tk_str):
    initialize_earth_engine()

    with open(geojson_path, "r") as f:
        data = json.load(f)
    coords = data["features"][0]["geometry"] if data.get("type") == "FeatureCollection" else data.get("geometry", data)
    roi_geometry = ee.Geometry(coords)

    date_t0, date_tk = ee.Date(t0_str), ee.Date(tk_str)

    dem = ee.Image("USGS/SRTMGL1_003").select("elevation").rename("dem_static")

    terra = ee.ImageCollection("MODIS/061/MOD11A1")
    aqua = ee.ImageCollection("MODIS/061/MYD11A1")
    modis_col = terra.merge(aqua).filterBounds(roi_geometry).map(
        lambda img: img.addBands(img.select("LST_Day_1km").multiply(0.02).subtract(273.15).rename("MODIS_LST"), overwrite=True)
    )

    def process_landsat(img):
        qa = img.select("QA_PIXEL")
        mask = qa.bitwiseAnd(1 << 4).eq(0).And(qa.bitwiseAnd(1 << 3).eq(0))
        img_m = img.updateMask(mask)
        optical = img_m.select("SR_B.").multiply(0.0000275).add(-0.2)
        thermal = img_m.select("ST_B10").multiply(0.00341802).add(149.0).subtract(273.15).rename("Landsat_LST")
        ndvi = optical.normalizedDifference(["SR_B5", "SR_B4"]).rename("NDVI")
        ndbi = optical.normalizedDifference(["SR_B6", "SR_B5"]).rename("NDBI")
        albedo = optical.expression(
            "0.356*blue + 0.130*green + 0.373*red + 0.085*nir + 0.072*swir1 - 0.0018",
            {
                "blue": optical.select("SR_B2"),
                "green": optical.select("SR_B3"),
                "red": optical.select("SR_B4"),
                "nir": optical.select("SR_B5"),
                "swir1": optical.select("SR_B6"),
            },
        ).rename("Albedo")
        return img.addBands([thermal, ndvi, ndbi, albedo]).select(["Landsat_LST", "NDVI", "NDBI", "Albedo"])

    landsat_col = ee.ImageCollection("LANDSAT/LC08/C02/T1_L2").merge(ee.ImageCollection("LANDSAT/LC09/C02/T1_L2")) \
        .filterBounds(roi_geometry).filter(ee.Filter.lt("CLOUD_COVER", 15)).map(process_landsat)

    # 1. MODIS Composites: Average over a 5-day window around target dates
    m_t0 = modis_col.filterDate(date_t0.advance(-2, "day"), date_t0.advance(3, "day")).mean()
    m_tk = modis_col.filterDate(date_tk.advance(-2, "day"), date_tk.advance(3, "day")).mean()

    # 2. Landsat Scenes: Search backwards up to 30 days from target date to guarantee a clean overpass
    l_t0 = landsat_col.filterDate(date_t0.advance(-30, "day"), date_t0.advance(1, "day")) \
                    .sort("system:time_start", False) \
                    .first()

    l_tk = landsat_col.filterDate(date_tk.advance(-30, "day"), date_tk.advance(1, "day")) \
                    .sort("system:time_start", False) \
                    .first()

    dlstfm_stack = ee.Image.cat([
        m_t0.select("MODIS_LST").rename("modis_lst_t0"),
        l_t0.select("Landsat_LST").rename("landsat_lst_t0"),
        m_tk.select("MODIS_LST").rename("modis_lst_tk"),
        l_tk.select("NDVI").rename("ndvi_tk"),
        l_tk.select("NDBI").rename("ndbi_tk"),
        l_tk.select("Albedo").rename("albedo_tk"),
        dem,
        l_tk.select("Landsat_LST").rename("ground_truth_lst_tk"),
    ]).clip(roi_geometry).reproject(crs=TARGET_CRS, scale=TARGET_SCALE)

    print("⚡ Streaming preprocessed 8-band raw cube into RAM...")
    download_url = dlstfm_stack.getDownloadURL({
        "name": f"dlstfm_cube_{tk_str}",
        "bands": BAND_NAMES,
        "region": roi_geometry,
        "scale": TARGET_SCALE,
        "crs": TARGET_CRS,
        "format": "GEO_TIFF",
    })

    with urllib.request.urlopen(download_url) as response:
        buffer = io.BytesIO(response.read())

    with rasterio.open(buffer) as src:
        profile = src.profile.copy()
        raw_cube = src.read().astype(np.float32)

    return raw_cube, profile, roi_geometry


def run_gpu_inference(raw_cube, profile, batch_size=16):
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"🚀 Running Mini-Batched GPU Inference on Device: {device} (Batch Size: {batch_size})")

    _, H, W = raw_cube.shape
    invalid_mask = np.isnan(raw_cube[7]) | np.isnan(raw_cube[0])
    raw_cube_clean = np.nan_to_num(raw_cube, nan=0.0)

    temporal_data = torch.from_numpy(raw_cube_clean[0:3]).unsqueeze(0)
    spatial_data = torch.from_numpy(raw_cube_clean[3:7]).unsqueeze(0)

    pad_m = 64
    temporal_pad = torch.nn.functional.pad(temporal_data, (pad_m, pad_m, pad_m, pad_m), mode="reflect")
    spatial_pad = torch.nn.functional.pad(spatial_data, (pad_m, pad_m, pad_m, pad_m), mode="reflect")
    _, _, padded_H, padded_W = temporal_pad.shape

    lst_accumulator = torch.zeros((padded_H, padded_W), dtype=torch.float32, device=device)
    weight_accumulator = torch.zeros((padded_H, padded_W), dtype=torch.float32, device=device)
    w_hybrid = torch.from_numpy(generate_hybrid_weight_matrix(256, 64)).to(device)

    traced_model = torch.jit.load(TRACED_MODEL_PATH, map_location=device)
    traced_model.eval()

    patch_size, stride = 256, 64

    # Pre-calculate all upper-left patch coordinates (y, x)
    patch_coords = [
        (y, x)
        for y in range(0, padded_H - patch_size + 1, stride)
        for x in range(0, padded_W - patch_size + 1, stride)
    ]

    print(f"⚡ Processing {len(patch_coords)} total patches in parallel mini-batches...")

    with torch.no_grad():
        for i in tqdm(range(0, len(patch_coords), batch_size), desc="Inference Progress"):
            batch_coords = patch_coords[i : i + batch_size]

            # 1. Gather patches for current mini-batch
            t_list, s_list = [], []
            for y, x in batch_coords:
                t_list.append(temporal_pad[0, :, y:y+patch_size, x:x+patch_size])
                s_list.append(spatial_pad[0, :, y:y+patch_size, x:x+patch_size])

            # 2. Stack into 4D batch tensors -> [B, 3, 256, 256] and [B, 4, 256, 256]
            t_batch = torch.stack(t_list).to(device)
            s_batch = torch.stack(s_list).to(device)

            # 3. Single parallel forward pass on GPU CUDA cores
            pred_batch = traced_model(t_batch, s_batch)

            # 4. Accumulate mini-batch predictions into canvas
            for idx, (y, x) in enumerate(batch_coords):
                pred_patch = pred_batch[idx].squeeze()  # Ensure 2D [256, 256]
                weighted_pred = pred_patch * w_hybrid

                lst_accumulator[y:y+patch_size, x:x+patch_size] += weighted_pred
                weight_accumulator[y:y+patch_size, x:x+patch_size] += w_hybrid

    final_padded_lst = lst_accumulator / torch.clamp(weight_accumulator, min=1e-5)
    final_lst = final_padded_lst[pad_m:pad_m+H, pad_m:pad_m+W].cpu().numpy()

    NODATA_VAL = -9999.0
    final_lst[invalid_mask] = NODATA_VAL

    # Configure as Tiled Cloud-Optimized GeoTIFF (COG)
    profile.update({
        "count": 1,
        "dtype": "float32",
        "nodata": NODATA_VAL,
        "crs": TARGET_CRS,
        "tiled": True,
        "blockxsize": 256,
        "blockysize": 256,
        "compress": "lzw"
    })

    return final_lst, profile


# ==============================================================================
# 4. GEE SPATIAL ANALYTICS & UTFVI HOTSPOT ENGINE
# ==============================================================================
def run_gee_analytics_and_ingest(date_str, local_tif_path, city_geom=None):
    print(f"\n📊 Running Local Python Zonal Stats & UTFVI Analytics for: {date_str}")

    # --- A. UPLOAD GEOTIFF TO R2 ---
    r2_tif_key = f"{PREFIX}/tashkent_{date_str}.tif"
    upload_to_r2(local_tif_path, r2_tif_key, "image/tiff")

    # --- B. READ GEOTIFF & GENERATE PNG PREVIEW ---
    with rasterio.open(local_tif_path) as src:
        lst_data = src.read(1)
        transform = src.transform
        crs = src.crs
        nodata = src.nodata
        bounds_utm = src.bounds

    # Convert UTM bounds to WGS84 for Mapbox
    from pyproj import Transformer
    transformer = Transformer.from_crs(crs.to_epsg(), 4326, always_xy=True)
    min_lng, min_lat = transformer.transform(bounds_utm.left, bounds_utm.bottom)
    max_lng, max_lat = transformer.transform(bounds_utm.right, bounds_utm.top)

    valid_mask = (lst_data != nodata) & (~np.isnan(lst_data)) & (lst_data > -50.0)

    # Render PNG using the LST palette
    png_path = os.path.join(LOCAL_TEMP_DIR, f"tashkent_{date_str}.png")
    hex_colors = [f"#{c}" for c in LST_PALETTE]
    cmap = LinearSegmentedColormap.from_list("lst_palette", hex_colors)

    norm_lst = np.clip((lst_data - 25.0) / (55.0 - 25.0), 0.0, 1.0)
    rgba = cmap(norm_lst)
    rgba[~valid_mask] = [0, 0, 0, 0]  # Transparent background

    img = Image.fromarray((rgba * 255).astype(np.uint8), mode="RGBA")
    img.save(png_path, format="PNG")
    upload_to_r2(png_path, f"{PREFIX}/tashkent_{date_str}.png", "image/png")

    # WGS84 bounds for Mapbox image overlay — apply 1% inset to match frontend
    lon_range = max_lng - min_lng
    lat_range = max_lat - min_lat
    scene_bounds = [
        [round(min_lng + lon_range * 0.005, 6), round(max_lat - lat_range * 0.005, 6)],  # top-left
        [round(max_lng - lon_range * 0.005, 6), round(max_lat - lat_range * 0.005, 6)],  # top-right
        [round(max_lng - lon_range * 0.005, 6), round(min_lat + lat_range * 0.005, 6)],  # bottom-right
        [round(min_lng + lon_range * 0.005, 6), round(min_lat + lat_range * 0.005, 6)],  # bottom-left
    ]

    # --- C. COMPUTING ZONAL STATS ---
    print("  * Calculating Mahalla & District LST Zonal Stats...")
    admin_path = ADMIN_GEOJSON_PATH if os.path.exists(ADMIN_GEOJSON_PATH) else CITY_GEOJSON_PATH
    with open(admin_path, "r") as f:
        admin_data = json.load(f)

    features = admin_data.get("features", [])
    mahalla_dict, district_dict = {}, {}

    # Pre-build full_name → district_id mapping from mahalla features
    fullname_to_district_id: dict = {}
    for feat in features:
        p = feat.get("properties", {})
        if p.get("feature_type") == "mahalla":
            parent = p.get("parent_district", "")
            d_id = p.get("district_id")
            if parent and d_id and parent not in fullname_to_district_id:
                fullname_to_district_id[parent] = int(d_id)

    # Reproject admin geometries from WGS84 to raster CRS for rasterio.mask
    from pyproj import Transformer as PT
    raster_epsg = crs.to_epsg() or 32642
    wgs_to_utm = PT.from_crs(4326, raster_epsg, always_xy=True)

    def reproject_geojson_geom(geom_dict: dict) -> dict:
        """Reproject a GeoJSON geometry dict from WGS84 → raster CRS."""
        geom = shape(geom_dict)
        # shapely transform using pyproj
        from shapely.ops import transform as shp_transform
        projected = shp_transform(wgs_to_utm.transform, geom)
        return projected.__geo_interface__

    with rasterio.open(local_tif_path) as src:
        for feat in features:
            props = feat.get("properties", {})
            f_type = props.get("feature_type", "mahalla")

            try:
                utm_geom = reproject_geojson_geom(feat["geometry"])
                out_image, _ = rasterio.mask.mask(src, [utm_geom], crop=True, nodata=nodata)
                values = out_image[0]
                feat_valid = (values != nodata) & (~np.isnan(values)) & (values > -50.0)

                if not np.any(feat_valid):
                    continue

                stats_obj = {
                    "mean":  round(float(np.mean(values[feat_valid])),  1),
                    "min":   round(float(np.min(values[feat_valid])),   1),
                    "max":   round(float(np.max(values[feat_valid])),   1),
                    "count": int(np.sum(feat_valid)),
                }

                if f_type == "district":
                    full_name = props.get("full_name", "")
                    d_id = fullname_to_district_id.get(full_name)
                    key = str(d_id) if d_id else full_name
                    district_dict[key] = stats_obj
                else:
                    m_id = props.get("mahalla_id") or props.get("id")
                    if m_id:
                        mahalla_dict[str(int(m_id))] = stats_obj

            except Exception:
                continue

    print(f"    → {len(mahalla_dict)} mahallas, {len(district_dict)} districts with stats")

    # --- D. COMPUTING UTFVI HOTSPOTS ---
    print("  * Extracting UTFVI Urban Heat Island Hotspots...")
    from pyproj import Transformer as ProjTransformer
    raster_epsg = crs.to_epsg() or 32642
    utm_to_wgs84 = ProjTransformer.from_crs(raster_epsg, 4326, always_xy=True)

    lst_k = lst_data + 273.15
    mean_k = np.mean(lst_k[valid_mask])
    utfvi = np.zeros_like(lst_k)
    utfvi[valid_mask] = (lst_k[valid_mask] - mean_k) / mean_k

    hotspot_mask = (utfvi >= 0.020) & valid_mask
    hotspots_list = []

    # Build spatial index over mahalla WGS84 polygons for fast point lookup
    mahalla_features = [f for f in features if f.get("properties", {}).get("feature_type") == "mahalla"]
    mahalla_shapes = []
    for f in mahalla_features:
        try:
            mahalla_shapes.append(shape(f["geometry"]))
        except Exception:
            mahalla_shapes.append(None)
    valid_mahalla_shapes = [s for s in mahalla_shapes if s is not None]
    valid_mahalla_feats  = [f for f, s in zip(mahalla_features, mahalla_shapes) if s is not None]
    spatial_index = STRtree(valid_mahalla_shapes)

    for geom_dict, _ in shapes(hotspot_mask.astype(np.uint8), mask=hotspot_mask, transform=transform):
        poly = shape(geom_dict)
        area_ha = poly.area / 10000.0

        if area_ha < 0.27:
            continue

        centroid = poly.centroid
        lng_wgs, lat_wgs = utm_to_wgs84.transform(centroid.x, centroid.y)

        if not (-180 <= lng_wgs <= 180 and -90 <= lat_wgs <= 90):
            continue

        try:
            with rasterio.open(local_tif_path) as src_h:
                out_img, _ = rasterio.mask.mask(src_h, [geom_dict], crop=True, nodata=nodata)
                h_vals = out_img[0]
                h_valid = (h_vals != nodata) & (~np.isnan(h_vals)) & (h_vals > -50.0)
                peak_temp = float(np.max(h_vals[h_valid])) if np.any(h_valid) else 0.0
                mean_temp = float(np.mean(h_vals[h_valid])) if np.any(h_valid) else 0.0
        except Exception:
            peak_temp, mean_temp = 0.0, 0.0

        # Fast spatial lookup using STRtree
        parent_m_name, parent_d_name, m_id = "Unknown", "Tashkent", 0
        pt = Point(lng_wgs, lat_wgs)
        candidates = spatial_index.query(pt)
        for idx_c in candidates:
            if valid_mahalla_shapes[idx_c].contains(pt):
                p = valid_mahalla_feats[idx_c].get("properties", {})
                parent_m_name = p.get("name", "Unknown")
                parent_d_name = p.get("parent_district", "Tashkent")
                m_id = p.get("mahalla_id", 0)
                break

        severity = "Critical" if peak_temp >= 45.0 else "Severe"
        hotspots_list.append({
            "id":            f"HST-{len(hotspots_list)+1:02d}",
            "mahalla_id":    m_id,
            "mahalla_name":  parent_m_name,
            "district_name": parent_d_name,
            "coordinates":   [round(lng_wgs, 5), round(lat_wgs, 5)],
            "peak_lst":      round(peak_temp, 1),
            "mean_lst":      round(mean_temp, 1),
            "area_ha":       round(area_ha, 2),
            "severity":      severity,
        })

    print(f"    → {len(hotspots_list)} hotspot polygons with valid WGS84 coords")

    # --- E. SYNCHRONIZE MAPS MANIFEST IN R2 ---
    manifest_key = f"{PREFIX}/maps_manifest.json"
    local_manifest_path = os.path.join(LOCAL_TEMP_DIR, "maps_manifest.json")

    manifest_data = {"maps": []}
    try:
        s3_client.download_file(BUCKET_NAME, manifest_key, local_manifest_path)
        with open(local_manifest_path, "r") as f:
            manifest_data = json.load(f)
    except Exception:
        print("  * Creating new manifest...")

    new_entry = {
        "date": date_str,
        "url": f"{PUBLIC_BASE_URL}/tashkent_{date_str}.png",
        "bounds": scene_bounds,
        "mahalla_stats": mahalla_dict,
        "district_stats": district_dict,
        "hotspots": hotspots_list,
    }

    maps_list = [m for m in manifest_data.get("maps", []) if m.get("date") != date_str]
    maps_list.append(new_entry)
    maps_list.sort(key=lambda x: x["date"])
    manifest_data["maps"] = maps_list

    with open(local_manifest_path, "w") as f:
        json.dump(manifest_data, f, indent=2)

    upload_to_r2(local_manifest_path, manifest_key, "application/json")
    print(f"✅ Ingestion Complete! Manifest updated for {date_str}.")

    # Copy updated manifest to local public folder so the dev server serves it
    import shutil
    local_public = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "public", "data", "lst-manifest.json"
    )
    try:
        shutil.copy(local_manifest_path, local_public)
        print(f"   Copied manifest → {local_public}")
    except Exception as e:
        print(f"   Note: could not copy manifest locally: {e}")


# ==============================================================================
# 5. MAIN PIPELINE EXECUTION DRIVER
# ==============================================================================
def run_pipeline(geojson_path, t0_date=None, tk_date=None, batch_size=16):
    # Set target date (tk) to 6 days ago to guarantee GEE satellite availability
    if tk_date is None:
        tk_dt = datetime.now(timezone.utc) - timedelta(days=6)
        tk_date = tk_dt.strftime("%Y-%m-%d")
    else:
        tk_dt = datetime.strptime(tk_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    # Set baseline date (t0) to 16 days prior to target date (1 full Landsat orbit cycle back)
    if t0_date is None:
        t0_date = (tk_dt - timedelta(days=16)).strftime("%Y-%m-%d")

    print(f"🚀 Running Daily LST Pipeline | Baseline (t0): {t0_date} -> Target (tk): {tk_date}")

    # Step 1: Stream raw satellite cube from GEE directly into RAM
    raw_cube, profile, city_geom = fetch_raw_satellite_cube(geojson_path, t0_date, tk_date)

    # Step 2: Mini-Batched GPU PyTorch inference with feathered stitching
    final_lst, profile = run_gpu_inference(raw_cube, profile, batch_size=batch_size)

    # Step 3: Write local temporary COG GeoTIFF
    tif_filename = f"tashkent_{tk_date}.tif"
    local_tif_path = os.path.join(LOCAL_TEMP_DIR, tif_filename)
    with rasterio.open(local_tif_path, "w", **profile) as dst:
        dst.write(final_lst, 1)
        dst.set_band_description(1, f"Predicted LST (°C) for {tk_date}")

    # Step 4: Upload to R2, compute GEE zonal stats & UTFVI hotspots, and update manifest
    run_gee_analytics_and_ingest(
        date_str=tk_date,
        local_tif_path=local_tif_path,
        city_geom=city_geom,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Daily LST ML Pipeline & R2 Analytics Ingestion")
    parser.add_argument("--geojson", type=str, default=CITY_GEOJSON_PATH)
    parser.add_argument("--tk-date", type=str, default=None)
    parser.add_argument("--t0-date", type=str, default=None)
    parser.add_argument("--batch-size", type=int, default=16, help="Mini-batch size for GPU inference")

    args = parser.parse_args()

    if args.tk_date is None:
        tk_dt = datetime.now(timezone.utc)
        TK_DATE = tk_dt.strftime("%Y-%m-%d")
    else:
        TK_DATE = args.tk_date
        tk_dt = datetime.strptime(TK_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    if args.t0_date is None:
        T0_DATE = (tk_dt - timedelta(days=16)).strftime("%Y-%m-%d")
    else:
        T0_DATE = args.t0_date

    print(f"🚀 Starting Daily LST Pipeline | Baseline (t0): {T0_DATE} -> Target (tk): {TK_DATE}")
    run_pipeline(args.geojson, T0_DATE, TK_DATE, batch_size=args.batch_size)