"""Step 3: where the sun is, where building shadows fall, and how much open ground each mahalla has in shade.
usage: python3 03_shade.py buildings_h.parquet ../public/data/tashkent_mahallas_master.geojson OUT.csv [DATE]   (DATE default 2024-06-04 = a Landsat scene date)
Model: shadow of a building = convex hull of its footprint and the footprint moved along the shadow direction by height / tan(sun elevation).
Ground = everything that is not a building roof. Trees, walls' fine detail, slopes and clouds are NOT modelled (modelled, not measured).
Times are local Tashkent time (UTC+5). The 10:55 slice matches a Landsat overpass."""
import sys, math, datetime as dt, numpy as np, geopandas as gpd, shapely, rasterio.features as rf
from rasterio.transform import from_origin
LAT, LON = 41.31, 69.28; RES = 5.0
def sun(date, hh, mm=0):
    n = date.timetuple().tm_yday; utc = hh + mm / 60 - 5; g = 2 * math.pi / 365 * (n - 1 + (utc - 12) / 24)
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g) - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    dec = 0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g)
    tst = utc * 60 + eqt + 4 * LON; ha = math.radians(tst / 4 - 180); la = math.radians(LAT)
    cz = math.sin(la) * math.sin(dec) + math.cos(la) * math.cos(dec) * math.cos(ha); z = math.acos(max(-1, min(1, cz)))
    az = math.atan2(math.sin(ha), math.cos(ha) * math.sin(la) - math.tan(dec) * math.cos(la)) + math.pi          # from north, clockwise
    return 90 - math.degrees(z), math.degrees(az) % 360
def shadows(geoms, h, elev, az):
    if elev < 4: return None
    L = h / math.tan(math.radians(elev)); dx = -L * math.sin(math.radians(az)); dy = -L * math.cos(math.radians(az))
    co, idx = shapely.get_coordinates(geoms, return_index=True); sh = co + np.c_[dx[idx], dy[idx]]
    allc = np.vstack([co, sh]); ai = np.r_[idx, idx]; o = np.argsort(ai, kind='stable'); mp = shapely.multipoints(allc[o], indices=ai[o]); return shapely.convex_hull(mp)
if __name__ == '__main__':
    date = dt.date.fromisoformat(sys.argv[4]) if len(sys.argv) > 4 else dt.date(2024, 6, 4)
    b = gpd.read_parquet(sys.argv[1]).to_crs(32642); mh = gpd.read_file(sys.argv[2]).to_crs(32642)
    x0, y0, x1, y1 = mh.total_bounds; x0 = math.floor(x0 / RES) * RES; y1 = math.ceil(y1 / RES) * RES; W = int((x1 - x0) / RES) + 1; Hh = int((y1 - y0) / RES) + 1; tr = from_origin(x0, y1, RES, RES)
    print('grid', W, 'x', Hh, 'at', RES, 'm')
    mid = rf.rasterize(((g, i + 1) for i, g in enumerate(mh.geometry)), out_shape=(Hh, W), transform=tr, dtype='int32', fill=0)
    roof = rf.rasterize(((g, 1) for g in b.geometry), out_shape=(Hh, W), transform=tr, dtype='uint8', fill=0).astype(bool)
    N = len(mh) + 1; ground = (mid > 0) & ~roof; tot = np.bincount(mid[ground], minlength=N).astype(float); allpx = np.bincount(mid[mid > 0], minlength=N).astype(float)
    res = mh[['mahalla_id', 'mahalla_name', 'district_id', 'population']].copy(); res['area_ha'] = (allpx[1:] * RES * RES / 1e4).round(2); res['building_cover'] = (np.bincount(mid[(mid > 0) & roof], minlength=N)[1:] / np.maximum(allpx[1:], 1)).round(4)
    ba = b.area.values; cen = b.geometry.centroid; bm = rf.rasterize(((p, i + 1) for i, p in enumerate(mh.geometry)), out_shape=(1, 1), transform=from_origin(0, 0, 1, 1), fill=0) if False else None
    j = gpd.sjoin(gpd.GeoDataFrame({'a': ba, 'h': b.height_model_m.values}, geometry=cen, crs=b.crs), mh[['geometry']].reset_index().rename(columns={'index': 'mi'}), how='inner', predicate='within')
    j['ah'] = j.a * j.h; gp = j.groupby('mi').agg(A=('a', 'sum'), AH=('ah', 'sum'), tall=('h', lambda s: 0)).reindex(range(len(mh))); res['mean_building_height_m'] = (gp.AH / gp.A).values.round(1)
    tallA = j[j.h >= 15].groupby('mi').a.sum().reindex(range(len(mh))).fillna(0).values; res['tall_share_of_footprint'] = (tallA / gp.A.values).round(3)
    slots = [(9, 0), (10, 55), (12, 0), (14, 0), (16, 0), (17, 0)]; days = []
    for hh, mm in slots:
        el, az = sun(date, hh, mm); sp = shadows(b.geometry.values, b.height_model_m.values, el, az); key = f'shade_{hh:02d}{mm:02d}'
        sh = rf.rasterize(((p, 1) for p in sp), out_shape=(Hh, W), transform=tr, dtype='uint8', fill=0).astype(bool) & ground
        res[key] = (np.bincount(mid[sh], minlength=N)[1:] / np.maximum(tot[1:], 1)).round(4); days.append(key); print(key, f'sun elevation {el:.0f} deg, azimuth {az:.0f} deg; city open-ground shade {sh.sum()/ground.sum():.1%}', flush=True)
    res['shade_mean_day'] = res[days].mean(axis=1).round(4); res.to_csv(sys.argv[3], index=False); print('wrote', sys.argv[3], len(res))
