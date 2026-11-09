"""Step 7: data for the 3D video: two 600 m squares (microrayon-style and low-rise), buildings in local metres, and for every video frame the sun position and the share of open ground in shade.
usage: python3 07_scene_data.py buildings_h.parquet mahallas.geojson MICRO_ID LOWRISE_ID OUT.json [DATE] [FRAMES]"""
import sys, os, json, datetime as dt, importlib, numpy as np, geopandas as gpd, shapely
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); S = importlib.import_module('03_shade')
b = gpd.read_parquet(sys.argv[1]).to_crs(32642); mh = gpd.read_file(sys.argv[2]).to_crs(32642); ids = (int(sys.argv[3]), int(sys.argv[4])); out = sys.argv[5]
date = dt.date.fromisoformat(sys.argv[6]) if len(sys.argv) > 6 else dt.date(2024, 6, 4); NF = int(sys.argv[7]) if len(sys.argv) > 7 else 288; HALF = 300; BUF = 400
T0, T1 = 6 * 60 + 30, 18 * 60 + 30                              # 06:30 to 18:30 local
mins = np.linspace(T0, T1, NF); sunp = [S.sun(date, int(m // 60), float(m % 60)) for m in mins]
tiles = []
for label, i in zip(('Microrayon-style blocks', 'Low-rise private houses'), ids):
    c = mh[mh.mahalla_id == i].geometry.iloc[0].representative_point(); cx, cy = c.x, c.y; box = shapely.box(cx - HALF, cy - HALF, cx + HALF, cy + HALF)
    near = b[b.intersects(box.buffer(BUF))].reset_index(drop=True); inbox = near[near.intersects(box)]; roofs = shapely.union_all(inbox.geometry.intersection(box).values); ground = box.area - roofs.area
    shade = []
    for el, az in sunp:
        if el < 4: shade.append(None); continue
        sp = S.shadows(near.geometry.values, near.height_model_m.values, el, az); shade.append(round(float(shapely.union_all(sp).intersection(box).difference(roofs).area / ground * 100), 1))
    bl = []
    for g, h in zip(near.geometry, near.height_model_m):
        for p in (g.geoms if g.geom_type == 'MultiPolygon' else [g]):
            p = shapely.affinity.translate(p.simplify(0.4), -cx, -cy)
            if p.is_empty or p.geom_type != 'Polygon': continue
            bl.append(dict(h=float(h), r=[[round(x, 1), round(y, 1)] for x, y in list(p.exterior.coords)[:-1]], holes=[[[round(x, 1), round(y, 1)] for x, y in list(r.coords)[:-1]] for r in p.interiors]))
    tiles.append(dict(label=label, half=HALF, buildings=bl, shade=shade, n=len(bl)))
    print(label, len(bl), 'buildings, mean shade over day', round(float(np.nanmean([s for s in shade if s is not None])), 1))
json.dump(dict(date=str(date), minutes=[float(m) for m in mins], sun=[dict(el=round(e, 2), az=round(a, 2)) for e, a in sunp], tiles=tiles), open(out, 'w'), separators=(',', ':')); print('wrote', out, round(os.path.getsize(out) / 1e6, 1), 'MB')
