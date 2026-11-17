"""
VERSION 3: physics-aware U-Net for 10 m land surface temperature (LST) of Tashkent. Reuses the 13-band patches downloaded by v2 (folder cache_unet13).
MODELLED, NOT MEASURED. Six switches, each testable on its own (join with +):
  psf     the loss blurs the 10 m output with the Landsat thermal sensor's point spread function before comparing (edge blocks are left out)
  t4      block averages are taken on radiance (temperature to the 4th power), as a thermal sensor does
  derived extra input channels: broadband albedo, vegetation cover, NDVI, moisture (NDMI), water (MNDWI), built-up (NDBI)
  gain    scene weather (ERA5-Land: sun, air temperature, dryness, wind, soil water, skin-air gap) scales how strongly surfaces differ; needs `era5` first
  phys    a simple energy-balance term (more sun, lower albedo = hotter; vegetation and water cool) is added, the network learns what it misses
  b30     (add to any config, e.g. all+b30) the loss is taken on 30 m blocks against Landsat's 30 m product instead of 120 m blocks; needs `download30` first
  ndwi    (add to any config, e.g. all+ndwi) an extra input channel NDWI = (green - NIR) / (green + NIR), built from two native 10 m bands (MNDWI uses the 20 m SWIR band), and the physics term's water cooling uses the stronger of MNDWI and NDWI
  wmask   (add to any config, e.g. all+wmask) an input channel with canals/rivers/ditches from OpenStreetMap (share of each 10 m pixel covered by water), and the physics term's water cooling uses it too; needs `download_water` first
  wphys   (add to any config, e.g. all+wphys) like wmask, but the water map is used only in the physics term (water cools), not given to the network as an input
  sign    penalty if, inside a mixed 120 m block, greener pixels come out warmer or more built-up pixels come out cooler
Seeds: ENS=7,8,9,10 python unet_lst_v3.py evaluate all  (or map all) averages those seeds' saved models; `map` then also saves a seed-disagreement layer, and `view all@ens` shows it.
More cities: put the city polygons in cities.geojson (draw them, or run `fetch_boundaries` to take them from OpenStreetMap; `cities` checks the file). For each city run, with CITY=<name>:
  download_more | era5 | download30 | download_water   (patches go to cache_<name>; only 3.84 km tiles at least half inside the polygon and with some built-up area are kept)
  Train on several cities: CITIES=tashkent,samarkand,... [HOLD_CITY=bukhara] python unet_lst_v3.py train all+wphys  (HOLD_CITY is never trained on and is scored as 'NEW CITY'). MAX_PER_CITY caps patches per extra city (400).
  Map another city: CITY=samarkand python unet_lst_v3.py map all+wphys   (EPOCHS=35 sets the number of epochs without editing the file)
More seasons: `download_more` adds every clear Landsat scene from 1 Apr to 15 Oct (all years) that is not in the cache yet, as new scene numbers after the old ones; then re-run `era5` and `download30`.
  TAG=apr_oct names the saved models/maps with a tag so old models are kept; SUBSET=old|new scores only the original June-August scenes or only the added ones.
Website: TILES=8 python unet_lst_v3.py map all+wphys  (TILES x TILES patches of 3.84 km; 8 covers the whole city; START=2025-04-01 END=2025-04-30 picks the period), then  python unet_lst_v3.py export_site all+wphys@ens
Usage:  python unet_lst_v3.py data_check | fix_means [cities] | fetch_boundaries [names] | cities | rf [base|nb] | canal <config> <config> ... | download_water | probe <lat> <lon> | era5 | download_more | download30 | train <config> | evaluate <config> | map <config> | view <config> <config> | smoke
        <config> is base, all, or switches such as psf+t4. Suggested runs: base, psf+t4, derived, gain, phys, sign, all (compare with the same evaluate numbers).
"""
import os, sys, json, math, time, glob, zlib, concurrent.futures as cf
import numpy as np

# ------------------------------------------------------------------ settings
PROJECT = os.environ.get('GEE_PROJECT_ID') or 'YOUR-GOOGLE-CLOUD-PROJECT'          # <-- Earth Engine project id (Code Editor top right, or console.cloud.google.com)
# Region of interest: the polygon you drew (lon, lat). Only 3.84 km tiles that fall at least half inside it are kept, and the loss ignores everything outside it.
ROI_POLY = [[69.2367130451396, 41.40557080986429], [69.15843545724897, 41.35971819807391], [69.13920938303022, 41.30093841553041], [69.14744912912397, 41.252430986837325],
            [69.1817814045146, 41.20078777025164], [69.24975930978803, 41.16926538087893], [69.25662576486616, 41.20233766019487], [69.37335550119428, 41.23900767442219],
            [69.36168252756147, 41.30248593132906], [69.39944803049117, 41.34013082991107], [69.32391702463178, 41.39166388338999], [69.2367130451396, 41.40557080986429]]
CITIES_FILE = 'cities.geojson'
CITY = os.environ.get('CITY', 'tashkent').strip().lower()          # which city this run downloads / maps; tashkent = the polygon above
CITY_QUERY = {'samarkand': 'Samarkand, Uzbekistan', 'bukhara': 'Bukhara, Uzbekistan', 'fargona': 'Fergana, Uzbekistan', 'kokand': 'Kokand, Uzbekistan', 'bishkek': 'Bishkek, Kyrgyzstan', 'almaty': 'Almaty, Kazakhstan'}
CITYS = '' if CITY == 'tashkent' else f'_{CITY}'                  # added to the names of map files
def ring_area_deg(r): return abs(sum(r[i][0] * r[i + 1][1] - r[i + 1][0] * r[i][1] for i in range(len(r) - 1))) / 2
def read_cities():
    """{name: outer ring [[lon, lat], ...]} from cities.geojson (the biggest polygon of a multipolygon)."""
    if not os.path.exists(CITIES_FILE): return {}
    out = {}
    for ft in json.load(open(CITIES_FILE)).get('features', []):
        g = ft.get('geometry') or {}; nm = str((ft.get('properties') or {}).get('name', '')).strip().lower()
        polys = g.get('coordinates', []) if g.get('type') == 'MultiPolygon' else [g.get('coordinates', [])] if g.get('type') == 'Polygon' else []
        rings = [p[0] for p in polys if p]
        if nm and rings: out[nm] = [[float(a[0]), float(a[1])] for a in max(rings, key=ring_area_deg)]
    return out
if CITY != 'tashkent':
    _c = read_cities()
    if CITY not in _c: raise SystemExit(f'CITY={CITY}: no polygon named "{CITY}" in {CITIES_FILE} (found: {sorted(_c) or "no file"}). Put your polygons there, or run  python unet_lst_v3.py fetch_boundaries {CITY}')
    ROI_POLY = _c[CITY]
