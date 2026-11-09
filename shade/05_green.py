"""Step 5 (proxy for trees): share of each mahalla covered by OpenStreetMap vegetation (parks, gardens, grass, woods, orchards) + mapped individual trees per km2.
OSM coverage of trees is patchy, so this is a rough stand-in until satellite vegetation (NDVI) is added.   usage: python3 05_green.py osm.pbf mahallas.geojson OUT.csv"""
import sys, osmium, shapely.wkb as wkb, geopandas as gpd, numpy as np, pandas as pd, rasterio.features as rf, math
from rasterio.transform import from_origin
fab = osmium.geom.WKBFactory(); polys = []; trees = []
W, S, E, N = 69.08, 41.20, 69.47, 41.42
GREEN = {'landuse': {'grass', 'forest', 'meadow', 'orchard', 'village_green', 'recreation_ground', 'vineyard', 'flowerbed'}, 'leisure': {'park', 'garden', 'nature_reserve'}, 'natural': {'wood', 'scrub', 'grassland', 'tree_row'}}
class H(osmium.SimpleHandler):
    def node(s, n):
        if n.tags.get('natural') == 'tree' and n.location.valid() and W <= n.location.lon <= E and S <= n.location.lat <= N: trees.append((n.location.lon, n.location.lat))
    def area(s, a):
        t = a.tags
        if any(t.get(k) in v for k, v in GREEN.items()):
            try: g = wkb.loads(fab.create_multipolygon(a), hex=True)
            except Exception: return
            c = g.centroid
            if W <= c.x <= E and S <= c.y <= N: polys.append(g)
H().apply_file(sys.argv[1], locations=True)
mh = gpd.read_file(sys.argv[2]).to_crs(32642); gp = gpd.GeoSeries(polys, crs=4326).to_crs(32642); tr_ = gpd.GeoSeries(gpd.points_from_xy(*zip(*trees)), crs=4326).to_crs(32642)
RES = 10.0; x0, y0, x1, y1 = mh.total_bounds; x0 = math.floor(x0 / RES) * RES; y1 = math.ceil(y1 / RES) * RES; Wd = int((x1 - x0) / RES) + 1; Hd = int((y1 - y0) / RES) + 1; t = from_origin(x0, y1, RES, RES)
mid = rf.rasterize(((g, i + 1) for i, g in enumerate(mh.geometry)), out_shape=(Hd, Wd), transform=t, dtype='int32', fill=0); gr = rf.rasterize(((g, 1) for g in gp), out_shape=(Hd, Wd), transform=t, dtype='uint8', fill=0).astype(bool)
n = len(mh) + 1; tot = np.bincount(mid[mid > 0], minlength=n); gs = np.bincount(mid[(mid > 0) & gr], minlength=n)
j = gpd.sjoin(gpd.GeoDataFrame(geometry=tr_), mh[['geometry']].reset_index(), predicate='within'); tc = j.groupby('index').size().reindex(range(len(mh))).fillna(0).values
out = pd.DataFrame({'mahalla_id': mh.mahalla_id, 'osm_green_share': np.round(gs[1:] / np.maximum(tot[1:], 1), 4), 'osm_trees_per_km2': np.round(tc / np.maximum(tot[1:] * RES * RES / 1e6, 1e-6), 1)}); out.to_csv(sys.argv[3], index=False)
print(len(polys), 'vegetation areas,', len(trees), 'trees; mean green share', round(float(out.osm_green_share.mean()), 3))
