"""Step 1: building footprints + estimated heights for Tashkent from an OpenStreetMap extract.
usage: python3 01_buildings.py uzbekistan.osm.pbf OUT.parquet
Height rule (first that applies): OSM height tag -> building:levels x 3.0 m -> default levels for the building type.
Every row records where its height came from (height_src) so the estimate is always visible."""
import sys, osmium, shapely.wkb as wkb, geopandas as gpd, re
W, S, E, N = 69.08, 41.20, 69.47, 41.42
DEFAULT_LEVELS = {'apartments': 5, 'dormitory': 5, 'hotel': 4, 'office': 4, 'commercial': 3, 'retail': 2, 'hospital': 3, 'school': 3, 'kindergarten': 2,
                  'university': 4, 'public': 2, 'civic': 2, 'industrial': 1.5, 'warehouse': 1.5, 'house': 1, 'detached': 1, 'residential': 1.5, 'garage': 1,
                  'garages': 1, 'shed': 1, 'roof': 1, 'yes': 1.2}
LEVEL_H = 3.0
fab = osmium.geom.WKBFactory()
rows = []
def num(v):
    m = re.match(r'\s*([0-9]+(?:[.,][0-9]+)?)', v or ''); return float(m.group(1).replace(',', '.')) if m else None
class H(osmium.SimpleHandler):
    def area(s, a):
        t = a.tags; b = t.get('building')
        if not b or b == 'no': return
        try: g = wkb.loads(fab.create_multipolygon(a), hex=True)
        except Exception: return
        c = g.centroid
        if not (W <= c.x <= E and S <= c.y <= N): return
        h = num(t.get('height')); lv = num(t.get('building:levels')); src = 'osm_height'
        if h is None or h < 2 or h > 250:
            if lv and 0 < lv < 80: h = lv * LEVEL_H; src = 'osm_levels'
            else: h = DEFAULT_LEVELS.get(b, 1.2) * LEVEL_H; src = 'type_default'
        rows.append((b, round(h, 1), src, g))
H().apply_file(sys.argv[1], locations=True)
gdf = gpd.GeoDataFrame(rows, columns=['btype', 'height_m', 'height_src', 'geometry'], crs=4326)
gdf.to_parquet(sys.argv[2]); print(len(gdf), 'buildings'); print(gdf.height_src.value_counts().to_dict()); print(gdf.btype.value_counts().head(8).to_dict())