def city_centre(): return (69.28, 41.31) if CITY == 'tashkent' else ((min(p[0] for p in ROI_POLY) + max(p[0] for p in ROI_POLY)) / 2, (min(p[1] for p in ROI_POLY) + max(p[1] for p in ROI_POLY)) / 2)
def roi_geom(ee): return ee.Geometry.Polygon([ROI_POLY], None, False)   # planar edges, as drawn
YEARS = (2019, 2025); MONTHS = (6, 8)          # Landsat scenes used: clear June-August days in these years
LANDSAT_CLOUD, S2_CLOUD, WINDOW_DAYS = 25, 30, 3
PATCH_M = 3840; PX = 384; BLOCK = 12           # 3.84 km patches, 10 m pixels, 12 x 12 pixels = 120 m
CACHE = 'cache_unet13' if CITY == 'tashkent' else f'cache_{CITY}'    # downloaded patches are stored here (npz files), re-runs reuse them
BANDS = ['B1', 'B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'B8A', 'B9', 'B10', 'B11', 'B12']   # the 13 bands the pretrained encoder was trained on, in its order
PRETRAINED_URL = 'https://hf.co/torchgeo/resnet18_sentinel2_all_moco/resolve/5b8cddc9a14f3844350b7f40b85bcd32aed75918/resnet18_sentinel2_all_moco-59bfdff9.pth'
TARGET_SCALE = 5.0                             # degrees; target is anomaly / 5
DETAIL_WEIGHT = 0.3                            # map: smooth Landsat + this share of the model's added detail
EPOCHS, BATCH, LR = 60, 8, 2e-3
EPOCHS = int(os.environ.get('EPOCHS', EPOCHS))      # EPOCHS=35 python unet_lst_v3.py train all+wphys
SEED = int(os.environ.get('SEED', 7))               # change with:  SEED=8 python unet_lst_v3.py train base   (another random start, saved under its own name)
TAG = os.environ.get('TAG', ''); TAGS = f'_{TAG}' if TAG else ''     # TAG=apr_oct python unet_lst_v3.py train all   -> unet_lst_v3_<config>_apr_oct.pt (keeps the older models)
SUBSET = os.environ.get('SUBSET', '')                               # evaluate only: 'old' = the first (June-August) scenes, 'new' = the scenes added by download_more
SUF = ('' if SEED == 7 else f'_s{SEED}') + TAGS
ENS = [int(v) for v in os.environ.get('ENS', '').split(',') if v.strip()]   # for example  ENS=7,8,9,10 python unet_lst_v3.py evaluate all   -> averages those seeds' saved models

# ------------------------------------------------------------------ Earth Engine side (download)
def ee_setup():
    import ee
    key = os.environ.get('GEE_SERVICE_ACCOUNT_KEY')      # CI / servers: a service-account json in this variable (same name as the uhi Modal job uses)
    if key:
        kd = json.loads(key); ee.Initialize(credentials=ee.ServiceAccountCredentials(kd['client_email'], key_data=key), project=os.environ.get('GEE_PROJECT_ID') or kd.get('project_id') or PROJECT); return ee
    try: ee.Initialize(project=PROJECT)
    except Exception:
        # 'notebook' mode works on a remote Jupyter / server without gcloud: it prints a link, you sign in there and paste the code back
        ee.Authenticate(auth_mode='notebook'); ee.Initialize(project=PROJECT)
    return ee

def prep_landsat(ee, img):
    qa = img.select('QA_PIXEL')
    clear = qa.bitwiseAnd(2).eq(0).And(qa.bitwiseAnd(4).eq(0)).And(qa.bitwiseAnd(8).eq(0)).And(qa.bitwiseAnd(16).eq(0))
    return img.select('ST_B10').multiply(0.00341802).add(149.0).subtract(273.15).rename('LST_C').updateMask(clear)

def s2_stack(ee, col):
    """Median of the 13 top-of-atmosphere bands (reflectance, DN/10000). `col` is the surface-reflectance collection, used for its cloud/shadow classes (SCL);
    the matching top-of-atmosphere image has the same id and carries all 13 bands incl. B10 (cirrus), which the pretrained encoder expects.
    The two collections are matched with a join on the image id (an id built inside a map is not allowed by Earth Engine)."""
    toa = ee.ImageCollection('COPERNICUS/S2_HARMONIZED').filter(ee.Filter.inList('system:index', col.aggregate_array('system:index')))
    joined = ee.ImageCollection(ee.Join.saveFirst('sr').apply(toa, col, ee.Filter.equals(leftField='system:index', rightField='system:index')))
    def prep(img):
        scl = ee.Image(img.get('sr')).select('SCL'); bad = scl.eq(1).Or(scl.eq(3)).Or(scl.eq(8)).Or(scl.eq(9)).Or(scl.eq(10)).Or(scl.eq(11))
        return img.select(BANDS).resample('bilinear').divide(10000).updateMask(bad.Not())      # 20 m and 60 m bands are smoothly resampled to 10 m
    return joined.map(prep).median()

def pull(ee, image, x0, y0, npx, scale):
    """Fetch npx x npx pixels of `image` on a UTM 42N grid whose upper-left corner is (x0, y0); masked pixels become -999."""
    req = {'expression': image.unmask(-999), 'fileFormat': 'NUMPY_NDARRAY',
           'grid': {'dimensions': {'width': npx, 'height': npx}, 'crsCode': 'EPSG:32642',
                    'affineTransform': {'scaleX': scale, 'shearX': 0, 'translateX': x0, 'shearY': 0, 'scaleY': -scale, 'translateY': y0}}}
    for attempt in range(4):
        try:
            arr = ee.data.computePixels(req)
            return np.stack([arr[n] for n in arr.dtype.names]).astype(np.float32)
        except Exception as e:
            time.sleep(2 + 3 * attempt); err = e
    raise err

def ll_to_utm42(lon, lat):
    """Longitude/latitude (degrees) -> UTM zone 42N (EPSG:32642) metres. Plain formula, no extra libraries; accurate to centimetres."""
    a, f = 6378137.0, 1 / 298.257223563; k0 = 0.9996; e2 = f * (2 - f); ep2 = e2 / (1 - e2)
    phi, lam = math.radians(lat), math.radians(lon - 69.0)
    N = a / math.sqrt(1 - e2 * math.sin(phi) ** 2); T = math.tan(phi) ** 2; C = ep2 * math.cos(phi) ** 2; A = math.cos(phi) * lam
    M = a * ((1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256) * phi - (3 * e2 / 8 + 3 * e2 ** 2 / 32 + 45 * e2 ** 3 / 1024) * math.sin(2 * phi)
             + (15 * e2 ** 2 / 256 + 45 * e2 ** 3 / 1024) * math.sin(4 * phi) - (35 * e2 ** 3 / 3072) * math.sin(6 * phi))
    x = k0 * N * (A + (1 - T + C) * A ** 3 / 6 + (5 - 18 * T + T ** 2 + 72 * C - 58 * ep2) * A ** 5 / 120) + 500000.0
    y = k0 * (M + N * math.tan(phi) * (A ** 2 / 2 + (5 - T + 9 * C + 4 * C ** 2) * A ** 4 / 24 + (61 - 58 * T + T ** 2 + 600 * C - 330 * ep2) * A ** 6 / 720))
    return x, y

def utm_box(ee):
    pts = [ll_to_utm42(lo, la) for lo, la in ROI_POLY]
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    return math.ceil(min(xs) / PATCH_M) * PATCH_M, math.floor(max(xs) / PATCH_M) * PATCH_M, math.ceil(min(ys) / PATCH_M) * PATCH_M, math.floor(max(ys) / PATCH_M) * PATCH_M

WATER_FILE = 'osm_water.json' if CITY == 'tashkent' else f'osm_water_{CITY}.json'          # canals, rivers, ditches from OpenStreetMap (written by download_water)
DEBUG = [0]
BUILT_MIN, MAX_TILES = float(os.environ.get('BUILT_MIN', 0.15)), int(os.environ.get('MAX_TILES', 24))      # other cities: keep tiles with at least this built-up share (Dynamic World), at most MAX_TILES of them

def city_tiles(ee, roi, xmin, xmax, ymin, ymax):
    """Tiles of a new city worth downloading: at least half inside the polygon, and built-up (a city's administrative area also holds fields and hills). Cached in tiles.json."""
    tj = f'{CACHE}/tiles.json'
    if os.path.exists(tj): t = json.load(open(tj)); print('tiles from', tj, ':', len(t)); return {(int(r[0]), int(r[1])) for r in t}      # each entry is [x0, y1, inside share, built-up share]
    utm = ee.Projection('EPSG:32642'); inside = ee.Image.constant(1).clip(roi)
    built = ee.ImageCollection('GOOGLE/DYNAMICWORLD/V1').filterBounds(roi).filterDate('2023-04-01', '2025-10-15').select('built').mean().reproject(utm.atScale(30))
    grid = [(x0, y1) for x0 in range(int(xmin) - PATCH_M, int(xmax) + PATCH_M, PATCH_M) for y1 in range(int(ymax) + PATCH_M, int(ymin) - PATCH_M, -PATCH_M)]
    def one(g):
        x0, y1 = g; a = pull(ee, inside, x0, y1, PATCH_M // 30, 30)[0]; ins = float((a > -900).mean())
        if ins < 0.5: return None
        b = pull(ee, built.updateMask(inside), x0, y1, PATCH_M // 30, 30)[0]; ok = b > -900
        return (x0, y1, ins, float(b[ok].mean()) if ok.any() else 0.0)
    with cf.ThreadPoolExecutor(8) as ex: res = [r for r in ex.map(one, grid) if r]
    good = [r for r in res if r[3] >= BUILT_MIN]; print(f'{CITY}: {len(res)} tiles at least half inside the polygon, {len(good)} with built-up share >= {BUILT_MIN}')
    if len(good) > MAX_TILES: good = [good[i] for i in sorted(np.random.default_rng(0).choice(len(good), MAX_TILES, replace=False))]; print('keeping', MAX_TILES, 'of them (MAX_TILES)')
    json.dump([[r[0], r[1], r[2], r[3]] for r in good], open(tj, 'w')); return {(r[0], r[1]) for r in good}

def download(more=False):
    import datetime
    ee = ee_setup(); os.makedirs(CACHE, exist_ok=True)
    roi = roi_geom(ee); utm = ee.Projection('EPSG:32642'); inside = ee.Image.constant(1).clip(roi)
    L = (ee.ImageCollection('LANDSAT/LC08/C02/T1_L2').merge(ee.ImageCollection('LANDSAT/LC09/C02/T1_L2')).filterBounds(roi)
         .filterDate(f'{YEARS[0]}-01-01', f'{YEARS[1] + 1}-01-01').filter(ee.Filter.calendarRange(*((4, 10) if more else MONTHS), 'month'))
         .filter(ee.Filter.lt('CLOUD_COVER', LANDSAT_CLOUD)).sort('system:time_start'))
    ids = L.aggregate_array('system:index').getInfo(); times = L.aggregate_array('system:time_start').getInfo()
    print(len(ids), 'Landsat scenes' + (' (April-October, before removing the cached ones and those after 15 Oct)' if more else '')); xmin, xmax, ymin, ymax = utm_box(ee); print('UTM box (x min, x max, y min, y max):', xmin, xmax, ymin, ymax)
    S2 = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED').filterBounds(roi).filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', S2_CLOUD))
    tiles = city_tiles(ee, roi, xmin, xmax, ymin, ymax) if CITY != 'tashkent' else None
    sidx = 0; have = set()
    if more:      # keep the old scenes and their numbers; add only scenes that are not cached yet, numbered after the last old one
        for f in glob.glob(f'{CACHE}/*.npz'): d = np.load(f); have.add(float(d['t'])); sidx = max(sidx, int(d['scene']) + 1)
        sj = f'{CACHE}/season.json'
        if not os.path.exists(sj): json.dump({'first_new_scene': sidx}, open(sj, 'w'))
        print('already cached:', len(have), 'scene times; new scenes are numbered from', sidx)
    Llist = L.toList(len(ids)); failed = []
    for k, (sid, t) in enumerate(zip(ids, times)):
        if more:
            dt = datetime.datetime.utcfromtimestamp(t / 1000.0)
            if float(t) in have or (dt.month == 10 and dt.day > 15): continue
        t0 = ee.Date(t); s2c = S2.filterDate(t0.advance(-WINDOW_DAYS, 'day'), t0.advance(WINDOW_DAYS + 1, 'day'))
        if s2c.size().getInfo() == 0: print(sid, 'skipped: no Sentinel-2 within the window', flush=True); continue
        img = ee.Image(Llist.get(k))   # taken straight from the filtered list (the ids of a merged collection carry a '1_' prefix, so they cannot be looked up by name)
        lst = prep_landsat(ee, img); lst120 = lst.reduceResolution(ee.Reducer.mean(), maxPixels=256).reproject(utm.atScale(120)).rename('LST_C').updateMask(inside)   # outside the polygon = no target
        mean = lst120.reduceRegion(ee.Reducer.mean(), roi, 120, maxPixels=1e9, tileScale=4).get('LST_C').getInfo()
        if mean is None: print(sid, 'skipped: no valid Landsat pixels inside the polygon (cloud or edge)', flush=True); continue
        tag = None
        try:       # one bad scene must not stop the whole city: it is reported and skipped
            s2 = s2_stack(ee, s2c)
            if s2.bandNames().size().getInfo() == 0: print(sid, 'skipped: the Sentinel-2 surface-reflectance images have no matching top-of-atmosphere images', flush=True); continue
            s2 = s2.reproject(utm.atScale(10)); tag = f'{sidx:03d}_{sid}'; jobs = []
            for x0 in range(int(xmin), int(xmax), PATCH_M):
                for y1 in range(int(ymax), int(ymin), -PATCH_M):
                    f = f'{CACHE}/{tag}_{x0}_{y1}.npz'
                    if tiles is not None and (x0, y1) not in tiles: continue
                    if not os.path.exists(f): jobs.append((f, x0, y1))
            def one(job):
                f, x0, y1 = job
                y_ = pull(ee, lst120, x0, y1, PATCH_M // 120, 120)[0]; fy = float((y_ > -900).mean())
                if fy < 0.5:
                    if DEBUG[0] < 8: DEBUG[0] += 1; print('  debug: tile', x0, y1, 'Landsat valid share', round(fy, 2), 'min/max', float(y_.min()), float(y_.max()), flush=True)
                    return 0
                x_ = pull(ee, s2, x0, y1, PX, 10); fx = float((x_ > -900).all(axis=0).mean())
                if fx < 0.9:
                    if DEBUG[0] < 8: DEBUG[0] += 1; print('  debug: tile', x0, y1, 'Sentinel-2 valid share', round(fx, 2), flush=True)
                    return 0
                np.savez_compressed(f, x=x_.astype(np.float16), y=y_.astype(np.float32), scene=sidx, mean=mean, x0=x0, y1=y1, t=t); return 1
            with cf.ThreadPoolExecutor(8) as ex: n = sum(ex.map(one, jobs))
            print(tag, 'tiles tried:', len(jobs), 'patches kept:', n, flush=True); sidx += 1
        except Exception as e:
            failed.append(sid); print(sid, 'FAILED and skipped:', str(e)[:200], flush=True)
            for f_ in (glob.glob(f'{CACHE}/{tag}_*.npz') if tag else []): os.remove(f_)      # no half-finished scene is kept (its scene number is reused by the next one)
    if failed:
        print(len(failed), 'scenes failed and were skipped:', failed[:10], '...' if len(failed) > 10 else '')
        if len(failed) > 0.3 * len(ids): raise SystemExit('too many scenes failed: check the messages above')

# ------------------------------------------------------------------ data
ERA_NAMES = ['sun (ssrd W/m2 / 800)', 't2m / 40', 'dewpoint deficit / 20', 'wind / 5', 'soil water / 0.4', '(skin - air temp) / 10']
NSC = len(ERA_NAMES)
ERA_FILE = os.path.join(CACHE, 'era5_scene.json')

def era_vec(v):
    """ERA5-Land values at the overpass hour -> the 6 scene numbers the model is given (all roughly 0-1)."""
    wind = math.hypot(v['u_component_of_wind_10m'], v['v_component_of_wind_10m']); t2 = v['temperature_2m'] - 273.15
    return [v['surface_solar_radiation_downwards_hourly'] / 3600.0 / 800.0, t2 / 40.0, (v['temperature_2m'] - v['dewpoint_temperature_2m']) / 20.0, wind / 5.0,
            v['volumetric_soil_water_layer_1'] / 0.4, (v['skin_temperature'] - 273.15 - t2) / 10.0]
ERA_BANDS = ['surface_solar_radiation_downwards_hourly', 'temperature_2m', 'dewpoint_temperature_2m', 'u_component_of_wind_10m', 'v_component_of_wind_10m', 'volumetric_soil_water_layer_1', 'skin_temperature']

def era5():
    """Scene-level weather at each Landsat overpass (ERA5-Land, hourly, at the city centre). Needs the patches already downloaded; writes one small json."""
    ee = ee_setup(); sc = {}
    for f in sorted(glob.glob(f'{CACHE}/*.npz')):
        d = np.load(f); sc[int(d['scene'])] = float(d['t'])
    pt = ee.Geometry.Point(list(city_centre())); ERA = ee.ImageCollection('ECMWF/ERA5_LAND/HOURLY').select(ERA_BANDS); out = {}
    for k, t in sorted(sc.items()):
        h = ee.Date(round(t / 3600000.0) * 3600000)                                           # the hour the satellite passed (about 06:00 UTC)
        v = ERA.filterDate(h, h.advance(1, 'hour')).first().reduceRegion(ee.Reducer.first(), pt, 11132).getInfo()
        out[str(k)] = era_vec(v); print('scene', k, [round(a, 2) for a in out[str(k)]], flush=True)
    json.dump(out, open(ERA_FILE, 'w')); print('saved', ERA_FILE)

def download30():
    """Adds Landsat at its 30 m grid (128 x 128 values per patch) next to every downloaded patch. Small and quick: no Sentinel-2 is fetched again."""
    ee = ee_setup(); roi = roi_geom(ee); inside = ee.Image.constant(1).clip(roi); by_scene = {}
    for f in sorted(glob.glob(f'{CACHE}/*.npz')):
        d = np.load(f); by_scene.setdefault(float(d['t']), []).append((f, int(d['x0']), int(d['y1'])))
    L = ee.ImageCollection('LANDSAT/LC08/C02/T1_L2').merge(ee.ImageCollection('LANDSAT/LC09/C02/T1_L2')).filterBounds(roi)
    for k, (t, items) in enumerate(sorted(by_scene.items())):
        img = ee.Image(L.filter(ee.Filter.eq('system:time_start', int(t))).first()); lst = prep_landsat(ee, img).resample('bilinear').updateMask(inside)
        def one(it):
            f, x0, y1 = it; o = f.replace('.npz', '_l30.npy')
            if not os.path.exists(o): np.save(o, pull(ee, lst, x0, y1, PATCH_M // 30, 30)[0].astype(np.float32))
            return 1
        with cf.ThreadPoolExecutor(8) as ex: n = sum(ex.map(one, items))
        print('scene', k, 'patches done:', n, flush=True)
    print('done. Train with a config that includes b30, for example: python unet_lst_v3.py train all+b30')


# ------------------------------------------------------------------ thin water (canals) from OpenStreetMap
WATER_WIDTH = {'river': 25.0, 'canal': 12.0, 'stream': 6.0, 'drain': 4.0, 'ditch': 3.0}      # metres, used when OSM has no width tag

def fetch_osm_water():
    """Downloads waterway lines in the polygon's bounding box from OpenStreetMap (Overpass) and stores them in UTM 42N with a width in metres."""
    import urllib.request, urllib.parse
    lons = [p[0] for p in ROI_POLY]; lats = [p[1] for p in ROI_POLY]; bb = f'{min(lats) - 0.01},{min(lons) - 0.01},{max(lats) + 0.01},{max(lons) + 0.01}'
    q = f'[out:json][timeout:180];way["waterway"~"^(river|canal|stream|drain|ditch)$"]({bb});out geom;'; last = None
    data = None      # the public Overpass servers are often overloaded (502/504): try each mirror a few times with a pause
    for attempt in range(4):
        for url in ('https://overpass-api.de/api/interpreter', 'https://overpass.kumi.systems/api/interpreter', 'https://overpass.private.coffee/api/interpreter'):
            try:
                data = json.loads(urllib.request.urlopen(urllib.request.Request(url, data=urllib.parse.urlencode({'data': q}).encode(), headers={'User-Agent': 'tashkent-lst'}), timeout=240).read()); break
            except Exception as e: last = e; print('Overpass', url, 'failed:', e, flush=True)
        if data is not None: break
        print('waiting 30 s before the next round...', flush=True); time.sleep(30)
    if data is None: raise SystemExit(f'could not reach OpenStreetMap ({last}). Run this on a machine with internet access, or download the waterways yourself and save them as {WATER_FILE}')
    lines = []
    for w in data.get('elements', []):
        if 'geometry' not in w or len(w['geometry']) < 2: continue
        tags = w.get('tags', {}); wd = None
        try: wd = float(str(tags.get('width', '')).split()[0].replace(',', '.'))
        except Exception: pass
        lines.append({'pts': [list(ll_to_utm42(p['lon'], p['lat'])) for p in w['geometry']], 'w': wd or WATER_WIDTH.get(tags.get('waterway'), 4.0), 'type': tags.get('waterway')})
    json.dump(lines, open(WATER_FILE, 'w')); n = {}
    for l in lines: n[l['type']] = n.get(l['type'], 0) + 1
    print('saved', WATER_FILE, '| waterway lines by type:', n); return lines

def water_raster(lines, x0, y1, npx, scale=10, ss=4):
    """Share (0-1) of each pixel covered by water lines drawn with their width, on the grid used for the patches (upper-left corner x0, y1)."""
    from PIL import Image, ImageDraw
    S = npx * ss; img = Image.new('L', (S, S), 0); dr = ImageDraw.Draw(img); xe, ys = x0 + npx * scale, y1 - npx * scale
    for l in lines:
        pts = l['pts']; xs = [p[0] for p in pts]; yy = [p[1] for p in pts]
        if max(xs) < x0 - 50 or min(xs) > xe + 50 or max(yy) < ys - 50 or min(yy) > y1 + 50: continue
        dr.line([((x - x0) / scale * ss, (y1 - y) / scale * ss) for x, y in pts], fill=255, width=max(1, int(round(l['w'] / scale * ss))), joint='curve')
    return (np.asarray(img, np.float32) / 255.0).reshape(npx, ss, npx, ss).mean((1, 3))

def load_water():
    if not os.path.exists(WATER_FILE): raise SystemExit(f'no {WATER_FILE}: run  python unet_lst_v3.py download_water  first')
    return json.load(open(WATER_FILE))

def make_water_files(lines):
    places = set()
    for f in glob.glob(f'{CACHE}/*.npz'): d = np.load(f); places.add((int(d['x0']), int(d['y1'])))
    for (x0, y1) in sorted(places):
        o = f'{CACHE}/wtr_{x0}_{y1}.npy'
        if not os.path.exists(o): np.save(o, (water_raster(lines, x0, y1, PX) * 255).astype(np.uint8))
    print('water masks written for', len(places), 'places')

def download_water():
    make_water_files(fetch_osm_water() if not os.path.exists(WATER_FILE) or '--refresh' in sys.argv else load_water())
    print('done. Train with a config that includes wmask, for example: python unet_lst_v3.py train all+wmask')


CITIES_ENV = [c.strip().lower() for c in os.environ.get('CITIES', '').split(',') if c.strip()]       # CITIES=tashkent,samarkand: train on the patches of several cities
HOLD_CITY = os.environ.get('HOLD_CITY', '').strip().lower()                                           # this city is never trained on; it is scored as 'NEW CITY'
MAX_PER_CITY = int(os.environ.get('MAX_PER_CITY', 400))                                              # at most this many patches per city other than tashkent (memory: about 3.8 MB each)
CITY_NAMES, CITY_IDX = [], np.zeros(0, int)
def vok(v):
    """valid LST pixel: not the -999 no-data value, and not exactly 0.0 (a no-data fill that some cached patches hold; real LST is never exactly 0.000)."""
    return (v > -900) & (np.abs(v) > 1e-6)
def scene_means(d):
    """{scene: temperature (C) the anomalies are measured from}. Tashkent keeps the polygon-wide scene mean stored in the patches; other cities use scene_means.json written by fix_means."""
    p = os.path.join(d, 'scene_means.json'); return {int(k): float(v) for k, v in json.load(open(p)).items()} if os.path.exists(p) else {}
def cache_dir(c): return CACHE if c == CITY else ('cache_unet13' if c == 'tashkent' else f'cache_{c}')

def load_patches(wm=False):
    global CITY_NAMES, CITY_IDX
    X, Y, M, S, P, MEAN, Y30, M30, SL, CI = None, [], [], [], [], [], [], [], [], []; has30 = True
    cities = CITIES_ENV or [CITY]; files = []
    for ci, c in enumerate(cities):
        fs = sorted(glob.glob(f'{cache_dir(c)}/*.npz'))
        if c != 'tashkent' and len(fs) > MAX_PER_CITY: fs = sorted(str(a) for a in np.random.default_rng(zlib.crc32(c.encode())).choice(fs, MAX_PER_CITY, replace=False))
        if not fs: print('warning: no patches for', c, 'in', cache_dir(c))
        files += [(ci, f) for f in fs]
    eras = [json.load(open(os.path.join(cache_dir(c), 'era5_scene.json'))) if os.path.exists(os.path.join(cache_dir(c), 'era5_scene.json')) else {} for c in cities]; CITY_NAMES = cities; SMS = [scene_means(cache_dir(c)) for c in cities]
    for i_f, (ci, f) in enumerate(files):       # X is filled in place (not collected in a list and stacked), so the peak memory is one copy of the data
        d = np.load(f); x = d['x'].astype(np.float32); y = d['y']
        if X is None: X = np.zeros((len(files), x.shape[0] + (1 if wm else 0)) + x.shape[1:], np.float16)
        valid_x = (x > -900).all(axis=0)
        x = np.where(x > -900, x, 0.0)
        for b in range(x.shape[0]):                       # fill masked S2 pixels with the patch mean of that band so convolutions do not see a hole
            if (~valid_x).any(): x[b][~valid_x] = x[b][valid_x].mean() if valid_x.any() else 0.0
        m = vok(y).astype(np.float32); mn = SMS[ci].get(int(d['scene']), float(d['mean'])); a = np.where(vok(y), y - mn, 0.0).astype(np.float32)
        p30 = f.replace('.npz', '_l30.npy')
        if os.path.exists(p30):
            l30 = np.load(p30); Y30.append(np.where(vok(l30), l30 - mn, 0.0).astype(np.float32) / TARGET_SCALE); M30.append(vok(l30).astype(np.float32))
        else: has30 = False; Y30.append(np.zeros((PATCH_M // 30, PATCH_M // 30), np.float32)); M30.append(np.zeros((PATCH_M // 30, PATCH_M // 30), np.float32))
        X[i_f, :x.shape[0]] = x
        if wm:      # channel 14: share of each pixel covered by OpenStreetMap water
            wf = f'{os.path.dirname(f)}/wtr_{int(d["x0"])}_{int(d["y1"])}.npy'
            if not os.path.exists(wf): raise SystemExit('the "wmask" switch needs the water masks: run  python unet_lst_v3.py download_water  first')
            X[i_f, x.shape[0]] = np.load(wf).astype(np.float32) / 255.0
        Y.append(a / TARGET_SCALE); M.append(m); S.append(int(d['scene']) + 1000 * ci); SL.append(int(d['scene'])); CI.append(ci); P.append((int(d['x0']), int(d['y1']))); MEAN.append(mn)
    CITY_IDX = np.array(CI, int); era = any(len(e) for e in eras)
    F_ = np.array([eras[ci].get(str(sl), [0.0] * NSC) for ci, sl in zip(CI, SL)], dtype=np.float32)
    return X, np.stack(Y), np.stack(M), np.array(S), np.array(P), np.array(MEAN, dtype=np.float32), F_, bool(era), np.stack(Y30), np.stack(M30), has30

def splits(S, P):
    date_held = (S % 4 == 0); place_held = ((P[:, 0] // 8000 + P[:, 1] // 8000) % 4 == 0)
    ch = (np.array(CITY_NAMES)[CITY_IDX] == HOLD_CITY) if HOLD_CITY and HOLD_CITY in CITY_NAMES and len(CITY_IDX) == len(S) else np.zeros(len(S), bool)      # a whole city held out
    out = {'train': ~date_held & ~place_held & ~ch, 'new dates, same places': date_held & ~place_held & ~ch,
           'same dates, NEW places': ~date_held & place_held & ~ch, 'new dates AND new places (strictest)': date_held & place_held & ~ch}
    if ch.any(): out[f'NEW CITY ({HOLD_CITY}), never trained on'] = ch
    return out

# ------------------------------------------------------------------ the physics switches
ALL_FLAGS = ['psf', 't4', 'derived', 'gain', 'phys', 'sign']
EXTRA_FLAGS = ['b30', 'ndwi', 'wmask', 'wphys']
def uses_w(flags): return 'wmask' in flags or 'wphys' in flags      # these switches need the OpenStreetMap water masks      # not part of 'all'; add it explicitly
PSF_SIGMA_M = 42.5          # Landsat thermal pixel is ~100 m across (FWHM); a Gaussian with this sigma has that FWHM
SIGN_W = 0.05               # weight of the sign-rule term

def parse_cfg(s):
    if s in ('base', ''): return set()
    toks = s.split('+'); f = set()
    for t in toks: f |= set(ALL_FLAGS) if t == 'all' else {t}
    bad = f - set(ALL_FLAGS) - set(EXTRA_FLAGS)
    if bad: raise SystemExit(f'unknown switch {bad}; choose from {ALL_FLAGS + EXTRA_FLAGS}, "base" or "all", joined with + (for example psf+t4 or all+b30)')
    return f
def cfg_name(flags): return '+'.join(sorted(flags)) if flags else 'base'

def band(x, n): i = BANDS.index(n); return x[:, i:i + 1]
def derived(x):
    """Physical quantities from the 13 reflectance bands: broadband albedo, vegetation cover, and four indices (NDVI, moisture, water, built-up)."""
    e = 1e-4; b2, b3, b4, b8, b11, b12 = (band(x, n) for n in ('B2', 'B3', 'B4', 'B8', 'B11', 'B12'))
    alb = 0.2266 * b2 + 0.1236 * b3 + 0.1573 * b4 + 0.3417 * b8 + 0.1170 * b11 + 0.0338 * b12        # Bonafoni & Sekertekin (2020) Sentinel-2 albedo
    ndvi = (b8 - b4) / (b8 + b4 + e); ndmi = (b8 - b11) / (b8 + b11 + e); mndwi = (b3 - b11) / (b3 + b11 + e); ndbi = (b11 - b8) / (b11 + b8 + e)
    import torch
    fvc = ((ndvi - 0.2) / 0.6).clamp(0, 1) ** 2
    return torch.cat([alb * 3, fvc, ndvi, ndmi, mndwi, ndbi], 1)

def ndwi_ch(x):
    return (band(x, 'B3') - band(x, 'B8')) / (band(x, 'B3') + band(x, 'B8') + 1e-4)

def inv_softplus(v): return math.log(math.expm1(v))

def make_net(flags):
    import torch, torch.nn as nn, torch.nn.functional as F, segmentation_models_pytorch as smp
    class Net(nn.Module):
        def __init__(self):
            super().__init__()
            self.flags = flags; self.unet = smp.Unet(encoder_name='resnet18', encoder_weights=None, in_channels=len(BANDS) + (6 if 'derived' in flags else 0) + (1 if 'ndwi' in flags else 0) + (1 if 'wmask' in flags else 0), classes=1)
            if 'gain' in flags:
                self.gain = nn.Sequential(nn.Linear(NSC, 16), nn.ReLU(), nn.Linear(16, 1)); nn.init.zeros_(self.gain[2].weight); nn.init.zeros_(self.gain[2].bias)   # starts as gain 1
            if 'phys' in flags:      # energy-balance shape: warming = k * sun * ((1 - albedo) - c * vegetation - w * water); k, c, w are learned
                self.pk = nn.Parameter(torch.tensor(inv_softplus(8.0))); self.pc = nn.Parameter(torch.tensor(inv_softplus(0.6))); self.pw = nn.Parameter(torch.tensor(inv_softplus(0.3)))
        def forward(self, x, s=None):
            d = derived(x); nd = ndwi_ch(x) if 'ndwi' in self.flags else None
            xin = x if 'wmask' in self.flags or not uses_w(self.flags) else x[:, :len(BANDS)]      # with wphys the water channel is not shown to the network
            u = self.unet(torch.cat([xin] + ([d] if 'derived' in self.flags else []) + ([nd] if nd is not None else []), 1))
            if 'gain' in self.flags: u = u * torch.exp(self.gain(s))[:, :, None, None]      # contrast between surfaces scales with sun, dryness, heat
            if 'phys' in self.flags:
                sun = s[:, 0:1, None, None] if s is not None else 1.0
                wat = d[:, 4:5]
                if nd is not None: wat = torch.maximum(wat, nd)
                if uses_w(self.flags): wat = torch.maximum(wat, x[:, len(BANDS):len(BANDS) + 1])       # the OpenStreetMap water share (channel 14)
                base = F.softplus(self.pk) * sun * ((1 - d[:, 0:1] / 3) - F.softplus(self.pc) * d[:, 1:2] - F.softplus(self.pw) * wat.clamp(min=0))
                base = base - base.mean(dim=(2, 3), keepdim=True)                              # only the pattern of the physics term, not its overall level
                u = u + base / TARGET_SCALE                                                    # the U-Net only has to learn what the simple physics misses
            return u
    return Net()

_KER = {}
def kernel1d(k, device):
    import torch
    key = (k, str(device))
    if key not in _KER:
        sig = PSF_SIGMA_M / 10.0; r = int(math.ceil(3 * sig)); g = torch.exp(-(torch.arange(-r, r + 1, dtype=torch.float32) ** 2) / (2 * sig ** 2)); g = g / g.sum()
        box = torch.ones(k) / k; ker = torch.nn.functional.conv1d(torch.nn.functional.pad(box[None, None], (len(g) - 1, len(g) - 1)), g[None, None].flip(-1))[0, 0]   # box of k pixels blurred by the sensor
        _KER[key] = (ker.to(device), (len(ker) - k) // 2)
    return _KER[key]

def aggregate(t, k, psf):
    """Average a 10 m image in k x k blocks (k = 12 -> 120 m). With psf the average is also blurred by the sensor's point spread function."""
    import torch.nn.functional as F
    if not psf: return F.avg_pool2d(t, k)
    ker, r = kernel1d(k, t.device); k2 = ker[:, None] * ker[None, :]
    return F.conv2d(F.pad(t, (r, r, r, r), mode='reflect'), k2[None, None], stride=k)

def agg_anom(out, mean, k, flags):
    """Model output (anomaly / 5) -> block averages (anomaly / 5). With 't4', averaging is done on radiance (T to the 4th), which is how a thermal sensor averages."""
    psf = 'psf' in flags
    if 't4' not in flags: return aggregate(out, k, psf)
    t = out * TARGET_SCALE + mean[:, :, None, None] + 273.15
    return ((aggregate(t ** 4, k, psf)) ** 0.25 - 273.15 - mean[:, :, None, None]) / TARGET_SCALE

def ring_mask(m, w=1):
    """With the sensor blur, blocks at the patch edge need pixels outside the patch, so they are left out of the loss (w blocks deep)."""
    m = m.clone(); m[..., :w, :] = 0; m[..., -w:, :] = 0; m[..., :, :w] = 0; m[..., :, -w:] = 0; return m
def edge_w(k): return int(math.ceil(math.ceil(3 * PSF_SIGMA_M / 10.0) / k))      # how many blocks of k pixels the blur reaches beyond the patch edge

def masked_mse(pred_p, y, m): return (((pred_p - y) ** 2) * m).sum() / m.sum().clamp(min=1)

def sign_loss(out, x):
    """Inside each 120 m block: greener should be cooler (corr with NDVI <= 0), more built-up should be hotter (corr with NDBI >= 0). Only blocks that are mixed are used."""
    import torch
    d = derived(x); P = lambda a: aggregate(a, BLOCK, False)
    def corr(a, b):
        ma, mb = P(a), P(b); cov = P(a * b) - ma * mb; va = (P(a * a) - ma ** 2).clamp(min=0); vb = (P(b * b) - mb ** 2).clamp(min=0)
        return cov / torch.sqrt(va * vb + 1e-8), (vb > 0.0025).float()
    c1, w1 = corr(out, d[:, 2:3]); c2, w2 = corr(out, d[:, 5:6])
    return (torch.relu(c1) * w1).sum() / w1.sum().clamp(min=1) + (torch.relu(-c2) * w2).sum() / w2.sum().clamp(min=1)

# ------------------------------------------------------------------ training
def train(cfg='all'):
    import torch
    flags = parse_cfg(cfg); name = cfg_name(flags); torch.manual_seed(SEED); np.random.seed(SEED)
    dev = 'cuda' if torch.cuda.is_available() else 'cpu'; X, Y, M, S, P, MEAN, F_, has_era, Y30, M30, has30 = load_patches(uses_w(flags)); sp = splits(S, P)
    if 'b30' in flags and not has30: raise SystemExit('the "b30" switch needs the 30 m Landsat files: run  python unet_lst_v3.py download30  first')
    if 'gain' in flags and not has_era: raise SystemExit('the "gain" switch needs the weather file: run  python unet_lst_v3.py era5  first')
    tr = np.where(sp['train'])[0]; print('config:', name, '| patches:', len(X), 'train:', len(tr), 'device:', dev)
    model = make_net(flags).to(dev); opt = torch.optim.AdamW(model.parameters(), lr=LR, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=LR, total_steps=EPOCHS * math.ceil(len(tr) / BATCH))
    b30 = 'b30' in flags; kb = 3 if b30 else BLOCK; YY, MM = (Y30, M30) if b30 else (Y, M)       # loss on 30 m blocks (b30) or on 120 m blocks
    Xt, Yt, Mt, MEt, Ft = torch.from_numpy(X), torch.from_numpy(YY[:, None]), torch.from_numpy(MM[:, None]), torch.from_numpy(MEAN[:, None]), torch.from_numpy(F_)
    for ep in range(EPOCHS):
        model.train(); np.random.shuffle(tr); tot = 0; sg = 0
        for i in range(0, len(tr), BATCH):
            ix = tr[i:i + BATCH]; x, y, m, me, s = Xt[ix].to(dev).float(), Yt[ix].to(dev), Mt[ix].to(dev), MEt[ix].to(dev), Ft[ix].to(dev)
            k = np.random.randint(4); fl = np.random.rand() < 0.5                     # random rotation / flip, applied identically to input and target
            if k: x, y, m = (torch.rot90(a, k, (2, 3)) for a in (x, y, m))
            if fl: x, y, m = (torch.flip(a, (3,)) for a in (x, y, m))
            out = model(x, s); p = agg_anom(out, me, kb, flags); mm = ring_mask(m, edge_w(kb)) if 'psf' in flags else m
            loss = masked_mse(p, y, mm); tot += loss.item() * len(ix)
            if 'sign' in flags: sl = sign_loss(out, x); loss = loss + SIGN_W * sl; sg += sl.item() * len(ix)
            opt.zero_grad(); loss.backward(); opt.step(); sched.step()
        if ep % 5 == 0 or ep == EPOCHS - 1:
            extra = f' | sign-rule violation {sg / len(tr):.3f}' if 'sign' in flags else ''
            print(f'epoch {ep}  train RMSE (C, {kb * 10} m blocks): {math.sqrt(tot / len(tr)) * TARGET_SCALE:.3f}{extra}', flush=True)
    torch.save(model.state_dict(), f'unet_lst_v3_{name}{SUF}.pt')
    if 'phys' in flags: print('learned physics: warming per unit sun', float(torch.nn.functional.softplus(model.pk).detach()), '| vegetation cooling', float(torch.nn.functional.softplus(model.pc).detach()), '| water cooling', float(torch.nn.functional.softplus(model.pw).detach()))

# ------------------------------------------------------------------ evaluation
def seed_file(name, sd): return f'unet_lst_v3_{name}' + ('' if sd == 7 else f'_s{sd}') + TAGS + '.pt'

def load_model(flags, name, dev):
    """One saved model, or (when ENS is set) the average of several seeds' saved models, as a single model."""
    import torch
    if not ENS:
        m = make_net(flags).to(dev); m.load_state_dict(torch.load(f'unet_lst_v3_{name}{SUF}.pt', map_location=dev)); return m
    class Ens(torch.nn.Module):
        def __init__(self, ms): super().__init__(); self.ms = torch.nn.ModuleList(ms)
        def forward(self, x, s): return torch.stack([m(x, s) for m in self.ms]).mean(0)
        def spread(self, x, s): return torch.stack([m(x, s) for m in self.ms]).std(0, unbiased=False)
    ms = []
    for sd in ENS:
        m = make_net(flags).to(dev); m.load_state_dict(torch.load(seed_file(name, sd), map_location=dev)); ms.append(m)
    print('ensemble of seeds', ENS); return Ens(ms).to(dev)

def predict_spread(model, x, s, dev):
    """How much the ensemble members disagree at each 10 m pixel (zeros for a single model)."""
    import torch
    if not hasattr(model, 'spread'): return np.zeros(x.shape[-2:], np.float32)
    model.eval()
    with torch.no_grad(): return model.spread(torch.tensor(np.asarray(x, dtype=np.float32)[None]).to(dev), torch.tensor(s[None]).to(dev))[0, 0].cpu().numpy()

def predict(model, x, s, dev):
    import torch
    model.eval()
    with torch.no_grad(): return model(torch.tensor(np.asarray(x, dtype=np.float32)[None]).to(dev), torch.tensor(s[None]).to(dev))[0, 0].cpu().numpy()

def evaluate(cfg='all'):
    import torch, torch.nn.functional as F
    flags = parse_cfg(cfg); name = cfg_name(flags); dev = 'cuda' if torch.cuda.is_available() else 'cpu'
    X, Y, M, S, P, MEAN, F_, has_era, Y30, M30, has30 = load_patches(uses_w(flags)); sp = splits(S, P)
    model = load_model(flags, name, dev); print('model:', name)
    if SUBSET:      # score only the original scenes ('old') or only the scenes added by download_more ('new')
        s0 = json.load(open(f'{CACHE}/season.json'))['first_new_scene']; keep = (S < s0) if SUBSET == 'old' else (S >= s0)
        sp = {k: v & keep for k, v in sp.items()}; print('subset:', SUBSET, '(scene numbers', '<' if SUBSET == 'old' else '>=', s0, ')')
    def agg(o, i, k): return agg_anom(torch.tensor(o)[None, None], torch.tensor([[MEAN[i]]]), k, flags)[0, 0].numpy()
    for tname in list(sp)[1:]:
        ix = np.where(sp[tname])[0]; se = n = 0.0; sd = 0.0
        for i in ix:
            p = agg(predict(model, X[i], F_[i], dev), i, BLOCK); m = (ring_mask(torch.tensor(M[i]), edge_w(BLOCK)).numpy() if 'psf' in flags else M[i]) > 0
            se += (((p - Y[i]) ** 2)[m]).sum(); n += m.sum(); sd += ((Y[i][m] - Y[i][m].mean()) ** 2).sum() if m.any() else 0
        line = f'[{tname}] RMSE at 120 m (C): {math.sqrt(se / n) * TARGET_SCALE:.3f} | SD of real pattern (C): {math.sqrt(sd / n) * TARGET_SCALE:.3f} | patches: {len(ix)}'
        if has30:       # the same model scored against Landsat's 30 m product (a smoothed reference: only a rough check)
            s3 = n3 = 0.0
            for i in ix:
                p3 = agg(predict(model, X[i], F_[i], dev), i, 3); m3 = (ring_mask(torch.tensor(M30[i]), edge_w(3)).numpy() if 'psf' in flags else M30[i]) > 0
                s3 += (((p3 - Y30[i]) ** 2)[m3]).sum(); n3 += m3.sum()
            line += f' | RMSE at 30 m (C): {math.sqrt(s3 / max(n3, 1)) * TARGET_SCALE:.3f}'
        print(line)
    # degrade-and-compare: 360 m in, 120 m out, real 120 m as the answer key. Only the inner 26 x 26 blocks are scored, so the sensor blur at the edge cannot matter.
    for tname in ['new dates, same places', 'new dates AND new places (strictest)'] + [k for k in sp if k.startswith('NEW CITY')]:
        res = {'plain': 0.0, 'unet full': 0.0, 'unet half detail': 0.0}; n = 0
        for i in np.where(sp[tname])[0]:
            if M[i].min() < 1: continue
            y = torch.tensor(Y[i][:30, :30][None, None]); l360 = F.avg_pool2d(y, 3); out = torch.tensor(predict(model, X[i], F_[i], dev))[None, None][:, :, :360, :360]
            mean = torch.tensor([[MEAN[i]]]); p120 = agg_anom(out, mean, 12, flags); p360 = agg_anom(out, mean, 36, flags)
            up = lambda a: F.interpolate(a, size=(30, 30), mode='bicubic', align_corners=False)
            plain = up(l360); detail = p120 + up(l360 - p360) - plain; sl = (slice(None), slice(None), slice(2, 28), slice(2, 28))
            for k, v in (('plain', plain), ('unet full', plain + detail), ('unet half detail', plain + 0.5 * detail)): res[k] += ((v - y)[sl] ** 2).mean().item()
            n += 1
        if n: print(f'[degrade 360->120 m, {tname}] RMSE vs real 120 m (C):', {k: round(math.sqrt(v / n) * TARGET_SCALE, 3) for k, v in res.items()}, '| patches:', n)

def probe(lat, lon, start='2025-06-01', end='2025-08-31', half=8):
    """What Sentinel-2 sees at a spot (for example a canal): along a west-east and a north-south line through it, prints the bands and the water/vegetation indices
    (10 m pixels, summer median). Click the map to get the coordinates, then:  python unet_lst_v3.py probe 41.3123 69.2345"""
    ee = ee_setup(); utm = ee.Projection('EPSG:32642'); roi = roi_geom(ee); cx, cy = ll_to_utm42(lon, lat); n = 2 * half + 1
    x0, y1 = int(cx // 10 * 10) - half * 10, int(cy // 10 * 10) + (half + 1) * 10
    S2 = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED').filterBounds(roi).filterDate(start, end).filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', S2_CLOUD))
    x = pull(ee, s2_stack(ee, S2).reproject(utm.atScale(10)), x0, y1, n, 10); ok = (x > -900).all(axis=0)
    dwi = ee.ImageCollection('GOOGLE/DYNAMICWORLD/V1').filterBounds(roi).filterDate(start, end).select(['water', 'built']).mean().reproject(utm.atScale(10))
    dw = pull(ee, dwi, x0, y1, n, 10)       # Dynamic World: a land-cover model's average probability of water / built-up at each pixel over the period
    g, nir, sw, bl, rd = (x[BANDS.index(b)] for b in ('B3', 'B8', 'B11', 'B2', 'B4'))
    e = 1e-4; ndwi = (g - nir) / (g + nir + e); mndwi = (g - sw) / (g + sw + e); ndvi = (nir - rd) / (nir + rd + e)
    alb = 0.2266 * bl + 0.1236 * g + 0.1573 * rd + 0.3417 * nir + 0.1170 * sw + 0.0338 * x[BANDS.index('B12')]
    print(f'pixel size 10 m, {n} x {n} pixels centred on {lat}, {lon}; median of {start} to {end}')
    for title, idx in (('west -> east through the point', [(half, j) for j in range(n)]), ('north -> south through the point', [(i, half) for i in range(n)])):
        print(f'\n{title}\n offset_m  green(B3)  NIR(B8)  SWIR(B11)  NDWI   MNDWI   NDVI   albedo  DW_water DW_built')
        for (i, j) in idx:
            d = (j - half) * 10 if title.startswith('west') else (i - half) * 10
            if ok[i, j]: print(f'{d:7d}   {g[i, j]:8.3f}  {nir[i, j]:7.3f}  {sw[i, j]:9.3f}  {ndwi[i, j]:6.2f} {mndwi[i, j]:6.2f} {ndvi[i, j]:6.2f} {alb[i, j]:7.3f}   {dw[0, i, j]:6.2f}   {dw[1, i, j]:6.2f}')
            else: print(f'{d:7d}   (cloud or no data)')
    print('\nReading it: open water has NDWI > 0 and MNDWI > 0 and low NIR (< 0.1); concrete, roads and dry channels have albedo 0.2-0.3, NDVI near 0 and no positive water index. DW_water close to 1 means a land-cover model recognises water there.')


# ------------------------------------------------------------------ random forest baseline, trained and scored on exactly the same patches and splits as the U-Net
def rf_features(x, nb):
    """The inputs of the earlier Earth Engine forest, from the 13 bands: NDVI, NDBI, MNDWI, red, SWIR2 (10 m) and, with nb, the average NDVI / NDBI / red within 50, 100 and 200 m."""
    from scipy import ndimage as ndi
    b = lambda n: x[BANDS.index(n)]; e = 1e-4
    ndvi = (b('B8') - b('B4')) / (b('B8') + b('B4') + e); ndbi = (b('B11') - b('B8')) / (b('B11') + b('B8') + e); mndwi = (b('B3') - b('B11')) / (b('B3') + b('B11') + e)
    f = [ndvi, ndbi, mndwi, b('B4'), b('B12')]
    if nb:
        H = x.shape[1]; g = np.stack([ndvi, ndbi, b('B4')]).reshape(3, H // 3, 3, H // 3, 3).mean((2, 4))          # averaged to a 30 m grid
        for r in (50, 100, 200):
            h = int(math.ceil(r / 30.0)); yy, xx = np.mgrid[-h:h + 1, -h:h + 1] * 30.0; k = ((yy ** 2 + xx ** 2) <= r * r).astype(np.float32); k /= k.sum()
            for c in range(3): f.append(ndi.zoom(ndi.convolve(g[c], k, mode='reflect'), 3, order=1))
    return np.stack(f).astype(np.float32)

def rf_baseline(nb=True, trees=150):
    import torch, torch.nn.functional as F
    from sklearn.ensemble import RandomForestRegressor
    X, Y, M, S, P, MEAN, F_, has_era, Y30, M30, has30 = load_patches(); sp = splits(S, P); name = 'forest + neighbourhood' if nb else 'forest, base inputs'
    if SUBSET:
        s0 = json.load(open(f'{CACHE}/season.json'))['first_new_scene']; keep = (S < s0) if SUBSET == 'old' else (S >= s0); sp = {k: (v if k == 'train' else v & keep) for k, v in sp.items()}; print('subset:', SUBSET, '(only the test sets are restricted; training uses all training patches)')
    tr = np.where(sp['train'])[0]; feats = {}; t0 = time.time()
    rows, ys = [], []
    for j, i in enumerate(tr):        # training rows at 120, 240 and 360 m (the earlier forest was trained at several scales and applied at 10 m)
        f = rf_features(X[i].astype(np.float32), nb)
        for k in (12, 24, 36):
            nbk = 384 // k; hh = nbk * k; ff = f[:, :hh, :hh].reshape(f.shape[0], nbk, k, nbk, k).mean((2, 4)); g = k // 12
            yy = Y[i][:nbk * g, :nbk * g].reshape(nbk, g, nbk, g).mean((1, 3)); mm = M[i][:nbk * g, :nbk * g].reshape(nbk, g, nbk, g).min((1, 3)) > 0
            rows.append(ff[:, mm].T); ys.append(yy[mm])
        if j % 200 == 0: print('features for training patches:', j, '/', len(tr), f'{time.time() - t0:.0f}s', flush=True)
    Xr, yr = np.concatenate(rows), np.concatenate(ys); print('training rows:', len(yr), 'inputs:', Xr.shape[1])
    rf = RandomForestRegressor(n_estimators=trees, min_samples_leaf=5, max_features=0.5, max_samples=0.3, n_jobs=-1, random_state=SEED).fit(Xr, yr); print(f'forest trained ({time.time() - t0:.0f}s)')
    blk = {}
    def pred30(i):       # forest prediction at every third 10 m pixel (30 m spacing), anomaly / TARGET_SCALE; also the prediction from 120 m block-average inputs
        f = rf_features(X[i].astype(np.float32), nb); fb = f.reshape(f.shape[0], 32, 12, 32, 12).mean((2, 4)); blk[i] = rf.predict(fb.reshape(fb.shape[0], -1).T).reshape(32, 32).astype(np.float32)
        f = f[:, ::3, ::3]; return rf.predict(f.reshape(f.shape[0], -1).T).reshape(f.shape[1], f.shape[2]).astype(np.float32)
    print('model:', name)
    cache = {}
    def get(i):
        if i not in cache: cache[i] = pred30(i)
        return cache[i]
    for tname in list(sp)[1:]:
        ix = np.where(sp[tname])[0]; se = n = 0.0; sd = 0.0
        for i in ix:
            p = get(i).reshape(32, 4, 32, 4).mean((1, 3)); m = ring_mask(torch.tensor(M[i]), edge_w(BLOCK)).numpy() > 0
            se += (((p - Y[i]) ** 2)[m]).sum(); n += m.sum(); sd += ((Y[i][m] - Y[i][m].mean()) ** 2).sum() if m.any() else 0
        sb = 0.0
        for i in ix: m = ring_mask(torch.tensor(M[i]), edge_w(BLOCK)).numpy() > 0; sb += (((blk[i] - Y[i]) ** 2)[m]).sum()
        print(f'[{tname}] RMSE at 120 m (C): {math.sqrt(se / n) * TARGET_SCALE:.3f} (10 m forest map averaged to 120 m) | {math.sqrt(sb / n) * TARGET_SCALE:.3f} (forest applied to 120 m block averages, no 10 m map: how the earlier Earth Engine test scored it) | SD of real pattern (C): {math.sqrt(sd / n) * TARGET_SCALE:.3f} | patches: {len(ix)}')
    for tname in ['new dates, same places', 'new dates AND new places (strictest)'] + [k for k in sp if k.startswith('NEW CITY')]:       # the same degrade-and-compare as for the U-Net
        res = {'plain': 0.0, 'forest full': 0.0, 'forest half detail': 0.0}; n = 0
        for i in np.where(sp[tname])[0]:
            if M[i].min() < 1: continue
            y = torch.tensor(Y[i][:30, :30][None, None]); l360 = F.avg_pool2d(y, 3); q = get(i)[:120, :120]
            p120 = torch.tensor(q.reshape(30, 4, 30, 4).mean((1, 3)))[None, None]; p360 = torch.tensor(q.reshape(10, 12, 10, 12).mean((1, 3)))[None, None]
            up = lambda a: F.interpolate(a, size=(30, 30), mode='bicubic', align_corners=False)
            plain = up(l360); detail = p120 + up(l360 - p360) - plain; sl = (slice(None), slice(None), slice(2, 28), slice(2, 28))
            for k, v in (('plain', plain), ('forest full', plain + detail), ('forest half detail', plain + 0.5 * detail)): res[k] += ((v - y)[sl] ** 2).mean().item()
            n += 1
        if n: print(f'[degrade 360->120 m, {tname}] RMSE vs real 120 m (C):', {k: round(math.sqrt(v / n) * TARGET_SCALE, 3) for k, v in res.items()}, '| patches:', n)


def rdp(pts, tol):
    """Douglas-Peucker simplification of a closed ring (degrees): split at the point farthest from the start, simplify both halves."""
    def line(p):
        p = np.asarray(p, float); keep = np.zeros(len(p), bool); keep[0] = keep[-1] = True; stack = [(0, len(p) - 1)]
        while stack:
            a, b = stack.pop()
            if b <= a + 1: continue
            q0, q1 = p[a], p[b]; d = q1 - q0; nrm = math.hypot(*d) or 1e-12
            dist = np.abs(d[0] * (p[a + 1:b, 1] - q0[1]) - d[1] * (p[a + 1:b, 0] - q0[0])) / nrm; k = int(np.argmax(dist))
            if dist[k] > tol: keep[a + 1 + k] = True; stack += [(a, a + 1 + k), (a + 1 + k, b)]
        return p[keep].tolist()
    p = np.asarray(pts, float)
    if len(p) < 8: return p.tolist()
    k = int(np.argmax(np.hypot(*(p - p[0]).T)))
    return line(p[:k + 1])[:-1] + line(p[k:])

def fetch_boundaries(names=None):
    """Administrative boundaries from OpenStreetMap (Nominatim) for the given city names, added to cities.geojson. Existing entries are kept unless --refresh."""
    import urllib.request, urllib.parse
    names = [n.lower() for n in (names or list(CITY_QUERY))]; feats = []
    if os.path.exists(CITIES_FILE): feats = json.load(open(CITIES_FILE)).get('features', [])
    have = {str((f.get('properties') or {}).get('name', '')).lower() for f in feats}
    for nm in names:
        if nm in have and '--refresh' not in sys.argv: print(nm, 'already in', CITIES_FILE, '(use --refresh to replace it)'); continue
        url = 'https://nominatim.openstreetmap.org/search?' + urllib.parse.urlencode({'q': CITY_QUERY.get(nm, nm), 'format': 'jsonv2', 'polygon_geojson': 1, 'limit': 1})
        try: res = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'tashkent-lst'}), timeout=90).read())
        except Exception as e: print(nm, 'failed:', e); continue
        g = res[0].get('geojson') if res else None
        if not g or g.get('type') not in ('Polygon', 'MultiPolygon'): print(nm, ': no polygon boundary found (got', g and g.get('type'), ')'); continue
        rings = [p[0] for p in (g['coordinates'] if g['type'] == 'MultiPolygon' else [g['coordinates']])]; ring = max(rings, key=ring_area_deg); n0 = len(ring); ring = rdp(ring, 0.0004)
        feats = [f for f in feats if str((f.get('properties') or {}).get('name', '')).lower() != nm]
        feats.append({'type': 'Feature', 'properties': {'name': nm, 'osm': res[0].get('display_name', '')[:120]}, 'geometry': {'type': 'Polygon', 'coordinates': [ring]}})
        print(nm, ':', res[0].get('display_name', '')[:80], '|', n0, '->', len(ring), 'vertices'); time.sleep(1.5)
    json.dump({'type': 'FeatureCollection', 'features': feats}, open(CITIES_FILE, 'w')); print('saved', CITIES_FILE); cities_report()

def cities_report():
    """Checks cities.geojson: names, vertices, area, centre and about how many 3.84 km tiles fall inside each polygon."""
    from matplotlib.path import Path
    c = read_cities()
    if not c: print('no', CITIES_FILE, 'or no polygons in it'); return
    for nm, r in sorted(c.items()):
        xy = np.array([ll_to_utm42(*p) for p in r]); area = abs(sum(xy[i, 0] * xy[i + 1, 1] - xy[i + 1, 0] * xy[i, 1] for i in range(len(xy) - 1))) / 2 / 1e6
        lo, la = np.array(r).T; xs = np.arange(math.floor(xy[:, 0].min() / PATCH_M), math.ceil(xy[:, 0].max() / PATCH_M)) * PATCH_M; ys = np.arange(math.floor(xy[:, 1].min() / PATCH_M), math.ceil(xy[:, 1].max() / PATCH_M)) * PATCH_M
        path = Path(xy); n = sum(bool(path.contains_point((x + PATCH_M / 2, y + PATCH_M / 2))) for x in xs for y in ys)
        warn = [] if len(r) > 3 else ['too few vertices']
        if abs(r[0][0] - r[-1][0]) > 1e-9 or abs(r[0][1] - r[-1][1]) > 1e-9: warn.append('ring not closed')
        print(f'{nm:10s} vertices {len(r):4d} | area {area:7.1f} km2 | centre {lo.mean():.3f}, {la.mean():.3f} | UTM zone {int((lo.mean() + 180) // 6) + 1} | about {n} tiles of 3.84 km inside' + (' | ' + ', '.join(warn) if warn else ''))


def fix_means(names=None):
    """For the extra cities, anomalies are measured from the scene's average over the downloaded tiles (built-up areas), not over the whole administrative polygon,
    which also holds mountains, fields and desert and moves with snow cover. Writes scene_means.json in each city's cache; the patches themselves are not changed."""
    names = [n.lower() for n in (names or [c for c in read_cities() if c != 'tashkent'])]
    for c in names:
        d = cache_dir(c); fs = sorted(glob.glob(f'{d}/*.npz'))
        if not fs: print(c, ': no patches'); continue
        acc = {}; old = {}
        for f in fs:
            z = np.load(f); y = z['y']; ok = vok(y); sc = int(z['scene']); sm, n = acc.get(sc, (0.0, 0)); acc[sc] = (sm + float(y[ok].sum()), n + int(ok.sum())); old[sc] = float(z['mean'])
        new = {sc: sm / n for sc, (sm, n) in acc.items() if n > 0}; json.dump({str(k): v for k, v in new.items()}, open(os.path.join(d, 'scene_means.json'), 'w'))
        diff = np.array([new[k] - old[k] for k in new]); print(f'{c:10s} scenes {len(new)} | new minus old scene mean (C): median {np.median(diff):+.1f}, from {diff.min():+.1f} to {diff.max():+.1f}')
    print('saved scene_means.json; training and evaluation use it from now on')


def data_check(max_x=15):
    """Looks for bad data city by city: spread of the Landsat target, scene mean temperatures, extreme values and the worst patches; also a sample of the Sentinel-2 inputs."""
    import datetime
    names = ['tashkent'] + sorted(c for c in read_cities() if c != 'tashkent')
    print(f'{"city":10s} {"patches":>7s} {"scenes":>6s} | scene mean C (min / median / max) | patch SD of anomaly C (median / p90 / max) | share |anomaly| > 15 C | patch mean anomaly C (median / p90 of the absolute value) | inputs finite, max reflectance')
    worst = []
    for c in names:
        d = cache_dir(c); fs = sorted(glob.glob(f'{d}/*.npz'))
        if not fs: continue
        sds, mins, maxs, means, scenes, valid, big, ts, off = [], [], [], [], set(), [], 0, [], []; smc = scene_means(d)
        for f in fs:
            z = np.load(f); y = z['y']; ok = vok(y); mn = smc.get(int(z['scene']), float(z['mean']))
            if ok.sum() < 10: continue
            a = y[ok] - mn; sds.append(float(a.std())); off.append(abs(float(a.mean()))); mins.append(float(a.min())); maxs.append(float(a.max())); means.append(mn); scenes.add(int(z['scene'])); valid.append(ok.mean()); big += int((np.abs(a) > 15).sum()); ts.append(float(z['t']))
            worst.append((sds[-1], c, os.path.basename(f), datetime.datetime.utcfromtimestamp(float(z['t']) / 1000).strftime('%Y-%m-%d'), mn, mins[-1], maxs[-1]))
        sd = np.array(sds); tot = sum(int(vok(np.load(f)['y']).sum()) for f in fs[:50]) / max(1, min(50, len(fs)))
        fin, mx = True, 0.0
        for f in fs[::max(1, len(fs) // max_x)][:max_x]:
            x = np.load(f)['x'].astype(np.float32); fin &= bool(np.isfinite(x).all()); mx = max(mx, float(np.nanmax(x)))
        print(f'{c:10s} {len(sd):7d} {len(scenes):6d} | {min(means):6.1f} / {np.median(means):6.1f} / {max(means):6.1f}       | {np.median(sd):5.2f} / {np.percentile(sd, 90):5.2f} / {sd.max():6.2f}        | {100 * big / max(1, sum(v * 1024 for v in valid)):6.2f}%             | {np.median(off):5.2f} / {np.percentile(off, 90):5.2f}                          | {fin}, {mx:.2f}')
    print('\nthe 12 patches with the largest spread (SD of the anomaly):')
    for w in sorted(worst, reverse=True)[:12]: print(f'  SD {w[0]:5.2f} C | {w[1]:10s} | {w[2]} | {w[3]} | scene mean {w[4]:5.1f} C | anomaly min {w[5]:6.1f}, max {w[6]:6.1f}')
    print('\nreading it: a normal city has patch SD around 2-3 C, scene means between about 10 and 55 C and reflectance up to about 1-2. Values far outside that point to bad scenes (cloud, snow, scene edge) or mountains inside the polygon.')


# ------------------------------------------------------------------ the 10 m map
def render_tiles(model, flags, X0, Y1, tiles, get_x, get_l, sv, mean, dev, water_lines=None, keep=None, fill=0.0):
    """Estimates 10 m surface temperature on a grid of tiles x tiles patches (3.84 km each) whose upper-left corner is (X0, Y1) in UTM 42N.
    get_x(x0, y1) -> Sentinel-2 array (13, PX + 96, PX + 96) with -999 where masked; get_l(x0, y1) -> Landsat 120 m array (32 x 32) with -999 where missing;
    sv = the 6 weather numbers, mean = the scene's mean Landsat temperature. keep(x0, y1) -> False skips a tile (left as `fill`).
    Output block averages are tied to Landsat where Landsat is valid (the residual at 120 m is added back, smoothly)."""
    import torch, torch.nn.functional as F
    mg = 48; N = PX * tiles; mb = mg // BLOCK
    full = np.full((N, N), fill, np.float32); plain = np.full((N, N), fill, np.float32); unc = np.full((N, N), fill, np.float32); wmap = np.zeros((N, N), np.float32)
    done, skipped, lv = 0, [], []
    for ty in range(tiles):
        for tx in range(tiles):
            x0, y1 = X0 + tx * PATCH_M, Y1 - ty * PATCH_M
            if keep is not None and not keep(x0, y1): continue
            x = get_x(x0, y1); ok = (x > -900).all(axis=0)
            if ok.mean() < 0.5: skipped.append((x0, y1)); continue      # Sentinel-2 mostly masked (cloud): no honest estimate for this tile
            for b in range(x.shape[0]): x[b] = np.where(ok, x[b], x[b][ok].mean())
            if uses_w(flags): x = np.concatenate([x, water_raster(water_lines, x0 - mg * 10, y1 + mg * 10, PX + 2 * mg)[None]])
            o = predict(model, x, sv, dev) ; out = o[mg:-mg, mg:-mg] * TARGET_SCALE; spread = predict_spread(model, x, sv, dev)[mg:-mg, mg:-mg] * TARGET_SCALE
            pb = agg_anom(torch.tensor(o)[None, None], torch.tensor([[mean]], dtype=torch.float32), BLOCK, flags)[0, 0].numpy()[mb:-mb, mb:-mb] * TARGET_SCALE   # block averages with real surroundings (no edge padding)
            l = get_l(x0, y1); lm = l > -900; la = np.where(lm, l - mean, np.nan); lv.append(float(lm.mean()))
            diff = np.where(lm, la - pb, 0.0)                                                      # what the model missed at 120 m
            corr = F.interpolate(torch.tensor(diff, dtype=torch.float32)[None, None], size=(PX, PX), mode='bicubic', align_corners=False)[0, 0].numpy()
            sm = F.interpolate(torch.tensor(np.where(lm, la, pb), dtype=torch.float32)[None, None], size=(PX, PX), mode='bicubic', align_corners=False)[0, 0].numpy()
            sl = (slice(ty * PX, (ty + 1) * PX), slice(tx * PX, (tx + 1) * PX))
            full[sl] = out + corr + mean; plain[sl] = sm + mean; unc[sl] = spread; done += 1
            if uses_w(flags): wmap[sl] = water_raster(water_lines, x0, y1, PX)
    return {'full': full, 'plain': plain, 'unc': unc, 'wmap': wmap, 'tiles_done': done, 'tiles_skipped': skipped, 'landsat_valid_share': float(np.mean(lv)) if lv else 0.0}

def make_map(cfg='all', centre=None, tiles=2, start='2025-06-01', end='2025-08-31'):
    centre = centre or city_centre()
    import torch, torch.nn.functional as F
    flags = parse_cfg(cfg); name = cfg_name(flags); ee = ee_setup(); dev = 'cuda' if torch.cuda.is_available() else 'cpu'; utm = ee.Projection('EPSG:32642')
    model = load_model(flags, name, dev); model.eval(); oname = name + TAGS + CITYS + ('_ens' if ENS else '')
    roi = roi_geom(ee); pt = list(ll_to_utm42(*centre)); WATER_LINES = load_water() if uses_w(flags) else None
    X0 = int(pt[0] // PATCH_M * PATCH_M) - (tiles // 2) * PATCH_M; Y1 = int(pt[1] // PATCH_M * PATCH_M) + PATCH_M + (tiles // 2 - 1) * PATCH_M
    L = (ee.ImageCollection('LANDSAT/LC08/C02/T1_L2').merge(ee.ImageCollection('LANDSAT/LC09/C02/T1_L2')).filterBounds(roi).filterDate(start, end).map(lambda i: prep_landsat(ee, i)).median().setDefaultProjection(utm.atScale(30)))
    lst120 = L.reduceResolution(ee.Reducer.mean(), maxPixels=256).reproject(utm.atScale(120)).rename('LST_C')
    mean = lst120.reduceRegion(ee.Reducer.mean(), roi, 120, maxPixels=1e9, tileScale=4).get('LST_C').getInfo()
    era = ee.ImageCollection('ECMWF/ERA5_LAND/HOURLY').select(ERA_BANDS).filterDate(start, end).filter(ee.Filter.calendarRange(6, 6, 'hour')).mean()   # a typical overpass-hour summer day
    sv = np.array(era_vec(era.reduceRegion(ee.Reducer.first(), ee.Geometry.Point(list(centre)), 11132).getInfo()), dtype=np.float32); print('weather inputs for this map:', dict(zip(ERA_NAMES, sv.round(2))))
    S2 = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED').filterBounds(roi).filterDate(start, end).filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', S2_CLOUD))
    s2 = s2_stack(ee, S2).reproject(utm.atScale(10)); mg = 48; N = PX * tiles
    r = render_tiles(model, flags, X0, Y1, tiles, lambda x0, y1: pull(ee, s2, x0 - mg * 10, y1 + mg * 10, PX + 2 * mg, 10), lambda x0, y1: pull(ee, lst120, x0, y1, PATCH_M // 120, 120)[0], sv, mean, dev, WATER_LINES)
    full, plain, unc, wmap = r['full'], r['plain'], r['unc'], r['wmap']
    blend = plain + DETAIL_WEIGHT * (full - plain)
    np.save(f'lst10m_v3_{oname}_full.npy', full); np.save(f'lst10m_v3_{oname}_smooth.npy', plain); np.save(f'lst10m_v3_{oname}_blend.npy', blend)
    if ENS: np.save(f'lst10m_v3_{oname}_unc.npy', unc)
    if uses_w(flags): np.save(f'lst10m_v3_{oname}_water.npy', wmap)
    json.dump({'x0': int(X0), 'y1': int(Y1), 'n': int(N), 'detail_weight': DETAIL_WEIGHT, 'start': start, 'end': end, 'tiles': tiles, 'scene_mean_c': float(mean), 'weather': {k: float(v) for k, v in zip(ERA_NAMES, sv)}, 'seeds': ENS, 'config': oname}, open(f'lst10m_v3_{oname}_meta.json', 'w'))
    print('saved lst10m_v3_' + oname + '_*.npy (tile origin UTM 42N:', X0, Y1, '). Now run: python unet_lst_v3.py view', name + ('@ens' if ENS else ''))

def export_site(modes=('all',)):
    """Turns a saved map into what the uhi web app needs: a transparent, georeferenced PNG in the app's own colour scale (25-55 C) plus a manifest entry.
    Writes to site_export/. Copy the PNG to uhi/public/data/unet10m/ and the manifest to uhi/public/data/unet10m-manifest.json. Running it again with a different START/END adds another entry.
    SITE_ARRAY=full (default, model detail at full strength) | blend (smooth Landsat + 0.3 x detail) | smooth (no model).  SITE_ID / SITE_LABEL name the entry.  SITE_MASK=0 keeps pixels outside the city polygon."""
    def tok(m): c, _, e = m.partition('@'); c, _, tg = c.partition('#'); return cfg_name(parse_cfg(c)) + (f'_{tg}' if tg else TAGS) + CITYS + ('_ens' if e else '')
    which = os.environ.get('SITE_ARRAY', 'full'); found = [tok(m) for m in modes if os.path.exists(f'lst10m_v3_{tok(m)}_{which}.npy')]
    if not found: print('run `map <config>` first (same TAG / ENS / CITY as here)'); return
    m = found[0]; meta = json.load(open(f'lst10m_v3_{m}_meta.json')); x0, y1, n = meta['x0'], meta['y1'], meta['n']; arr = np.load(f'lst10m_v3_{m}_{which}.npy')
    os.makedirs('site_export', exist_ok=True); sid = os.environ.get('SITE_ID') or f"{meta.get('start', 'x')}_{meta.get('end', 'x')}"; fn = f'unet10m_{sid}.png'
    bounds, ok = write_png(arr, x0, y1, n, os.path.join('site_export', fn), mask=os.environ.get('SITE_MASK', '1') != '0')
    entry = {'id': sid, 'label': os.environ.get('SITE_LABEL') or f"Typical day, {meta.get('start', '')} to {meta.get('end', '')}", 'url': f'/data/unet10m/{fn}', 'bounds': bounds, 'range_c': [25, 55],
             'period': [meta.get('start'), meta.get('end')], 'scene_mean_c': meta.get('scene_mean_c'), 'weather': meta.get('weather'), 'seeds': meta.get('seeds'), 'config': meta.get('config'), 'array': which,
             'valid_share': round(float(ok.mean()), 3), 'valid_min_c': round(float(arr[ok].min()), 1), 'valid_max_c': round(float(arr[ok].max()), 1)}
    mf = os.path.join('site_export', 'unet10m-manifest.json'); old = json.load(open(mf)).get('maps', []) if os.path.exists(mf) else []
    json.dump({'maps': [e for e in old if e.get('id') != sid] + [entry]}, open(mf, 'w'), indent=1)
    print(f'wrote site_export/{fn} ({os.path.getsize(os.path.join("site_export", fn)) / 1e6:.1f} MB) and site_export/unet10m-manifest.json ({len(old) + 1} entr{"y" if len(old) == 0 else "ies"}) | pixels shown {100 * ok.mean():.0f}% | range {entry["valid_min_c"]} to {entry["valid_max_c"]} C')
    print('copy: PNG -> uhi/public/data/unet10m/   manifest -> uhi/public/data/unet10m-manifest.json')


def write_png(arr, x0, y1, n, path, mask=True):
    """Transparent, georeferenced PNG in the app's colour scale (the same palette and 25-55 C range as the Landsat PNGs). Returns the four corners (lon, lat: top-left, top-right, bottom-right, bottom-left) and the mask of shown pixels."""
    from PIL import Image
    import matplotlib; matplotlib.use('Agg'); from matplotlib.colors import LinearSegmentedColormap
    ok = np.isfinite(arr) & (arr > -50)
    if mask: ok &= poly_mask(x0, y1, n, 10.0)      # outside the city polygon there is no Landsat to anchor the map to, so leave it transparent
    cmap = LinearSegmentedColormap.from_list('lst_palette', ['#0000ff', '#00ffff', '#ffff00', '#ff7700', '#ff0000'])
    rgba = cmap(np.clip((np.nan_to_num(arr, nan=25.0) - 25.0) / 30.0, 0, 1)); rgba[~ok] = 0
    Image.fromarray((rgba * 255).astype(np.uint8), 'RGBA').save(path, optimize=True)
    size = n * 10.0; ll = lambda x, y: [float(v) for v in utm42_to_ll(x, y)]; return [ll(x0, y1), ll(x0 + size, y1), ll(x0 + size, y1 - size), ll(x0, y1 - size)], ok

def poly_mask(x0, y1, n, res=10.0, upsample=True):
    """True inside the city polygon. The grid starts at (x0, y1) and is n pixels of 10 m per side; it is returned at `res` metres per pixel, or (upsample=True) repeated back to 10 m pixels."""
    from PIL import Image, ImageDraw
    m = int(round(n * 10.0 / res)); im = Image.new('L', (m, m), 0)
    ImageDraw.Draw(im).polygon([((ll_to_utm42(lo, la)[0] - x0) / res, (y1 - ll_to_utm42(lo, la)[1]) / res) for lo, la in ROI_POLY], fill=1)
    a = np.array(im, bool); return a if (res == 10.0 or not upsample) else np.kron(a, np.ones((int(round(res / 10.0)),) * 2, bool))

# ------------------------------------------------------------------ pipeline: one 10 m map per new Landsat scene (used by pipeline/run_pipeline.py in the uhi repo)
class SceneUnusable(Exception): pass

def in_season(dt): return 4 <= dt.month <= 10 and not (dt.month == 10 and dt.day > 15)      # the months the model was trained on (1 Apr - 15 Oct); winter, snow and frost are outside what it has seen

def find_scenes(ee, roi, days_back=60, min_age_days=WINDOW_DAYS + 1):
    """Clear Landsat 8/9 scenes over the city in the last `days_back` days, in the model's season, at least `min_age_days` old (so the whole +-3 day Sentinel-2 window exists, as in training). One scene per date (the clearest).
    Returns [{'date', 'time_ms', 'scene_id', 'cloud'}] oldest first."""
    import datetime
    now = datetime.datetime.utcnow(); L = (ee.ImageCollection('LANDSAT/LC08/C02/T1_L2').merge(ee.ImageCollection('LANDSAT/LC09/C02/T1_L2')).filterBounds(roi)
        .filterDate((now - datetime.timedelta(days=days_back)).strftime('%Y-%m-%d'), (now - datetime.timedelta(days=min_age_days)).strftime('%Y-%m-%d')).filter(ee.Filter.lt('CLOUD_COVER', LANDSAT_CLOUD)))
    ids = L.aggregate_array('system:index').getInfo(); ts = L.aggregate_array('system:time_start').getInfo(); cl = L.aggregate_array('CLOUD_COVER').getInfo(); best = {}
    for sid, t, c in zip(ids, ts, cl):
        dt = datetime.datetime.utcfromtimestamp(t / 1000.0)
        if not in_season(dt): continue
        d = dt.strftime('%Y-%m-%d')
        if d not in best or c < best[d]['cloud']: best[d] = {'date': d, 'time_ms': t, 'scene_id': sid, 'cloud': c}
    return [best[d] for d in sorted(best)]

def scene_map(ee, roi, scene, flags, model, dev, water_lines):
    """The 10 m map for one Landsat scene, built the way the training patches were: Sentinel-2 median within +-3 days, ERA5-Land at the overpass hour, Landsat 120 m as the anchor and mean."""
    t = scene['time_ms']; utm = ee.Projection('EPSG:32642'); inside = ee.Image.constant(1).clip(roi); mg = 48
    L = (ee.ImageCollection('LANDSAT/LC08/C02/T1_L2').merge(ee.ImageCollection('LANDSAT/LC09/C02/T1_L2')).filterBounds(roi).filter(ee.Filter.eq('system:time_start', t)))
    if L.size().getInfo() == 0: raise SceneUnusable('Landsat scene not found')
    img = ee.Image(L.sort('CLOUD_COVER').first()); lst = prep_landsat(ee, img)
    lst120 = lst.reduceResolution(ee.Reducer.mean(), maxPixels=256).reproject(utm.atScale(120)).rename('LST_C').updateMask(inside)
    mean = lst120.reduceRegion(ee.Reducer.mean(), roi, 120, maxPixels=1e9, tileScale=4).get('LST_C').getInfo()
    if mean is None: raise SceneUnusable('no valid Landsat pixels inside the polygon (cloud or edge)')
    t0 = ee.Date(t); S2 = (ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED').filterBounds(roi).filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', S2_CLOUD)).filterDate(t0.advance(-WINDOW_DAYS, 'day'), t0.advance(WINDOW_DAYS + 1, 'day')))
    if S2.size().getInfo() == 0: raise SceneUnusable('no Sentinel-2 image within the window')
    s2 = s2_stack(ee, S2)
    if s2.bandNames().size().getInfo() == 0: raise SceneUnusable('Sentinel-2 surface-reflectance images have no matching top-of-atmosphere images')
    s2 = s2.reproject(utm.atScale(10)); h = ee.Date(round(t / 3600000.0) * 3600000)
    v = ee.ImageCollection('ECMWF/ERA5_LAND/HOURLY').select(ERA_BANDS).filterDate(h, h.advance(1, 'hour')).first().reduceRegion(ee.Reducer.first(), ee.Geometry.Point(list(city_centre())), 11132).getInfo()
    if not v or any(v.get(b) is None for b in ERA_BANDS): raise SceneUnusable('ERA5-Land weather not available yet for this hour')
    sv = np.array(era_vec(v), dtype=np.float32)
    pts = [ll_to_utm42(lo, la) for lo, la in ROI_POLY]; X0 = int(math.floor(min(p[0] for p in pts) / PATCH_M) * PATCH_M); Y1 = int(math.ceil(max(p[1] for p in pts) / PATCH_M) * PATCH_M)
    tiles = int(math.ceil(max(max(p[0] for p in pts) - X0, Y1 - min(p[1] for p in pts)) / PATCH_M)); pm = poly_mask(X0, Y1, tiles * PX, 120.0, upsample=False); k = PATCH_M // 120
    def keep(x0, y1): i, j = int((Y1 - y1) // PATCH_M), int((x0 - X0) // PATCH_M); return bool(pm[i * k:(i + 1) * k, j * k:(j + 1) * k].any())
    r = render_tiles(model, flags, X0, Y1, tiles, lambda x0, y1: pull(ee, s2, x0 - mg * 10, y1 + mg * 10, PX + 2 * mg, 10), lambda x0, y1: pull(ee, lst120, x0, y1, PATCH_M // 120, 120)[0], sv, mean, dev, water_lines, keep=keep, fill=np.nan)
    meta = {'x0': X0, 'y1': Y1, 'n': PX * tiles, 'date': scene['date'], 'scene_id': scene['scene_id'], 'scene_mean_c': float(mean), 'weather': {a: float(b) for a, b in zip(ERA_NAMES, sv)}, 'seeds': ENS,
            'landsat_valid_share': r['landsat_valid_share'], 'tiles_done': r['tiles_done'], 'tiles_skipped': [list(t_) for t_ in r['tiles_skipped']], 'tiles_wanted': int(sum(keep(X0 + a * PATCH_M, Y1 - b * PATCH_M) for a in range(tiles) for b in range(tiles)))}
    return r['full'], r['unc'], meta

def qc_scene(full, meta):
    """Checks a finished scene map before it is published. Returns a list of problems (empty = fine)."""
    x0, y1, n = meta['x0'], meta['y1'], meta['n']; inside = poly_mask(x0, y1, n, 10.0); v = full[inside]; bad = []
    if meta['tiles_done'] < 0.9 * max(1, meta['tiles_wanted']): bad.append(f"only {meta['tiles_done']} of {meta['tiles_wanted']} tiles could be estimated (Sentinel-2 cloud?)")
    fin = v[np.isfinite(v)]
    if fin.size < 0.85 * v.size: bad.append(f'only {100 * fin.size / max(1, v.size):.0f}% of the city has a value')
    if fin.size:
        p = np.percentile(fin, [1, 50, 99])
        if not (10 < p[0] and p[2] < 80 and 15 < p[1] < 65): bad.append(f'implausible temperatures (1st / 50th / 99th percentile {p[0]:.1f} / {p[1]:.1f} / {p[2]:.1f} C)')
        if abs(float(fin.mean()) - meta['scene_mean_c']) > 4: bad.append(f"city mean {fin.mean():.1f} C is far from the Landsat mean {meta['scene_mean_c']:.1f} C")
    if meta['landsat_valid_share'] < 0.5: bad.append(f"Landsat covers only {100 * meta['landsat_valid_share']:.0f}% of the tiles (the anchor is weak)")
    return bad

def canal_check(modes, core_m=8.0, ring_in=25.0, ring_out=75.0, win=15):
    """Is the estimated temperature along canals cooler than their surroundings? Uses the OpenStreetMap lines and the saved map files.
    core = pixels covered by the water line (>= half of the pixel for canals and rivers, >= a quarter for thin ditches); surroundings = pixels 25-75 m from any water line. For every core pixel the
    difference to the average of the surrounding pixels within about 150 m is taken. Negative = cooler than the surroundings (what we expect for water)."""
    from scipy import ndimage as ndi
    def tok(m): c, _, e = m.partition('@'); c, _, tg = c.partition('#'); return cfg_name(parse_cfg(c)) + (f'_{tg}' if tg else TAGS) + CITYS + ('_ens' if e else '')
    modes = [tok(m) for m in modes]; found = [m for m in modes if os.path.exists(f'lst10m_v3_{m}_full.npy')]
    if not found: print('run `map <config>` first'); return
    meta = json.load(open(f'lst10m_v3_{found[0]}_meta.json')); x0, y1, n = meta['x0'], meta['y1'], meta['n']; lines = load_water()
    groups = {'canals and rivers': [l for l in lines if l['type'] in ('canal', 'river')], 'ditches, drains, streams': [l for l in lines if l['type'] not in ('canal', 'river')]}
    anyw = water_raster(lines, x0, y1, n) > 0.05; dist = ndi.distance_transform_edt(~anyw) * 10.0; ring = (dist >= ring_in) & (dist <= ring_out)
    layers = [('Landsat smooth (120 m, no model)', np.load(f'lst10m_v3_{found[0]}_smooth.npy'))]
    for m in found:
        fu = np.load(f'lst10m_v3_{m}_full.npy'); b120 = np.kron(fu.reshape(n // BLOCK, BLOCK, n // BLOCK, BLOCK).mean((1, 3)), np.ones((BLOCK, BLOCK), np.float32))     # the same map averaged to 120 m, as Landsat would see it
        layers += [(f'[{m}] blend', np.load(f'lst10m_v3_{m}_blend.npy')), (f'[{m}] full strength', fu), (f'[{m}] full, averaged to 120 m', b120)]
    print(f'map {n} x {n} pixels (10 m). Canal pixel minus the average of the surroundings (25-75 m away, within about {win * 10} m).')
    for gname, gl in groups.items():
        thr = 0.5 if gname.startswith('canals') else 0.25      # thin ditches never cover half of a 10 m pixel
        core = (water_raster(gl, x0, y1, n) >= thr) & ~ndi.binary_dilation(water_raster([l for l in lines if l not in gl], x0, y1, n) > 0.05, iterations=1) if len(gl) < len(lines) else (water_raster(gl, x0, y1, n) >= thr)
        print(f'\n{gname}: {int(core.sum())} core pixels')
        if core.sum() < 30: print('  too few pixels in this map'); continue
        for name, a in layers:
            num = ndi.uniform_filter(np.where(ring, a, 0.0), size=2 * win + 1) ; den = ndi.uniform_filter(ring.astype(np.float32), size=2 * win + 1); ok = core & (den > 0.05)
            d = a[ok] - num[ok] / den[ok]
            print(f'  {name:55s} mean {d.mean():+.2f} C | median {np.median(d):+.2f} C | cooler than surroundings: {100 * (d < 0).mean():.0f}% of canal pixels')
    print('\nReading it: the Landsat row shows how much cooler the canal corridor really is at about 100 m. If a model row is warmer than the surroundings (positive) where Landsat is cooler, the model is wrong along the canals.')


# ------------------------------------------------------------------ interactive map (one self-contained HTML file)
def utm42_to_ll(x, y):
    lon, lat = 69.0 + (x - 500000.0) / 83000.0, y / 111000.0           # first guess, then refine with the forward formula (Newton steps)
    for _ in range(8):
        x1, y1 = ll_to_utm42(lon, lat); dx, dy = x - x1, y - y1
        xa, ya = ll_to_utm42(lon + 1e-5, lat); xb, yb = ll_to_utm42(lon, lat + 1e-5)
        j = np.array([[(xa - x1) / 1e-5, (xb - x1) / 1e-5], [(ya - y1) / 1e-5, (yb - y1) / 1e-5]]); d = np.linalg.solve(j, [dx, dy]); lon += d[0]; lat += d[1]
    return lon, lat

def view_map(modes=('base', 'all')):
    import base64, io
    import matplotlib; matplotlib.use('Agg'); import matplotlib.cm as cm
    from matplotlib.colors import Normalize
    # 'all' = the single model, 'all@ens' = the seed average
    def tok(m): c, _, e = m.partition('@'); c, _, tg = c.partition('#'); return cfg_name(parse_cfg(c)) + (f'_{tg}' if tg else TAGS) + CITYS + ('_ens' if e else '')       # all, all@ens, all#apr_oct, all#apr_oct@ens (CITY=samarkand picks that city's maps)
    modes = [tok(m) for m in modes]; found = [m for m in modes if os.path.exists(f'lst10m_v3_{m}_full.npy')]
    if not found: print('run `map <config>` first'); return
    meta = json.load(open(f'lst10m_v3_{found[0]}_meta.json')); x0, y1, n = meta['x0'], meta['y1'], meta['n']; size = n
    corners = [utm42_to_ll(x0 + dx, y1 - dy) for dx in (0, size * 10) for dy in (0, size * 10)]
    south, north = min(c[1] for c in corners), max(c[1] for c in corners); west, east = min(c[0] for c in corners), max(c[0] for c in corners)
    smooth = np.load(f'lst10m_v3_{found[0]}_smooth.npy'); layers = [('Smooth Landsat (no model)', smooth, 'temp')]
    for m in found:
        full = np.load(f'lst10m_v3_{m}_full.npy'); blend = np.load(f'lst10m_v3_{m}_blend.npy')
        layers += [(f'U-Net [{m}]: smooth + {meta["detail_weight"]:g} x detail', blend, 'temp'), (f'U-Net [{m}]: full strength', full, 'temp'), (f'U-Net [{m}]: detail only (C vs smooth)', full - smooth, 'diff')]
        if os.path.exists(f'lst10m_v3_{m}_water.npy'): layers.append((f'OpenStreetMap water used by [{m}] (share of pixel)', np.load(f'lst10m_v3_{m}_water.npy'), 'mask'))
        if os.path.exists(f'lst10m_v3_{m}_unc.npy'): layers.append((f'U-Net [{m}]: seed disagreement (C, higher = less certain)', np.load(f'lst10m_v3_{m}_unc.npy'), 'unc'))
    if len(found) == 2: layers.append((f'{found[1]} minus {found[0]} (full strength, C)', np.load(f'lst10m_v3_{found[1]}_full.npy') - np.load(f'lst10m_v3_{found[0]}_full.npy'), 'diff'))
    out = []
    for name, arr, kind in layers:
        norm, cmap = {'temp': (Normalize(25, 55), matplotlib.colormaps['RdYlBu_r']), 'diff': (Normalize(-4, 4), matplotlib.colormaps['coolwarm']), 'unc': (Normalize(0, 1.5), matplotlib.colormaps['YlOrRd']), 'mask': (Normalize(0, 1), matplotlib.colormaps['cool'])}[kind]
        rgba = (cmap(norm(np.clip(arr, norm.vmin, norm.vmax))) * 255).astype(np.uint8); rgba[..., 3] = 255 if kind != 'mask' else (arr > 0.05) * 220      # the water layer is transparent where there is no water
        import matplotlib.image as mpimg; buf = io.BytesIO(); mpimg.imsave(buf, rgba, format='png')
        q = np.round(np.nan_to_num(arr, nan=-327.0) * 100).astype('<i2')
        out.append({'name': name, 'png': base64.b64encode(buf.getvalue()).decode(), 'vals': base64.b64encode(q.tobytes()).decode(), 'kind': kind})
    html = HTML_TEMPLATE.replace('__LAYERS__', json.dumps(out)).replace('__BOUNDS__', json.dumps([[south, west], [north, east]])).replace('__N__', str(n))
    open('lst10m_map.html', 'w').write(html); print('saved lst10m_map.html (', round(len(html) / 1e6, 1), 'MB ). Download it from the file browser and open it in a browser (it needs internet for the background map).')

HTML_TEMPLATE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Tashkent 10 m surface temperature (modelled)</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"><script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>html,body,#map{height:100%;margin:0;font-family:system-ui,sans-serif}.panel{position:absolute;z-index:1000;background:rgba(255,255,255,.93);padding:8px 10px;border-radius:6px;font-size:12px;box-shadow:0 1px 4px rgba(0,0,0,.3)}
#legend{left:10px;bottom:24px;width:260px}#bar{height:12px;background:linear-gradient(to right,#313695,#74add1,#e0f3f8,#ffffbf,#fdae61,#f46d43,#a50026)}#top{right:10px;bottom:24px;width:270px}#read{left:10px;top:10px;width:330px;max-height:60%;overflow:auto}</style></head><body><div id="map"></div>
<div class="panel" id="top"><b>Opacity</b> <input id="op" type="range" min="0" max="1" step="0.05" value="0.85" style="width:150px"><br><span style="color:#a00">MODELLED, NOT MEASURED. Detail inside ~100 m is inferred and unverified.</span></div>
<div class="panel" id="legend"><b>Surface temperature (C)</b><div id="bar"></div><div style="display:flex;justify-content:space-between"><span>25</span><span>40</span><span>55</span></div><div>Detail-only layers: blue = cooler than the smooth map, red = hotter (-4 to +4 C). Seed disagreement: 0 (pale) to 1.5 C (dark red)</div></div>
<div class="panel" id="read">Click the map to read every layer.</div>
<script>
var L_=__LAYERS__, B=__BOUNDS__, N=__N__;
var map=L.map('map').fitBounds(B);
var osm=L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'&copy; OpenStreetMap contributors'});
var sat=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',{maxZoom:19,attribution:'Imagery &copy; Esri'});
sat.addTo(map); var overlays={}, first=true;
L_.forEach(function(l){var b=atob(l.vals), u=new Uint8Array(b.length); for(var i=0;i<b.length;i++)u[i]=b.charCodeAt(i); l.arr=new Int16Array(u.buffer);
 l.layer=L.imageOverlay('data:image/png;base64,'+l.png,B,{opacity:0.85,interactive:false}); overlays[l.name]=l.layer; if(first){l.layer.addTo(map);first=false;}});
L.control.layers({'Satellite':sat,'OpenStreetMap':osm},overlays,{collapsed:false}).addTo(map);
document.getElementById('op').oninput=function(){var v=+this.value;L_.forEach(function(l){l.layer.setOpacity(v)});};
map.on('click',function(e){var c=Math.floor((e.latlng.lng-B[0][1])/(B[1][1]-B[0][1])*N), r=Math.floor((B[1][0]-e.latlng.lat)/(B[1][0]-B[0][0])*N), t='';
 if(c<0||r<0||c>=N||r>=N){document.getElementById('read').innerHTML='Outside the mapped tile.';return;}
 L_.forEach(function(l){var v=l.arr[r*N+c]/100; t+='<div>'+l.name+': <b>'+v.toFixed(1)+(l.kind=='temp'?' C':(l.kind=='unc'?' C spread':(l.kind=='mask'?' water share':' C diff')))+'</b></div>';});
 document.getElementById('read').innerHTML='<b>'+e.latlng.lat.toFixed(5)+', '+e.latlng.lng.toFixed(5)+'</b>'+t;});
</script></body></html>"""


# ------------------------------------------------------------------ smoke test with fake data (checks the code runs, says nothing about quality)
def smoke():
    import shutil, torch
    global CACHE, EPOCHS, ERA_FILE
    CACHE = 'cache_smoke'; ERA_FILE = os.path.join(CACHE, 'era5_scene.json'); EPOCHS = 1; os.makedirs(CACHE, exist_ok=True); rng = np.random.default_rng(0); era = {}
    for s in range(8):
        era[str(s)] = list(rng.uniform(0.3, 1.0, NSC).round(3))
        for k in range(3):
            base = rng.normal(0, 1, (13, PX // 8, PX // 8)).astype(np.float32); x = np.kron(base, np.ones((8, 8), np.float32)) * 0.05 + 0.3
            y = (-x[7].reshape(32, 12, 32, 12).mean((1, 3)) * 40 + 38).astype(np.float32)
            if k == 0: y[:2, :2] = -999
            np.savez_compressed(f'{CACHE}/{s:03d}_x_{k}.npz', x=x.astype(np.float16), y=y, scene=s, mean=38.0, x0=k * 8000, y1=8000 * s, t=0)
    json.dump(era, open(ERA_FILE, 'w'))
    for f in glob.glob(f'{CACHE}/*.npz'): l30 = np.kron(np.load(f)['y'], np.ones((4, 4), np.float32)); np.save(f.replace('.npz', '_l30.npy'), l30)
    global WATER_FILE
    WATER_FILE = os.path.join(CACHE, 'osm_water.json'); json.dump([{'pts': [[0.0, 8000.0 * s - 1900.0], [30000.0, 8000.0 * s - 1900.0 + 300.0]], 'w': 12.0, 'type': 'canal'} for s in range(8)], open(WATER_FILE, 'w'))
    make_water_files(load_water()); w_ = np.load(glob.glob(f'{CACHE}/wtr_*.npy')[0]); print('water mask share:', float((w_ > 0).mean()), 'max', int(w_.max()))
    for cfg in ('base', 'psf+t4', 'derived', 'gain', 'phys', 'sign', 'all', 'all+b30', 'b30', 'all+ndwi', 'ndwi', 'all+wmask', 'wmask', 'all+wphys'):
        train(cfg)
    evaluate('all+b30'); evaluate('base'); evaluate('all+wmask'); evaluate('all+wphys'); rf_baseline(True, trees=20); rf_baseline(False, trees=20)
    global ENS
    sd = torch.load('unet_lst_v3_derived+gain+phys+psf+sign+t4.pt', map_location='cpu'); sd = {k: (v + 0.05 * torch.randn_like(v) if v.is_floating_point() else v) for k, v in sd.items()}
    torch.save(sd, seed_file('derived+gain+phys+psf+sign+t4', 8)); ENS = [7, 8]; evaluate('all')
    m = load_model(parse_cfg('all'), 'derived+gain+phys+psf+sign+t4', 'cpu'); X, _, _, _, _, _, F_, *_ = load_patches(); sp_ = predict_spread(m, X[0], F_[0], 'cpu'); print('spread mean', float(sp_.mean()), 'shape', sp_.shape); assert sp_.mean() > 0
    global CITIES_ENV, HOLD_CITY, CITIES_FILE
    import shutil as _sh; _sh.copytree(CACHE, 'cache_fakecity'); CITIES_ENV = ['tashkent', 'fakecity']; HOLD_CITY = 'fakecity'     # two cities, one held out entirely
    train('all'); evaluate('all'); CITIES_ENV = []; HOLD_CITY = ''; _sh.rmtree('cache_fakecity')
    CITIES_FILE = os.path.join(CACHE, 'cities.geojson'); json.dump({'type': 'FeatureCollection', 'features': [{'type': 'Feature', 'properties': {'name': 'fakecity'}, 'geometry': {'type': 'Polygon', 'coordinates': [[[69.2, 41.3], [69.3, 41.3], [69.3, 41.4], [69.2, 41.4], [69.2, 41.3]]]}}]}, open(CITIES_FILE, 'w')); cities_report()
    ENS = []
    global SUBSET, TAGS
    json.dump({'first_new_scene': 4}, open(f'{CACHE}/season.json', 'w'))
    for SUBSET in ('old', 'new'): evaluate('all')
    SUBSET = ''; TAGS = '_tt'; torch.save(torch.load('unet_lst_v3_derived+gain+phys+psf+sign+t4.pt', map_location='cpu'), seed_file('derived+gain+phys+psf+sign+t4', 7)); evaluate('all'); os.remove(seed_file('derived+gain+phys+psf+sign+t4', 7)); TAGS = ''
    shutil.rmtree(CACHE); print('smoke test finished')

if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'help'; arg = sys.argv[2] if len(sys.argv) > 2 else 'all'
    if cmd == 'download': download()
    elif cmd == 'era5': era5()
    elif cmd == 'download30': download30()
    elif cmd == 'download_more': download(more=True)
    elif cmd == 'train': train(arg)
    elif cmd == 'evaluate': evaluate(arg)
    elif cmd == 'map': make_map(arg, tiles=int(os.environ.get('TILES', 2)), start=os.environ.get('START', '2025-06-01'), end=os.environ.get('END', '2025-08-31'))
    elif cmd == 'export_site': export_site(tuple(sys.argv[2:]) or ('all',))
    elif cmd == 'fetch_boundaries': fetch_boundaries([a for a in sys.argv[2:] if not a.startswith('--')] or None)
    elif cmd == 'cities': cities_report()
    elif cmd == 'data_check': data_check()
    elif cmd == 'fix_means': fix_means([a for a in sys.argv[2:]] or None)
    elif cmd == 'rf': rf_baseline(nb=(arg != 'base'))
    elif cmd == 'download_water': download_water()
    elif cmd == 'probe': probe(float(sys.argv[2]), float(sys.argv[3]))
    elif cmd == 'canal': canal_check(tuple(sys.argv[2:]) or ('all',))
    elif cmd == 'view': view_map(tuple(sys.argv[2:]) or ('base', 'all'))
    elif cmd == 'smoke': smoke()
    else: print(__doc__)
